"""Extrapolate a metric forward from what the company has already reported.

Only figures the extraction stage marked confirmed are used. A doubtful figure
is usually a real number carrying the wrong period, an annual total sitting in
a quarterly series, and one of those destroys both the trend and the seasonal
shape it is meant to inform.

Two kinds of metric are forecast differently, because they behave differently.

A flow, such as revenue or profit or earnings per share, grows in proportion:
it is carried forward by multiplying the same period a year earlier by the
growth rate the recent periods have shown. A quarter is compared with the same
quarter of the previous year rather than with the quarter before it, because
these businesses are seasonal. Home Depot sells far more in spring than in
winter, and Deere far more in the planting season.

A ratio, such as a gross margin or a comparable sales change, does not compound.
Doubling revenue does not double a margin. A ratio is carried forward from its
recent level, moved by the amount it has typically been moving.

The literature is the reason this is arithmetic rather than a model call. On
quarterly earnings, seasonal extrapolation performs close to analyst consensus,
and analysts beat it only slightly more often than not.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from src.domain.domain_values import Figure, PeriodKind, Unit
from src.domain.spec import MetricSpec

# A year apart, give or take the drift of a fifty two or fifty three week
# calendar and the odd extra week.
_YEAR = timedelta(days=365)
_YEAR_SLACK = timedelta(days=45)

# How many recent periods inform the growth rate. Four covers a full year of
# quarters, which is enough to see a trend without reaching back into a
# different trading environment.
_RECENT = 4

# Fewest observations that allow a trend to be estimated at all.
_MINIMUM_HISTORY = 2

_RATIO_UNITS = (Unit.PERCENT,)


@dataclass(frozen=True)
class Point:
    """One reported figure, with the document and line it was read from.

    The citation travels with the figure all the way into the forecast. A
    number in a workbook is only checkable if the reader can open the document
    it came from and see the same row, so the reference is carried rather than
    summarised away.
    """

    period_end: date
    value: float
    document: str
    line: int
    quote: str

    def cite(self) -> str:
        return f"{self.document}:{self.line}"


@dataclass(frozen=True)
class Series:
    """One metric's confirmed history, newest first."""

    metric: MetricSpec
    points: tuple[Point, ...]

    @property
    def latest(self) -> Point | None:
        return self.points[0] if self.points else None

    def a_year_before(self, when: date) -> Point | None:
        """The figure for the same period of the previous year."""
        target = when - _YEAR
        for point in self.points:
            if abs(point.period_end - target) <= _YEAR_SLACK:
                return point
        return None


@dataclass(frozen=True)
class HistoryForecast:
    """A figure carried forward from reported history, and how it was reached."""

    figure: Figure
    method: str
    reasoning: str
    sources: tuple[Point, ...]
    points_used: int


def series_from(metric: MetricSpec, observations) -> Series:
    """Build a confirmed series for one metric at the span it is reported in.

    Three of these companies report the target metric quarterly and Hays
    reports it once a year, so the span that matters differs by company and is
    taken from the longest run available.
    """
    wanted = _reporting_span(observations)
    seen: dict[tuple, Point] = {}
    for item in observations:
        found = item.observation
        if (
            item.trust != "confirmed"
            or found.period_end is None
            or found.period_kind is not wanted
        ):
            continue
        key = (found.period_end, round(found.value, 4))
        seen.setdefault(
            key,
            Point(
                period_end=found.period_end,
                value=found.value,
                document=found.evidence.document,
                line=found.evidence.line,
                quote=found.evidence.quote,
            ),
        )
    points = sorted(seen.values(), key=lambda point: point.period_end, reverse=True)
    return Series(metric=metric, points=tuple(points))


def _reporting_span(observations) -> PeriodKind:
    """The span this metric is most often confirmed at."""
    counts: dict[PeriodKind, int] = {}
    for item in observations:
        if item.trust == "confirmed" and item.observation.period_end is not None:
            kind = item.observation.period_kind
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return PeriodKind.UNKNOWN
    return max(counts, key=lambda kind: counts[kind])


