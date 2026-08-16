"""Collect one company's observations into a history file.

Reads every document listed for a company, gathers the stated values of that
company's three metrics, removes repeats and writes them in period order to
artifacts/<TICKER>/history.json.

The same figure is printed many times: an annual report repeats the quarters,
and a slide deck repeats the release. Repeats are collapsed on the period and
the value, keeping the earliest publication, because the first statement of a
figure is the one the market saw.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

from src.domain.domain_values import Company, PeriodKind
from src.domain.spec import PROJECT_ROOT, ChallengeSpec, default_spec
from src.extract.fiscal_calendar_reader import calendar_for
from src.extract.history_number_extractor import Observation, labels_for, observations
from src.sources.docs_listing import TABULAR_KINDS, Document, documents

ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"

# Ways of reading a date that fix the exact day a period closed. A date read
# from a year alone, or from text beside the table, places a figure in time
# but does not pin it to a period.
_PRECISE_DATES = ("column", "matched", "calendar")


def gather(
    company: Company,
    spec: ChallengeSpec | None = None,
    since: date | None = None,
    on_document=None,
) -> dict[str, list["Assessed"]]:
    """Every observation for one company, grouped by metric.

    on_document is called with each document as it is read, so a caller can
    report progress without this module knowing how progress is displayed.
    """
    challenge = spec or default_spec()
    metrics = challenge.for_company(company)
    searches = {metric.id: labels_for(metric.id) for metric in metrics}
    collected: dict[str, list[Observation]] = {metric.id.value: [] for metric in metrics}

    for document in documents(company, kinds=TABULAR_KINDS, since=since):
        for metric in metrics:
            for item in observations(document, metric, searches[metric.id]):
                collected[metric.id.value].append(item)
        if on_document is not None:
            on_document(document)

    return {name: assess(items) for name, items in collected.items()}


def _settle_dates(items: list[Observation]) -> list[Observation]:
    """Give an approximately dated figure the date another document states.

    A figure read from a column with its own date is settled: the date and the
    figure sit in the same column and belong together. A figure read from a
    table that compares two years in one row is not, because the heading names
    both years and the row prints both figures.

    The same figure almost always appears again elsewhere, in the release that
    first stated it, where its column does carry a date. Where exactly one such
    statement exists for the same value and the same reporting span, its date
    is adopted here. Nothing is invented: the date comes from a document that
    printed it against that figure.

    Where several periods share a value, or none states it precisely, the
    approximate date is left alone and stays marked as such.
    """
    settled: dict[tuple, set[date]] = {}
    for item in items:
        if item.period_source == "column" and item.period_end is not None:
            key = (round(item.value, 4), item.period_kind)
            settled.setdefault(key, set()).add(item.period_end)

    resolved: list[Observation] = []
    for item in items:
        if item.period_source == "column" or item.period_end is None:
            resolved.append(item)
            continue

        closing = _closing_date_for(item)
        if closing is not None:
            resolved.append(
                replace(
                    item,
                    period_end=closing,
                    period_source="calendar",
                    concerns=tuple(
                        concern for concern in item.concerns
                        if "names a year rather than a closing date" not in concern
                    ),
                )
            )
            continue

        known = settled.get((round(item.value, 4), item.period_kind))
        if known and len(known) == 1:
            only = next(iter(known))
            resolved.append(
                replace(
                    item,
                    period_end=only,
                    period_source="matched",
                    concerns=tuple(
                        concern for concern in item.concerns
                        if "the date was taken from" not in concern
                    ),
                )
            )
        else:
            resolved.append(item)

    return resolved


_MISSING_SCALE = "scale not stated in the table"

# Money is abbreviated by whole factors of a thousand and by nothing else, so
# these are the only corrections a missing scale can call for.
_SCALE_STEPS = (0.000001, 0.001, 1.0, 1000.0)

# How far a figure may sit from the company's own usual size and still be the
# same quantity. Revenue moves over a decade and a quarter is a fraction of a
# year, so the window is wide, but it is far narrower than the thousandfold
# gap between one scale and the next.
_SAME_SIZE = 20.0


def _settle_scales(items: list[Observation]) -> list[Observation]:
    """Rescale a figure whose table never said what units it was printed in.

    A table that omits its scale still states a real figure, and the company's
    own reported history says how large that figure should be. Where a reading
    is a clean factor of a thousand away from the size this company reports,
    the table was printed in thousands or in billions and the reading is
    corrected to millions.

    Only exact factors of a thousand are applied, because that is the only way
    money is ever abbreviated. A figure that is merely unusual, rather than
    misscaled, is left alone and stays marked as doubtful.
    """
    known = [
        item.value for item in items
        if _MISSING_SCALE not in item.concerns and item.value
    ]
    if len(known) < 3:
        return items

    usual = sorted(abs(value) for value in known)[len(known) // 2]
    if usual <= 0:
        return items

    resolved: list[Observation] = []
    for item in items:
        if _MISSING_SCALE not in item.concerns or not item.value:
            resolved.append(item)
            continue

        best = min(
            _SCALE_STEPS,
            key=lambda step: abs(math.log10(abs(item.value) * step / usual)),
        )
        corrected = abs(item.value) * best
        if not (usual / _SAME_SIZE <= corrected <= usual * _SAME_SIZE):
            resolved.append(item)
            continue

        resolved.append(
            replace(
                item,
                value=round(item.value * best, 6),
                concerns=tuple(c for c in item.concerns if c != _MISSING_SCALE),
            )
        )
    return resolved


def _closing_date_for(item: Observation) -> date | None:
    """Turn a figure dated only to a year into the day that year closed.

    A yearly figure headed "2022" ended on the day that company's financial
    year closed, which the documents state in full elsewhere. A figure covering
    a shorter span cannot be placed this way: a quarter of 2022 could be any of
    four, and guessing one would be worse than admitting the period is unknown.
    """
    if item.period_source != "year" or item.period_end is None:
        return None
    if item.period_kind is not PeriodKind.YEAR:
        return None
    return calendar_for(item.company).closing_date(item.period_end.year)


@dataclass(frozen=True)
class Assessed:
    """One figure together with what the other documents say about it."""

    observation: Observation
    support: int
    competing: tuple[float, ...]
    trust: str


def assess(items: list[Observation]) -> list[Assessed]:
    """Collapse repeats and judge each figure against the rest.

    A company states the same figure many times: in the release, in the
    quarterly filing, in next year's comparatives and on a slide. Agreement
    between documents that were converted separately is the strongest evidence
    available here that a cell was read correctly, and disagreement is the
    clearest sign that one of them was not.

    Three outcomes:

      conflicted  another document claims a different figure for the same
                  period, so at least one reading is wrong
      confirmed   nothing contradicts it, and either the reading raised no
                  concern or separate documents agree on it
      doubtful    nothing contradicts it, but the reading left a concern and
                  no second document backs it up
    """
    items = _settle_scales(_settle_dates(items))

    same_figure: dict[tuple, list[Observation]] = {}
    for item in items:
        key = (item.period_end, item.period_kind, round(item.value, 4))
        same_figure.setdefault(key, []).append(item)

    values_by_period: dict[tuple, set[float]] = {}
    for period_end, period_kind, value in same_figure:
        values_by_period.setdefault((period_end, period_kind), set()).add(value)

    assessed: list[Assessed] = []
    for key, group in same_figure.items():
        period_end, period_kind, value = key
        # The earliest statement of a figure is the one the market saw first,
        # and later repeats are copies of it.
        chosen = min(group, key=lambda item: item.evidence.published_at)
        support = len({item.evidence.document for item in group})
        competing = tuple(
            sorted(other for other in values_by_period[(period_end, period_kind)]
                   if other != value)
        )

        # Two figures disagree only when both claim the same period exactly.
        # A figure with no date, or dated only to a year, claims no period, so
        # such figures sit together in one group without contradicting one
        # another.
        precise = period_end is not None and chosen.period_source in _PRECISE_DATES
        if competing and precise:
            trust = "conflicted"
        elif not chosen.concerns or support >= 2:
            trust = "confirmed"
        else:
            trust = "doubtful"

        assessed.append(
            Assessed(observation=chosen, support=support, competing=competing, trust=trust)
        )

    return sorted(assessed, key=lambda item: item.observation.sort_key(), reverse=True)


def build(
    company: Company,
    spec: ChallengeSpec | None = None,
    since: date | None = None,
    on_document=None,
) -> Path:
    """Read one company's history and write it out. Returns the file path."""
    grouped = gather(company, spec=spec, since=since, on_document=on_document)
    return write_history(company, grouped)


def write_history(company: Company, grouped: dict[str, list["Assessed"]]) -> Path:
    """Record a company's gathered history under artifacts.

    Separate from gathering so that a caller which already holds the history
    does not read the corpus a second time to save it. The file is the record
    of what the run actually had to work from, and is written whether or not
    the figures that follow turn out well.
    """
    payload = {
        "company": company.value,
        "metrics": {
            name: {
                "observations": [_as_json(item) for item in items],
                "count": len(items),
            }
            for name, items in grouped.items()
        },
    }

    directory = ARTIFACTS_DIR / company.slug.upper()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "history.json"
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return target


def _as_json(entry: "Assessed") -> dict:
    item = entry.observation
    record = asdict(item)
    record["metric_id"] = item.metric_id.value
    record["company"] = item.company.value
    record["unit"] = item.unit.value
    record["basis"] = item.basis.value
    record["period_kind"] = item.period_kind.value
    record["trust"] = entry.trust
    record["support_documents"] = entry.support
    record["competing_values"] = list(entry.competing)
    return record
