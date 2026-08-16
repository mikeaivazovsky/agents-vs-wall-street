"""Orchestration: turn the specification into four filled workbooks.

The pipeline reads the specification, walks the twelve metrics company by
company, obtains a figure for each one, hands it to the writer and logs every
step.

Each company is processed on its own. A company that fails is logged and the
run continues, so a fault in the third company does not throw away the two
workbooks already finished.

There is exactly one place where a figure is obtained, obtain_figure below.
That function is the seam the forecasting module will occupy. Nothing in
src/workbook depends on it, so replacing it does not reach into the writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.decision_model.history_analyzer import forecast as forecast_from_history
from src.decision_model.history_analyzer import series_from
from src.decision_model.llm_adjuster import review
from src.decision_model.metric_forecaster import decide
from src.domain.domain_values import Company, Figure
from src.domain.spec import ChallengeSpec, MetricSpec, default_spec
from src.extract.numbers_presenter import gather, write_history
from src.run.logging import RunLogger, relative_path
from src.workbook.writer import WorkbookWriter


# Written only when no evidence at all stands behind a metric. A cell is never
# left empty, because a missing figure scores the maximum accuracy penalty, but
# a figure reached this way is reported as a placeholder all the way up.
PLACEHOLDER_VALUE = 0.0


@dataclass
class RunResult:
    """What one execution of the pipeline achieved."""

    written: int = 0
    failed: int = 0
    placeholders: int = 0
    saved: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def is_forecast(self) -> bool:
        """Whether every figure written came from the forecasting work."""
        return self.placeholders == 0


def obtain_figure(
    metric: MetricSpec,
    observations,
    logger: RunLogger | None = None,
) -> tuple[Figure, bool]:
    """Produce the figure for one metric.

    Returns the figure and whether it is a placeholder rather than a forecast.
    A cell is never left empty, because a missing figure scores the maximum
    accuracy penalty, but a placeholder is reported all the way up so that a
    run full of them cannot be mistaken for a finished one.

    Guidance and commentary are not read yet. The decision is built to run on
    whichever signals exist, so those arrive as extra arguments here without
    anything downstream changing.
    """
    series = series_from(metric, observations)
    history = forecast_from_history(metric, series)

    # The model is asked to challenge the extrapolation only once there is an
    # extrapolation to challenge. With nothing reported there is nothing for it
    # to reason about, and a figure invented from an empty series would be the
    # opposite of what this system is for.
    verdict = review(metric, series, history) if history is not None else None

    decision = decide(
        metric,
        history=history,
        adjustment=verdict.adjustment if verdict else None,
        adjustment_reason=verdict.reason if verdict else "",
    )

    if logger is not None:
        logger.info(
            f"{metric.company.value} | {metric.id.value} | method {decision.method} "
            f"| signals {'+'.join(decision.signals_used) or 'none'} "
            f"| missing {'+'.join(decision.signals_missing)} "
            f"| confirmed periods {len(series.points)}"
        )
        for line in decision.reasoning:
            logger.info(f"{metric.company.value} | {metric.id.value} | {line}")

        if verdict is not None:
            logger.info(
                f"{metric.company.value} | {metric.id.value} | model | "
                f"{verdict.model} | confidence {verdict.confidence} | "
                f"{'cached' if verdict.cached else 'called'}"
            )

        # Every reported figure the forecast rests on, named by document and
        # line. A reader can open each one and see the same row.
        if history is not None:
            for point in _unique_sources(history.sources):
                logger.info(
                    f"{metric.company.value} | {metric.id.value} | evidence | "
                    f"{point.period_end} = {point.value:,.2f} | "
                    f"{point.document}:{point.line} | {point.quote[:90]}"
                )

    return decision.figure, not decision.is_forecast


def _unique_sources(points):
    """Each cited figure once, newest period first."""
    seen: dict[tuple, object] = {}
    for point in points:
        seen.setdefault((point.document, point.line), point)
    return sorted(seen.values(), key=lambda point: point.period_end, reverse=True)


def run(
    logger: RunLogger,
    spec: ChallengeSpec | None = None,
) -> RunResult:
    """Fill and save every workbook in the specification."""
    challenge = spec or default_spec()
    result = RunResult()

    logger.info(
        f"specification loaded | {len(challenge.metrics)} metrics "
        f"| {len(challenge.companies())} companies"
    )

    for company in challenge.companies():
        _run_company(company, challenge, logger, result)

    logger.summary(written=result.written, failed=result.failed)
    if result.placeholders:
        logger.warning(
            f"{result.placeholders} of {len(challenge.metrics)} figures are "
            f"placeholders | these workbooks must not be uploaded"
        )
    return result


def _run_company(
    company: Company,
    challenge: ChallengeSpec,
    logger: RunLogger,
    result: RunResult,
) -> None:
    """Fill and save one company's workbook.

    Every failure is contained here. The workbook is saved only once all of its
    metrics are in place, so a partly filled workbook is never left in
    submission/ to be uploaded by mistake.
    """
    metrics = challenge.for_company(company)
    if not metrics:
        logger.warning(f"{company.value} | no metrics in the specification")
        return

    logger.info(f"{company.value} | starting | {len(metrics)} metrics")
    pending = len(metrics)

    # The corpus is read once for the whole company. Each of its three metrics
    # is then decided from the history already in hand.
    history = gather(company, spec=challenge)
    logger.info(
        f"{company.value} | history gathered | "
        + " | ".join(
            f"{name} {sum(1 for a in items if a.trust == 'confirmed')} confirmed"
            for name, items in history.items()
        )
    )

    # The history is saved before any figure is decided from it, so the record
    # of what the run had to work from survives even if the rest fails.
    saved_history = write_history(company, history)
    logger.info(f"{company.value} | history saved | {relative_path(saved_history)}")

    try:
        with WorkbookWriter(metrics[0].template_path, metrics[0].output_path) as writer:
            for metric in metrics:
                figure, is_placeholder = obtain_figure(
                    metric, history.get(metric.id.value, []), logger
                )

                if is_placeholder:
                    result.placeholders += 1
                    logger.warning(
                        f"{company.value} | {metric.id.value} | placeholder value, "
                        f"not a forecast"
                    )

                writer.write(metric, figure)
                pending -= 1
                result.written += 1
                logger.metric_written(
                    company=company,
                    metric_id=metric.id,
                    figure=figure,
                    cell=metric.cell,
                    workbook=metric.workbook,
                )

            saved = writer.save()

        result.saved.append(saved)
        logger.workbook_saved(saved)

    except Exception as error:
        # The remaining metrics for this company are counted as failures: the
        # workbook was not saved, so none of them reached a cell.
        result.written -= len(metrics) - pending
        result.failed += len(metrics)
        logger.exception(f"{company.value} | workbook not produced", error)
