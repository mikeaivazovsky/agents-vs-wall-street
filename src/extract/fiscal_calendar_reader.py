"""Work out when each company's financial year actually closes.

A column headed only by a year names the financial year, not the day it ended,
and the two are rarely the same thing.

Three of these companies keep a fifty two or fifty three week year, so the
closing date drifts by a few days and lands in a different month from one year
to the next. Home Depot closes on the Sunday nearest the end of January, which
falls in January in some years and February in others. Analog Devices and Deere
close near the end of October, sometimes slipping into November. Hays, being a
United Kingdom company, closes on 30 June every year without drift.

The financial year is named after the calendar year holding most of it, so a
year that closes in January or February is named after the year before its
closing date. Home Depot's financial 2025 ran from February 2025 to February
2026 and is called 2025 throughout, while Deere's financial 2025 closed in
October 2025 and carries the same number as its closing date.

None of this is written down here. The closing dates are read out of the
documents, which state them in full whenever they present a yearly figure, and
the naming rule follows from the month they fall in.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from src.domain.domain_values import Company
from src.sources.docs_listing import TABULAR_KINDS, documents

# "years ended October 30, 2022", "year ended 30 June 2019", and the same
# phrases with a fiscal prefix. The date is what matters, so both the American
# and the British ordering are read.
_YEAR_ENDED = re.compile(
    r"(?:fiscal\s+)?years?\s+ended\s+"
    r"(?:([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})|(\d{1,2})\s+([A-Za-z]+),?\s*(\d{4}))",
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Almost everywhere a financial year is named after the calendar year it ends
# in: Hays closes on 30 June 2021 and calls that year 2021. The exception is
# the American retail calendar, which closes just after the Christmas trading
# season in January or early February and names the year after the one it
# spent almost all of. Home Depot ran from February 2025 to February 2026 and
# called it 2025 throughout.
#
# So the naming only shifts when the closing month is January or February.
_LAST_RETAIL_CLOSING_MONTH = 2


@dataclass(frozen=True)
class FiscalCalendar:
    """When one company's financial years closed."""

    company: Company
    closing_month: int
    year_ends: dict[int, date]

    @property
    def named_after_previous_year(self) -> bool:
        return self.closing_month <= _LAST_RETAIL_CLOSING_MONTH

    def closing_date(self, financial_year: int) -> date | None:
        """The day the named financial year closed, if the documents state it."""
        return self.year_ends.get(financial_year)

    def label_for(self, closing: date) -> int:
        """The financial year a closing date belongs to."""
        return closing.year - 1 if self.named_after_previous_year else closing.year


@lru_cache(maxsize=8)
def calendar_for(company: Company) -> FiscalCalendar:
    """Read a company's financial calendar out of its own documents.

    Every closing date the documents state is collected. The month they mostly
    fall in decides how a financial year is named, and dates from any other
    month are left out: they come from a subsidiary or a comparative table
    kept on a different calendar, not from the company's own year.
    """
    found: list[date] = []
    for document in documents(company, kinds=TABULAR_KINDS):
        for match in _YEAR_ENDED.finditer(document.read_text()):
            closing = _date_from(match)
            if closing is not None:
                found.append(closing)

    if not found:
        return FiscalCalendar(company=company, closing_month=0, year_ends={})

    # A drifting year straddles two months, so the neighbouring month counts as
    # the same closing point rather than as a different calendar.
    months = Counter(item.month for item in found)
    main_month = months.most_common(1)[0][0]
    accepted = {main_month, main_month % 12 + 1, (main_month - 2) % 12 + 1}

    year_ends: dict[int, date] = {}
    for closing in found:
        if closing.month not in accepted:
            continue
        label = (closing.year - 1 if main_month <= _LAST_RETAIL_CLOSING_MONTH
                 else closing.year)
        # The latest closing date seen for a financial year is the real one:
        # an earlier date in the same window belongs to a shorter period.
        if label not in year_ends or closing > year_ends[label]:
            year_ends[label] = closing

    return FiscalCalendar(
        company=company, closing_month=main_month, year_ends=year_ends
    )


def _date_from(match: re.Match) -> date | None:
    month_name, day, year, uk_day, uk_month_name, uk_year = match.groups()
    if month_name:
        month = _MONTHS.get(month_name.lower())
        parts = (year, month, day)
    else:
        month = _MONTHS.get((uk_month_name or "").lower())
        parts = (uk_year, month, uk_day)

    if not month or not all(parts):
        return None
    try:
        return date(int(parts[0]), parts[1], int(parts[2]))
    except (ValueError, TypeError):
        return None
