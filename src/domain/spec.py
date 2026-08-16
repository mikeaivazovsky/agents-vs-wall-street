"""What to produce, and where to put it.

Two files describe the task and neither repeats the other.

challenge/companies.json is the organisers' definition and the authority for
companies, tickers, periods, output file names, metric labels and units. It is
read as supplied and never edited.

config/challenge_spec.yaml adds only what the organisers state inside the
workbooks rather than in that file: the sheet and the cell address.

The two are joined on ticker and label. Anything present on one side and
missing from the other stops the run, because a silent gap would mean a figure
with no cell to go in, or a cell no figure is destined for.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.domain_values import (
    Basis,
    Company,
    FiscalPeriod,
    MetricId,
    Unit,
    basis_for,
    metric_id_for,
)

# src/domain/spec.py sits two directories below the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

COMPANIES_PATH = PROJECT_ROOT / "challenge" / "companies.json"
DEFAULT_SPEC_PATH = PROJECT_ROOT / "config" / "challenge_spec.yaml"
TEMPLATE_DIR = PROJECT_ROOT / "challenge" / "templates"
SUBMISSION_DIR = PROJECT_ROOT / "submission"

_CELL_PATTERN = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")


class SpecError(ValueError):
    """Raised when the task definition is missing, malformed or inconsistent."""


class CellSpec(BaseModel):
    """One entry of config/challenge_spec.yaml.

    Holds a cell position and identifies the metric it belongs to by the same
    ticker and label the organisers use.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: Company
    label: str = Field(min_length=1)
    sheet: str = Field(min_length=1)
    cell: str

    @field_validator("cell")
    @classmethod
    def _check_cell(cls, value: str) -> str:
        if not _CELL_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a cell address such as C7")
        return value

    @property
    def key(self) -> tuple[Company, str]:
        return (self.ticker, self.label)


class MetricSpec(BaseModel):
    """One required figure: what it is, and which cell it belongs in.

    Built by joining the organisers' definition to a cell entry. Nothing
    constructs one by hand.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: MetricId
    company: Company
    period: FiscalPeriod
    label: str
    unit: Unit
    basis: Basis
    workbook: str
    sheet: str
    cell: str

    @property
    def template_path(self) -> Path:
        """The organisers' untouched template for this metric's workbook."""
        return TEMPLATE_DIR / self.workbook

    @property
    def output_path(self) -> Path:
        """Where the completed workbook is written."""
        return SUBMISSION_DIR / self.workbook


class ChallengeSpec(BaseModel):
    """The full set of figures the final run must produce."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: tuple[MetricSpec, ...]

    @model_validator(mode="after")
    def _check_unique(self) -> "ChallengeSpec":
        ids = [metric.id for metric in self.metrics]
        if len(set(ids)) != len(ids):
            raise ValueError("metric ids must be unique")

        cells = [(metric.workbook, metric.sheet, metric.cell) for metric in self.metrics]
        if len(set(cells)) != len(cells):
            raise ValueError("two metrics target the same cell")
        return self

    def companies(self) -> tuple[Company, ...]:
        """The companies in the order the organisers list them."""
        seen: list[Company] = []
        for metric in self.metrics:
            if metric.company not in seen:
                seen.append(metric.company)
        return tuple(seen)

    def for_company(self, company: Company) -> tuple[MetricSpec, ...]:
        return tuple(metric for metric in self.metrics if metric.company is company)

    def output_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for metric in self.metrics:
            if metric.output_path not in paths:
                paths.append(metric.output_path)
        return tuple(paths)


def _read_companies(path: Path) -> list[dict]:
    """Load the organisers' definition exactly as supplied."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SpecError(
            f"cannot read the organisers' definition at {path.name}: {error.strerror}"
        ) from error

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SpecError(f"{path.name} is not valid json: {error}") from error

    companies = parsed.get("companies") if isinstance(parsed, dict) else None
    if not isinstance(companies, list) or not companies:
        raise SpecError(f"{path.name} has no companies list")
    return companies


def _read_cell_specs(path: Path) -> tuple[CellSpec, ...]:
    """Load our own cell positions."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SpecError(f"cannot read {path.name}: {error.strerror}") from error

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise SpecError(f"{path.name} is not valid yaml: {error}") from error

    if not isinstance(parsed, dict) or not isinstance(parsed.get("metrics"), list):
        raise SpecError(f"{path.name} must be a mapping with a metrics list")

    entries: list[CellSpec] = []
    for index, item in enumerate(parsed["metrics"], start=1):
        try:
            entries.append(CellSpec.model_validate(item))
        except Exception as error:
            raise SpecError(f"{path.name} entry {index} is not valid: {error}") from error

    seen: set[tuple[Company, str]] = set()
    for entry in entries:
        if entry.key in seen:
            raise SpecError(
                f"{path.name} has two entries for {entry.ticker.value} {entry.label!r}"
            )
        seen.add(entry.key)
    return tuple(entries)


def load_spec(
    companies_path: Path | None = None,
    spec_path: Path | None = None,
) -> ChallengeSpec:
    """Join the organisers' definition to our cell positions.

    Every mismatch found is reported together rather than one at a time, so a
    single run shows the whole picture instead of revealing one problem per
    attempt.
    """
    companies_file = companies_path or COMPANIES_PATH
    cells_file = spec_path or DEFAULT_SPEC_PATH

    companies = _read_companies(companies_file)
    cell_specs = _read_cell_specs(cells_file)
    unmatched = {entry.key: entry for entry in cell_specs}

    metrics: list[MetricSpec] = []
    problems: list[str] = []

    for company_entry in companies:
        try:
            company = Company(company_entry["ticker"])
            period = FiscalPeriod(company_entry["period"])
            workbook = company_entry["outputFile"]
            declared = company_entry["metrics"]
        except KeyError as error:
            problems.append(f"{companies_file.name} entry is missing {error}")
            continue
        except ValueError as error:
            problems.append(f"{companies_file.name}: {error}")
            continue

        for metric_entry in declared:
            label = metric_entry["label"]
            key = (company, label)
            cell_spec = unmatched.pop(key, None)

            if cell_spec is None:
                problems.append(
                    f"{cells_file.name} has no entry for {company.value} {label!r}"
                )
                continue

            try:
                metrics.append(
                    MetricSpec(
                        id=metric_id_for(company, label),
                        company=company,
                        period=period,
                        label=label,
                        unit=Unit(metric_entry["units"]),
                        basis=basis_for(label),
                        workbook=workbook,
                        sheet=cell_spec.sheet,
                        cell=cell_spec.cell,
                    )
                )
            except ValueError as error:
                problems.append(f"{company.value} {label!r}: {error}")

    for key in unmatched:
        company, label = key
        problems.append(
            f"{cells_file.name} describes {company.value} {label!r}, which "
            f"{companies_file.name} does not require"
        )

    if problems:
        raise SpecError(
            "the task definition and the cell specification disagree:\n  - "
            + "\n  - ".join(problems)
        )

    try:
        return ChallengeSpec(metrics=tuple(metrics))
    except Exception as error:
        raise SpecError(f"the task definition is not usable: {error}") from error


@lru_cache(maxsize=1)
def default_spec() -> ChallengeSpec:
    """The task as defined by the two standard files, parsed once per process."""
    return load_spec()
