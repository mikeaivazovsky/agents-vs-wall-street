"""Ask a model to challenge the extrapolation, within a limit.

The arithmetic that produces a figure cannot judge whether the recent past is a
fair guide to the next period. It reads a growth rate off four quarters and
carries it forward. It cannot know that a rate of twenty five per cent belongs
to a semiconductor cycle turning up from a trough and will not hold, or that a
base quarter contained a one off that will not repeat.

That judgement is what a model is asked for here, and nothing else. It sees the
reported series, the method used and the figure proposed, and answers with a
proportional correction and its reasoning. It never sees a workbook, never
chooses a final number, and cannot move the figure beyond the limit the caller
imposes.

The limit exists because of what the published research finds. Models reading
company narrative forecast earnings with larger errors than analyst consensus
and lean optimistic, while seasonal extrapolation performs close to consensus.
The arithmetic is therefore the anchor and the model is the correction, not the
other way round.

A refusal counts as a valid answer. Where the series gives no reason to depart
from the extrapolation, no adjustment is the right answer, and the prompt says
so plainly to keep an obliging model from inventing a reason to move.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from src.decision_model.history_analyzer import HistoryForecast, Series
from src.domain.spec import MetricSpec
from src.sources.openai_client import ask, is_configured

# Kept in step with the limit metric_forecaster applies. Stating it in the
# prompt as well means the model reasons inside the same bounds it will be
# held to, rather than proposing a move that is silently cut down.
ADJUSTMENT_LIMIT = 0.10

# How much of the series the model is shown. Twelve periods reach back three
# years for a quarterly reporter, far enough to show a cycle without burying
# the recent trend in history that no longer describes the business.
_PERIODS_SHOWN = 12

_INSTRUCTIONS = """
You review a forecast for one financial metric of one company.

A figure has already been produced by seasonal extrapolation: the same period a
year earlier, carried forward at the growth rate of recent periods. You are
given the reported series it was built from.

Your only task is to judge whether that extrapolation is a fair guide to the
next period, and to propose a proportional correction if it is not.

Correct the figure only for a reason visible in the series you were given, such
as:
- a growth rate that reflects a recovery from an unusually weak base and is
  unlikely to persist at that pace
- a base period that looks exceptional against its neighbours
- a trend that is clearly decelerating or accelerating across recent periods
- seasonality the simple year on year comparison does not capture

Do not correct for anything you cannot see in the series. You have no news, no
guidance, no analyst estimates and no knowledge of events after the last period
shown. Do not use anything you may recall about this company from elsewhere.

If the extrapolation looks reasonable, answer with an adjustment of zero. That
is a correct and expected answer, not a failure to be helpful.

The adjustment is a proportion of the proposed figure, so 0.03 raises it by
three per cent. It must lie between -{limit} and {limit}.
""".strip()


class Adjustment(BaseModel):
    """A proposed correction to an extrapolated figure."""

    adjustment: float = Field(
        description="Proportional change to the proposed figure, zero to leave it alone",
        ge=-ADJUSTMENT_LIMIT,
        le=ADJUSTMENT_LIMIT,
    )
    reason: str = Field(
        description="One sentence naming what in the series justifies the change",
        max_length=400,
    )
    confidence: str = Field(description="One of: low, medium, high")


@dataclass(frozen=True)
class AdjusterVerdict:
    """What the model answered, and how it was obtained."""

    adjustment: float
    reason: str
    confidence: str
    model: str
    cached: bool


def review(
    metric: MetricSpec,
    series: Series,
    history: HistoryForecast,
) -> AdjusterVerdict | None:
    """Ask for a correction to an extrapolated figure. None when unavailable."""
    if not is_configured():
        return None

    question = _describe(metric, series, history)
    reply = ask(
        instructions=_INSTRUCTIONS.format(limit=ADJUSTMENT_LIMIT),
        question=question,
        schema=Adjustment,
        cache_key_extra=metric.id.value,
    )
    if reply is None:
        return None

    answer = reply.value
    return AdjusterVerdict(
        adjustment=answer.adjustment,
        reason=answer.reason,
        confidence=answer.confidence,
        model=reply.model,
        cached=reply.cached,
    )


def _describe(metric: MetricSpec, series: Series, history: HistoryForecast) -> str:
    """State the case for review in plain terms.

    The series is given oldest first, the way a reader follows a trend. Only
    the period and the figure are shown: the model is judging the shape of the
    numbers, and document names would invite it to reason about sources it
    cannot open.
    """
    shown = list(reversed(series.points[:_PERIODS_SHOWN]))
    lines = [f"  {point.period_end}  {point.value:,.2f}" for point in shown]

    span = shown[0].period_end if shown else "unknown"
    return "\n".join(
        [
            f"Company: {metric.company.value}",
            f"Metric: {metric.label}",
            f"Unit: {metric.unit.value}",
            f"Reporting basis: {metric.basis.value}",
            f"Period being forecast: {metric.period.value}",
            "",
            f"Reported series, oldest first, from {span}:",
            *lines,
            "",
            f"Method used: {history.method}",
            f"Reasoning: {history.reasoning}",
            f"Figure proposed: {history.figure.value:,.4f} {metric.unit.value}",
        ]
    )
