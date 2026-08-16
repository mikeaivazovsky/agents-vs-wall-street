"""Domain vocabulary shared by every layer.

This module depends on nothing else in the project. Everything else may depend
on it.

The enums exist so that a unit or a reporting basis cannot be misspelled or
silently swapped. Confusing two units, for example writing pounds where pence
are expected, changes a figure by a factor of one hundred and is the most
expensive mistake this system can make, so units are a type rather than a
convention.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


class Company(str, Enum):
    """The four companies in the challenge, keyed by ticker.

    The values are the ticker strings used in challenge/companies.json and are
    matched against it directly, so Hays carries its exchange prefix.
    """

    HD = "HD"
    ADI = "ADI"
    HAS = "LSE:HAS"
    DE = "DE"

    @property
    def slug(self) -> str:
        """The ticker in a form usable inside an identifier."""
        return self.value.split(":")[-1].lower()


class FiscalPeriod(str, Enum):
    """Reporting periods used by the challenge.

    The values match the period header in each template exactly. Three of the
    companies report a quarter and Hays reports a full financial year.
    """

    FY2026Q2 = "FY2026Q2"
    FY2026Q3 = "FY2026Q3"
    FY2026 = "FY2026"


class Unit(str, Enum):
    """Units a figure can be expressed in.

    The values are the exact unit strings in challenge/companies.json, which
    are also the text printed in the units column of the templates. One value
    therefore serves as the internal unit, as the key read from the organisers'
    definition and as the text the workbook is checked against.

    Percentages are held as percentage points, so 4.5 means 4.5 per cent.
    GBP_PENCE is pence rather than pounds, so 6.2 means 6.2 pence. Hays is the
    only company reporting in pence, and treating its earnings per share as
    pounds would understate the figure by a factor of one hundred.
    """

    USD_MILLIONS = "USDm"
    USD_PER_SHARE = "USD / share"
    GBP_MILLIONS = "GBPm"
    GBP_PENCE = "GBp"
    PERCENT = "%"


class DocumentType(str, Enum):
    """Kinds of document in the historical corpus.

    The values are the document_type strings written in each document's front
    matter. Filings and slide decks carry financial tables. Call transcripts
    carry none at all, so they hold no reported figure that can be read from a
    table and are excluded when history is being gathered.
    """

    FILING = "FILING"
    SLIDE = "SLIDE"
    CALL_TRANSCRIPT = "CALL_TRANSCRIPT"


class PeriodKind(str, Enum):
    """How much time a reported figure covers.

    A single table often prints a quarter and a year to date side by side, so a
    figure is meaningless until it is known which of them it came from.
    """

    QUARTER = "quarter"
    HALF = "half"
    NINE_MONTHS = "nine_months"
    YEAR = "year"
    UNKNOWN = "unknown"


class Basis(str, Enum):
    """The accounting basis a figure is measured on.

    Two figures can carry the same label and unit and still be incomparable if
    they rest on different bases. Deere's earnings per share is required on a
    generally accepted accounting principles basis while Home Depot's and
    Analog Devices' are required adjusted, and an adjusted figure is normally
    the higher of the two.
    """

    REPORTED = "reported"
    GAAP = "gaap"
    ADJUSTED = "adjusted"
    PRE_EXCEPTIONAL = "pre_exceptional"


class MetricId(str, Enum):
    """Stable identifier for each of the twelve required figures.

    An identifier is derived from the ticker and the metric label rather than
    chosen by hand, so it cannot disagree with the organisers' definition. Use
    metric_id_for to build one. The twelve members below are the identifiers
    that challenge/companies.json produces, and an unknown identifier means the
    organisers changed a label.
    """

    HD_NET_SALES = "hd_net_sales"
    HD_ADJUSTED_DILUTED_EPS = "hd_adjusted_diluted_eps"
    HD_COMPARABLE_SALES_TOTAL_COMPANY = "hd_comparable_sales_total_company"

    ADI_REVENUE = "adi_revenue"
    ADI_ADJUSTED_DILUTED_EPS = "adi_adjusted_diluted_eps"
    ADI_ADJUSTED_GROSS_MARGIN = "adi_adjusted_gross_margin"

    HAS_NET_FEES = "has_net_fees"
    HAS_PRE_EXCEPTIONAL_BASIC_EPS = "has_pre_exceptional_basic_eps"
    HAS_PRE_EXCEPTIONAL_OPERATING_PROFIT = "has_pre_exceptional_operating_profit"

    DE_WORLDWIDE_NET_SALES_AND_REVENUES = "de_worldwide_net_sales_and_revenues"
    DE_DILUTED_EPS_GAAP = "de_diluted_eps_gaap"
    DE_PRODUCTION_PRECISION_AG_OPERATING_PROFIT = (
        "de_production_precision_ag_operating_profit"
    )


_NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")


def metric_id_for(company: Company, label: str) -> MetricId:
    """Build the identifier for a company and metric label.

    Raises ValueError if the pair does not correspond to one of the twelve
    known metrics, which is how a changed or misspelled label is caught.
    """
    slug = _NON_IDENTIFIER.sub("_", label.strip().lower()).strip("_")
    candidate = f"{company.slug}_{slug}"
    try:
        return MetricId(candidate)
    except ValueError as error:
        raise ValueError(
            f"{company.value} metric {label!r} is not one of the twelve known "
            f"metrics (derived identifier {candidate!r})"
        ) from error


def basis_for(label: str) -> Basis:
    """Read the reporting basis out of the metric label.

    The organisers state the basis inside the label itself, for example
    "Diluted EPS (GAAP)" against "Adjusted diluted EPS". Reading it from there
    keeps the basis tied to their wording instead of a separate judgement that
    could disagree with it.

    A label that names no basis is treated as reported, which is the usual case
    for a revenue or a segment profit line.
    """
    lowered = label.lower()
    if "gaap" in lowered:
        return Basis.GAAP
    if "pre-exceptional" in lowered:
        return Basis.PRE_EXCEPTIONAL
    if "adjusted" in lowered:
        return Basis.ADJUSTED
    return Basis.REPORTED


class UnitMismatch(ValueError):
    """Raised when a figure is used where a different unit is expected."""


@dataclass(frozen=True)
class Figure:
    """A number together with the unit it is measured in.

    Layers above extraction pass figures rather than bare floats, so a unit
    always travels with its value and can be checked at the point of use.
    """

    value: float
    unit: Unit

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise TypeError(f"figure value must be a number, got {type(self.value).__name__}")
        if not math.isfinite(self.value):
            raise ValueError(f"figure value must be finite, got {self.value!r}")

    def require_unit(self, expected: Unit) -> None:
        """Raise unless this figure is already in the expected unit.

        There is no automatic conversion. A figure that arrives in the wrong
        unit indicates a mistake earlier in the pipeline, and converting it
        here would hide that mistake rather than fix it.
        """
        if self.unit is not expected:
            raise UnitMismatch(
                f"expected a figure in {expected.value!r} but got {self.unit.value!r}"
            )

    def __str__(self) -> str:
        return f"{self.value} {self.unit.value}"
