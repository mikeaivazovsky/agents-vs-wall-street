"""Read one metric's reported figures out of a document.

Given a document and a metric, this finds the table rows that state that
metric, reads the value, works out the scale it is printed in and the period it
covers, and returns each one with the exact line it came from.

Nothing is guessed. A figure whose scale or period cannot be established is
still returned, marked with the reason, so that the coverage of the extractor
can be measured rather than assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from src.domain.domain_values import Basis, Company, MetricId, PeriodKind, Unit
from src.domain.spec import PROJECT_ROOT, MetricSpec
from src.extract.tables_aligning import Row, Table, tables
from src.sources.docs_listing import Document
from src.validate.number_rules_applier import (
    choose_cell,
    looks_like_chart_axis,
    rejection_reason,
    scale_factor,
)

LABELS_PATH = PROJECT_ROOT / "config" / "metric_labels.yaml"

# Units expressed per share or as a percentage are printed at their true size.
# Only money totals are abbreviated to thousands or millions.
_UNSCALED_UNITS = (Unit.USD_PER_SHARE, Unit.GBP_PENCE, Unit.PERCENT)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# "May 3,2026" and "May 3, 2026" both occur, and so does "3 May 2026" in the
# United Kingdom documents.
_DATE_US = re.compile(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})\b")
_DATE_UK = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+)\.?,?\s*(\d{4})\b")
_YEAR_ONLY = re.compile(r"^(?:fy)?\s*(\d{4})$", re.IGNORECASE)

_SPANS = (
    (re.compile(r"\bthree\s+months\b|\bfirst\s+quarter\b|\bsecond\s+quarter\b"
                r"|\bthird\s+quarter\b|\bfourth\s+quarter\b|\bquarter\s+ended\b",
                re.IGNORECASE), PeriodKind.QUARTER),
    (re.compile(r"\bsix\s+months\b|\bhalf\s+year\b|\bfirst\s+half\b", re.IGNORECASE),
     PeriodKind.HALF),
    (re.compile(r"\bnine\s+months\b", re.IGNORECASE), PeriodKind.NINE_MONTHS),
    (re.compile(r"\btwelve\s+months\b|\byear\s+ended\b|\bfiscal\s+year\b"
                r"|\bfull\s+year\b|\bannual\b", re.IGNORECASE), PeriodKind.YEAR),
)

# A cell holding one number, possibly negative in accounting brackets, possibly
# carrying its own currency mark or per cent sign.
_NUMBER = re.compile(
    r"^[^\d(+-]*?(\(?\s*[+-]?\d[\d,\s]*(?:\.\d+)?\s*\)?)\s*[%a-zA-Z]{0,3}$"
)

# A number that uses the comma as a thousands separator: every group after the
# first holds exactly three digits.
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})*(\.\d+)?$")


@dataclass(frozen=True)
class Evidence:
    """Where a figure was found, in enough detail to check it by hand."""

    document: str
    document_type: str
    published_at: date
    line: int
    quote: str
    caption: str
    scale_source: str
    source_label: str


@dataclass(frozen=True)
class Observation:
    """One reported figure for one metric, as printed in one document."""

    metric_id: MetricId
    company: Company
    value: float
    unit: Unit
    basis: Basis
    period_end: date | None
    period_kind: PeriodKind
    period_source: str
    evidence: Evidence
    confidence: str
    concerns: tuple[str, ...] = field(default_factory=tuple)

    def sort_key(self) -> tuple:
        return (self.period_end or date.min, self.evidence.published_at)


@dataclass(frozen=True)
class MetricLabels:
    """The row labels to look for, and the table captions to skip."""

    synonyms: frozenset[str]
    ignore_captions: tuple[str, ...]


@lru_cache(maxsize=1)
def _labels_file() -> dict:
    try:
        parsed = yaml.safe_load(LABELS_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError(f"cannot read {LABELS_PATH.name}: {error}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{LABELS_PATH.name} must be a mapping")
    return parsed


def labels_for(metric_id: MetricId) -> MetricLabels:
    """The search terms for one metric."""
    parsed = _labels_file()
    entry = (parsed.get("metrics") or {}).get(metric_id.value)
    if not entry or not entry.get("synonyms"):
        raise RuntimeError(f"{LABELS_PATH.name} lists no synonyms for {metric_id.value}")
    shared = [str(item).lower() for item in (parsed.get("ignore_captions") or [])]
    own = [str(item).lower() for item in (entry.get("ignore_captions") or [])]
    return MetricLabels(
        synonyms=frozenset(normalise(item) for item in entry["synonyms"]),
        ignore_captions=tuple(shared + own),
    )


def normalise(text: str) -> str:
    """Lowercase a label and collapse its spacing, for comparison only."""
    cleaned = str(text).lower()

    # A footnote dagger is a marker rather than decoration. Hays uses it to
    # mark the line stated before exceptional items, so it survives while the
    # markup wrapped around it does not.
    cleaned = cleaned.replace("^{‡}", "‡").replace("^{†}", "†")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    # Footnote numbers and a bracketed unit or qualifier are printed inside the
    # label and carry no meaning for matching: "Comparable sales (% change) (1)"
    # names the same line as "comparable sales".
    cleaned = re.sub(r"\(\d+\)|\[\d+\]|\(\s*%[^)]*\)|\(pence\)|\(\$m\)|\(£m\)",
                     " ", cleaned)
    cleaned = cleaned.replace("’", "'").replace(" ", " ")
    cleaned = re.sub(r"[:.]+$", "", cleaned.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_number(cell: str) -> float | None:
    """Read a number from a table cell.

    Accounting notation puts a negative number in brackets, so "(14)" is minus
    fourteen. A cell containing anything other than one number is rejected,
    which keeps footnote markers and stray words out of the results.
    """
    match = _NUMBER.match(cell.strip())
    if not match:
        return None
    body = match.group(1).strip()

    # A thousands separator always leaves groups of exactly three digits. A
    # cell reading "3,5" is a reference to footnotes three and five printed
    # beside the label, not the number thirty five, and reading it as a figure
    # would replace the real one standing further along the row.
    bare = body.strip("()").replace(" ", "")
    if "," in bare and not _THOUSANDS.match(bare):
        return None

    negative = body.startswith("(") and body.endswith(")")
    digits = bare.replace(",", "")
    if not digits or digits in {"+", "-"}:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def parse_period_end(cell: str) -> date | None:
    """Read the date a column of figures is measured to."""
    text = cell.strip()

    match = _DATE_US.search(text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                return None

    match = _DATE_UK.search(text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(1)))
            except ValueError:
                return None

    match = _YEAR_ONLY.match(text)
    if match:
        # A column headed only by a year states a full year figure. The last
        # day of that calendar year is a placeholder for ordering, and the
        # period kind records what it really means.
        return date(int(match.group(1)), 12, 31)
    return None


def _period_kinds_in(table: Table, caption: str) -> tuple[PeriodKind, ...]:
    """Every reporting span the table mentions, in the order they are printed.

    A quarterly statement prints the quarter beside the year to date, and the
    order the two are named in is the order their columns appear, so the order
    is what lets a column be tied to a span.
    """
    haystack = " ".join([caption] + [" ".join(row.cells) for row in table.rows])
    seen: list[tuple[int, PeriodKind]] = []
    for pattern, kind in _SPANS:
        match = pattern.search(haystack)
        if match:
            seen.append((match.start(), kind))
    return tuple(kind for _, kind in sorted(seen))


def _column_groups(header: Row) -> dict[int, int]:
    """Group each header column by which block of periods it belongs to.

    A statement that covers two spans prints the same dates twice, once for the
    quarter and once for the year to date:

        | Selected sales data: | February 1, 2026 | February 2, 2025 | %Change
                               | February 1, 2026 | February 2, 2025 | %Change |

    The second time a date reappears, a new block has started. Columns that
    hold no date, such as a percentage change, belong to the block on their
    left.
    """
    groups: dict[int, int] = {}
    times_seen: dict[date, int] = {}
    current = 0

    for position in range(1, len(header.cells)):
        found = parse_period_end(header.cells[position])
        if found is not None:
            current = times_seen.get(found, 0)
            times_seen[found] = current + 1
        groups[position] = current
    return groups


def _scale_factor(scale_source: str, unit: Unit) -> tuple[float, str | None]:
    """How much to multiply a printed figure by to reach the target unit.

    A figure stated per share or as a percentage is printed at its true size,
    so the abbreviation that applies to the money totals in the same table does
    not apply to it.
    """
    if unit in _UNSCALED_UNITS:
        return 1.0, None

    factor = scale_factor(scale_source)
    if factor is None:
        return 1.0, "scale not stated in the table"
    return factor, None


def _header_above(table: Table, row_index: int) -> Row | None:
    """The nearest row above a data row that carries column dates."""
    for candidate in reversed(table.rows[:row_index]):
        if any(parse_period_end(cell) for cell in candidate.cells[1:]):
            return candidate
    return None


def _table_wide_period(table: Table, row_index: int) -> date | None:
    """A date that applies to the whole table rather than to one column.

    Some statements head the table with a single line, "Three Months Ended
    May 3, 2026", instead of dating each column. The date then covers every
    figure below it. Such a line is recognised by carrying a date and no
    figures at all, which keeps it apart from a row of results.
    """
    for candidate in reversed(table.rows[:row_index]):
        if any(parse_number(cell) is not None for cell in candidate.cells[1:]):
            continue
        for cell in candidate.cells:
            found = parse_period_end(cell)
            if found is not None:
                return found
    return None


def _context_period(table: Table) -> date | None:
    """A date stated in the text introducing the table."""
    return parse_period_end(table.context) if table.context else None


def observations(
    document: Document,
    metric: MetricSpec,
    labels: MetricLabels | None = None,
) -> tuple[Observation, ...]:
    """Every stated value of one metric inside one document.

    A figure whose value cannot be held by the metric's unit is left out: it
    is a misread cell rather than an unusual result.
    """
    search = labels or labels_for(metric.id)
    found: list[Observation] = []

    for table in tables(document.read_text()):
        caption = table.caption
        if any(term in caption.lower() for term in search.ignore_captions):
            continue

        spans = _period_kinds_in(table, caption)
        factor, scale_concern = _scale_factor(table.scale_hint, metric.unit)

        for index, row in enumerate(table.rows):
            if normalise(row.label) not in search.synonyms:
                continue

            candidates = [
                (position, cell, parse_number(cell))
                for position, cell in enumerate(row.cells[1:], start=1)
            ]
            candidates = [item for item in candidates if item[2] is not None]
            if looks_like_chart_axis([item[2] for item in candidates]):
                continue
            chosen = choose_cell(candidates, metric.unit)
            if chosen is None:
                continue

            position, _, raw_value = chosen
            value = round(raw_value * factor, 6)

            # A figure its own unit cannot hold was misread, and keeping it
            # would put a wrong number into everything computed from this
            # history later.
            impossible = rejection_reason(value, metric.unit)
            if impossible:
                continue

            header = _header_above(table, index)

            concerns: list[str] = []
            if scale_concern:
                concerns.append(scale_concern)

            period_end = None
            period_source = "none"
            # A header whose label cell is blank loses it when the row is
            # evened out, while the data row keeps its label. The header is
            # then one cell shorter and every date sits one place to the left
            # of the figure it belongs to. Measuring the difference puts them
            # back together, and a header of equal width is left untouched.
            offset = len(row.cells) - len(header.cells) if header is not None else 0
            dated_position = position - offset

            if header is not None and 0 <= dated_position < len(header.cells):
                cell = header.cells[dated_position]
                period_end = parse_period_end(cell)
                if period_end is not None:
                    # A column headed only by a year names the year, not the
                    # day the period closed. Deere and Hays both close their
                    # year away from December, so the placeholder date is not
                    # a date they ever report to, and several different periods
                    # of the same year would otherwise land on it together.
                    if _YEAR_ONLY.match(cell.strip()):
                        period_source = "year"
                        concerns.append(
                            "the column names a year rather than a closing "
                            "date, so the period is known only to the year"
                        )
                    else:
                        period_source = "column"

            if period_end is None:
                # A date taken from anywhere other than the column itself
                # cannot say which column it belongs to. A table comparing two
                # years prints both in one row, so a single date read from the
                # heading would be attached to whichever figure came first and
                # would be wrong for the other. Such a date is kept, because it
                # still places the figure roughly in time, but it is never
                # treated as settled.
                period_end = _table_wide_period(table, index)
                period_source = "table" if period_end else "none"
                if period_end is None:
                    period_end = _context_period(table)
                    period_source = "context" if period_end else "none"

                concerns.append(
                    f"the date was taken from the {period_source} rather than "
                    f"from the column, so it may belong to a neighbouring figure"
                    if period_end is not None
                    else "no date found for this figure"
                )

            period_kind = PeriodKind.UNKNOWN
            if len(spans) == 1:
                period_kind = spans[0]
            elif len(spans) > 1 and header is not None:
                # Tie the column to a span by which block of repeated dates it
                # falls in. The blocks appear in the same order as the spans
                # are named above them.
                block = _column_groups(header).get(dated_position)
                if block is not None and block < len(spans):
                    period_kind = spans[block]
                else:
                    concerns.append(
                        "the column could not be tied to one of the reporting "
                        "spans the table states"
                    )
            elif len(spans) > 1:
                concerns.append(
                    "table states more than one reporting span and has no "
                    "dated header to tie the column to one of them"
                )

            found.append(
                Observation(
                    metric_id=metric.id,
                    company=metric.company,
                    value=value,
                    unit=metric.unit,
                    basis=metric.basis,
                    period_end=period_end,
                    period_kind=period_kind,
                    period_source=period_source,
                    evidence=Evidence(
                        document=document.relative_name(),
                        document_type=document.document_type.value,
                        published_at=document.published_at,
                        line=row.line_number,
                        quote=row.raw[:300],
                        caption=caption[:200],
                        scale_source=table.scale_hint,
                        source_label=row.label[:120],
                    ),
                    confidence="high" if not concerns else "low",
                    concerns=tuple(concerns),
                )
            )

    return tuple(found)
