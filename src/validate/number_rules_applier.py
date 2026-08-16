"""Rules that decide whether a figure can be what it claims to be.

Two jobs, both driven by config/number_rules.yaml rather than by anything
written here.

The first is choosing which number in a row to take. A row often prints a
money amount and a percentage together, so the cell carrying the per cent sign
is preferred when the metric is a percentage.

The second is rejecting the impossible. A margin of six million per cent is not
a surprising result, it is a misread cell, and letting it through would poison
whatever is calculated from it later.

These limits describe units, not companies. Nothing here says what any company
is likely to report. That belongs to a stage that measures it from the history
this one helps produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml

from src.domain.domain_values import Unit
from src.domain.spec import PROJECT_ROOT

RULES_PATH = PROJECT_ROOT / "config" / "number_rules.yaml"


class RulesError(RuntimeError):
    """Raised when the number rules are missing or malformed."""


@dataclass(frozen=True)
class UnitRule:
    """What a single unit is allowed to hold."""

    minimum: float
    maximum: float
    prefer_marked: str | None

    def holds(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum

    def describe(self) -> str:
        return f"{self.minimum:g} to {self.maximum:g}"


@lru_cache(maxsize=1)
def _rules() -> dict:
    try:
        parsed = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RulesError(f"cannot read {RULES_PATH.name}: {error}") from error
    if not isinstance(parsed, dict):
        raise RulesError(f"{RULES_PATH.name} must be a mapping")
    return parsed


def scale_factor(scale_source: str) -> float | None:
    """The factor a printed figure must be multiplied by, if it is stated.

    Returns None when the table says nothing about its scale, which the caller
    records as a concern rather than guessing.
    """
    lowered = scale_source.lower()
    for word, factor in (_rules().get("scale_words") or {}).items():
        if word in lowered:
            return float(factor)
    return None


def looks_like_chart_axis(values: list[float]) -> bool:
    """Whether a row of numbers is a chart axis rather than reported results.

    A slide converts its charts to tables, and an axis becomes a row of evenly
    spaced tick marks. Reported financials are effectively never evenly spaced,
    so an exact constant step across three or more figures marks an axis.
    """
    settings = _rules().get("reject_even_spacing") or {}
    if not settings.get("enabled"):
        return False
    least = int(settings.get("minimum_length", 3))
    if len(values) < least:
        return False

    step = values[1] - values[0]
    if step == 0:
        return False
    return all(
        abs((values[index + 1] - values[index]) - step) < 1e-9
        for index in range(len(values) - 1)
    )


def rule_for(unit: Unit) -> UnitRule | None:
    """The limits for one unit, or None if the unit has none."""
    entry = (_rules().get("units") or {}).get(unit.value)
    if not entry:
        return None
    return UnitRule(
        minimum=float(entry["min"]),
        maximum=float(entry["max"]),
        prefer_marked=entry.get("prefer_marked"),
    )


def choose_cell(
    cells: list[tuple[int, str, float]],
    unit: Unit,
) -> tuple[int, str, float] | None:
    """Pick which number in a row states the metric.

    cells holds the position, original text and parsed value of every number in
    the row, in the order they appear. The leftmost is taken unless the unit
    names a mark to look for, in which case the leftmost cell carrying that
    mark wins. That is what separates a gross margin of fifty one per cent from
    the gross profit of 2,440 million printed beside it.
    """
    if not cells:
        return None

    rule = rule_for(unit)
    if rule and rule.prefer_marked:
        for candidate in cells:
            if rule.prefer_marked in candidate[1]:
                return candidate

    if unit is not Unit.PERCENT:
        # A cell carrying a per cent sign states a rate of change, never an
        # amount of money. Statements print the growth rate beside the figure,
        # and on a slide whose money cells were mangled in conversion the
        # growth rate is the first cell that still parses. Taking it would
        # record a percentage as millions.
        without_rates = [item for item in cells if "%" not in item[1]]
        return without_rates[0] if without_rates else None

    return cells[0]


def rejection_reason(value: float, unit: Unit) -> str | None:
    """Why a figure cannot be held by its unit, or None if it can."""
    rule = rule_for(unit)
    if rule is None or rule.holds(value):
        return None
    return (
        f"{value:,.4g} is outside what {unit.value} can hold "
        f"({rule.describe()}), so the cell was misread"
    )