def forecast(metric: MetricSpec, series: Series) -> HistoryForecast | None:
    """Carry a metric forward one period. None when history cannot support it."""
    latest = series.latest
    if latest is None:
        return None

    if metric.unit in _RATIO_UNITS:
        return _forecast_ratio(metric, series)
    return _forecast_flow(metric, series)


def _forecast_flow(metric: MetricSpec, series: Series) -> HistoryForecast:
    """Carry a money or per share figure forward by its recent growth."""
    last = series.points[0]
    step = _YEAR / 4 if _looks_quarterly(series) else _YEAR
    seasonal_base = series.a_year_before(last.period_end + step)
    growth, growth_sources = _median_growth(series)

    if seasonal_base is not None and growth is not None:
        value = seasonal_base.value * (1 + growth)
        return HistoryForecast(
            figure=Figure(value=round(value, 4), unit=metric.unit),
            method="seasonal growth",
            reasoning=(
                f"the same period a year earlier reported {seasonal_base.value:,.2f} "
                f"[{seasonal_base.cite()}] and recent periods have grown "
                f"{growth:+.1%} against their own prior year, so the base is "
                f"carried forward at that rate"
            ),
            sources=(seasonal_base, *growth_sources),
            points_used=len(series.points),
        )

    if growth is not None:
        value = last.value * (1 + growth)
        return HistoryForecast(
            figure=Figure(value=round(value, 4), unit=metric.unit),
            method="growth from last reported",
            reasoning=(
                f"no figure survives for the same period a year earlier, so the "
                f"last reported {last.value:,.2f} [{last.cite()}] is grown at "
                f"{growth:+.1%}"
            ),
            sources=(last, *growth_sources),
            points_used=len(series.points),
        )

    return HistoryForecast(
        figure=Figure(value=round(last.value, 4), unit=metric.unit),
        method="last reported",
        reasoning=(
            f"history holds too few confirmed periods to measure a trend, so the "
            f"last reported {last.value:,.2f} [{last.cite()}] is carried forward "
            f"unchanged"
        ),
        sources=(last,),
        points_used=len(series.points),
    )


def _forecast_ratio(metric: MetricSpec, series: Series) -> HistoryForecast:
    """Carry a percentage forward from its level, not by compounding it."""
    last = series.points[0]
    recent = series.points[:_RECENT]

    if len(recent) >= _MINIMUM_HISTORY:
        drift = statistics.median(
            later.value - earlier.value for later, earlier in zip(recent, recent[1:])
        )
        value = last.value + drift
        return HistoryForecast(
            figure=Figure(value=round(value, 4), unit=metric.unit),
            method="level with drift",
            reasoning=(
                f"a percentage does not compound, so the last reported "
                f"{last.value:,.2f} [{last.cite()}] is moved by {drift:+.2f} "
                f"points, the amount it has typically moved between recent periods"
            ),
            sources=tuple(recent),
            points_used=len(series.points),
        )

    return HistoryForecast(
        figure=Figure(value=round(last_value, 4), unit=metric.unit),
        method="last reported",
        reasoning=(
            f"only one confirmed period is available, so the last reported "
            f"{last.value:,.2f} [{last.cite()}] is carried forward unchanged"
        ),
        sources=(last,),
        points_used=len(series.points),
    )


def _median_growth(series: Series) -> tuple[float | None, tuple[Point, ...]]:
    """The rate recent periods grew against the same period a year earlier.

    The median is used rather than the mean so that one exceptional quarter,
    a pandemic or an acquisition, does not set the rate for the next one.
    """
    rates: list[float] = []
    used: list[Point] = []
    for point in series.points[:_RECENT]:
        earlier = series.a_year_before(point.period_end)
        if earlier and earlier.value != 0:
            rates.append(point.value / earlier.value - 1)
            used.extend((point, earlier))
    if len(rates) < _MINIMUM_HISTORY:
        return None, ()
    return statistics.median(rates), tuple(used)


def _looks_quarterly(series: Series) -> bool:
    """Whether the reported periods sit a quarter apart."""
    if len(series.points) < 2:
        return False
    gap = series.points[0].period_end - series.points[1].period_end
    return gap < timedelta(days=200)
