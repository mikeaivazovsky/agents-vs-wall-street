"""Decide one figure from whatever evidence is available.

This is the only place a final number is chosen. It gathers the signals that
exist, weighs them, and records why the answer is what it is.

Three signals are planned and one is built:

  history    what the company has already reported, extrapolated forward
  guidance   what the company has told the market to expect for this period
  factors    what the calls and commentary say about demand, price and margin

Weighing follows what the evidence supports. Guidance carries more weight than
extrapolation because management is describing a period it is already trading
through, while extrapolation only knows the past. Factors do not produce a
figure of their own; they move the figure the others produce, within a limit,
because a narrative reading is the least verifiable signal here and should not
be able to overturn reported arithmetic.

Every signal is optional. A missing signal reduces the weight behind the answer
and is named in the record, but never stops a figure being produced: an empty
cell scores the maximum penalty, so a weakly supported number beats none.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.domain_values import Figure
from src.domain.spec import MetricSpec
from src.decision_model.history_analyzer import HistoryForecast

# Relative weight of each signal that states a figure of its own. Guidance
# outranks extrapolation: management is reporting on a period already under
# way, and the published research finds analyst and management expectations
# harder to beat than a seasonal extrapolation of the past.
_WEIGHTS = {"guidance": 0.6, "history": 0.4}

# The most a narrative reading may move the figure the reported evidence
# produced. Commentary is the least checkable signal available, and models
# reading it are known to lean optimistic, so it adjusts rather than decides.
_MAX_ADJUSTMENT = 0.10


@dataclass(frozen=True)
class Decision:
    """The chosen figure and the reasoning behind it."""

    metric: MetricSpec
    figure: Figure
    method: str
    signals_used: tuple[str, ...]
    signals_missing: tuple[str, ...]
    reasoning: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_forecast(self) -> bool:
        """Whether any evidence at all stood behind this figure."""
        return bool(self.signals_used)


def decide(
    metric: MetricSpec,
    history: HistoryForecast | None = None,
    guidance: Figure | None = None,
    adjustment: float | None = None,
    adjustment_reason: str = "",
) -> Decision:
    """Combine the available signals into one figure.

    adjustment is a proportion, so 0.02 raises the figure by two per cent. It
    is clamped rather than rejected: a reading that wants to move the number
    further than the limit still points the right way, and the limit is what
    keeps it from deciding on its own.
    """
    stated: list[tuple[str, float, float]] = []
    if guidance is not None:
        guidance.require_unit(metric.unit)
        stated.append(("guidance", guidance.value, _WEIGHTS["guidance"]))
    if history is not None:
        history.figure.require_unit(metric.unit)
        stated.append(("history", history.figure.value, _WEIGHTS["history"]))

    used = [name for name, _, _ in stated]
    missing = [name for name in ("guidance", "history") if name not in used]
    reasoning: list[str] = []

    if not stated:
        # Nothing reported and nothing promised. There is no honest number to
        # give, and the caller decides what to write rather than this module
        # inventing one.
        missing.append("factors")
        return Decision(
            metric=metric,
            figure=Figure(value=0.0, unit=metric.unit),
            method="no evidence",
            signals_used=(),
            signals_missing=tuple(missing),
            reasoning=("no confirmed history and no guidance were available",),
        )

    # A weighted mean over whichever signals exist. With one signal the weight
    # cancels and that signal stands alone, which is what should happen.
    total_weight = sum(weight for _, _, weight in stated)
    value = sum(figure * weight for _, figure, weight in stated) / total_weight

    if history is not None:
        reasoning.append(f"history: {history.reasoning}")
    if guidance is not None:
        reasoning.append(f"guidance: the company has pointed to {guidance.value:,.2f}")
    if len(stated) > 1:
        shares = ", ".join(
            f"{name} {weight / total_weight:.0%}" for name, _, weight in stated
        )
        reasoning.append(f"combined as a weighted mean, {shares}")

    method = "+".join(used)

    if adjustment:
        limited = max(-_MAX_ADJUSTMENT, min(_MAX_ADJUSTMENT, adjustment))
        before = value
        value = value * (1 + limited)
        used.append("factors")
        method += "+factors"
        reasoning.append(
            f"factors: {adjustment_reason or 'commentary'} moved the figure "
            f"{limited:+.1%} from {before:,.2f}"
            + (
                f", limited from {adjustment:+.1%}"
                if abs(adjustment) > _MAX_ADJUSTMENT
                else ""
            )
        )
    else:
        missing.append("factors")

    return Decision(
        metric=metric,
        figure=Figure(value=round(value, 4), unit=metric.unit),
        method=method,
        signals_used=tuple(used),
        signals_missing=tuple(missing),
        reasoning=tuple(reasoning),
    )
