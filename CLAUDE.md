# Agents vs Wall Street

## What this repository does
One command produces four OpenStocks workbooks containing twelve
forecast figures, one workbook per company:

  HD  FY2026Q2 - net sales USDm, adjusted diluted EPS,
                 comparable sales total company %
  ADI FY2026Q3 - revenue USDm, adjusted diluted EPS,
                 adjusted gross margin %
  HAS FY2026   - net fees GBPm, pre-exceptional basic EPS in pence,
                 pre-exceptional operating profit GBPm
  DE  FY2026Q3 - worldwide net sales and revenues USDm,
                 diluted EPS GAAP, Production and Precision Ag
                 operating profit USDm

Percentage metrics are written as plain numbers: 4.5 means 4.5 per cent.
Hays EPS is written in pence: 6.2 means 6.2 pence.
Workbooks are uploaded to OpenStocks manually. This system never
uploads anything programmatically.

## Environment
Python 3.12 managed by poetry, virtualenv in .venv inside the project.
Never create another virtualenv and never install packages outside
poetry. Run everything as `poetry run python ...`.
Node is present only to run the organisers' submission checks.

## Engineering principles

Prefer the smallest change that solves the problem. Extend existing
code before writing new code. Write a new module only when the
responsibility genuinely does not belong to an existing one.

Reuse before duplication. If similar logic already exists, call it or
generalise it rather than copying it.

Separate data from logic. Cell addresses, units, metric labels,
plausible ranges and source descriptions live in config/ as data.
None of these values are hardcoded anywhere in src/.

Clean layering. Dependencies point downwards only:

  src/domain/     domain types, units, enums, invariants. Depends on nothing.
  src/sources/    access to documents and external data
  src/extract/    pulling figures out of documents with unit and citation
  src/model/      financial reasoning and metric calculation
  src/validate/   checks on units, periods, outliers, conflicts
  src/workbook/   writing values into xlsx according to the spec
  src/run/        orchestration, caching, logging, fallbacks

src/model must not know that the output is a spreadsheet.
src/workbook must not know where a value came from.
Each layer must be testable without its neighbours.

Judge module size by responsibility, not by line count. Do not put
unrelated concerns in one file, and do not create a separate file for
every small class. Group by what changes together.

Write code as an experienced engineer would: readable, obvious control
flow, explicit failure handling, no cleverness that needs explaining.

## Units are typed, not conventional
src/domain/domain_values.py defines the enums: Unit, Company, MetricId,
Basis, FiscalPeriod. A figure always carries its unit inside the system.
Bare floats are not passed between layers above src/extract.
The workbook writer raises if a value's unit does not match the unit
declared in the spec for that cell. Unit mismatch is the single most
expensive failure mode in this challenge.

## Every figure is traceable
No number reaches a workbook without a recorded evidence chain: the
source document, the location within it, the quoted figure and the
date. A figure with no evidence chain uses the fallback path instead
and this is logged.

## Comment conventions

Comments describe the current state of the code. They never describe
history. Do not write "previously we did X", "changed to Y", "removed
because", "as requested", "new approach" or anything else that reads
as a changelog. Version history belongs in git, not in comments.

Comments must be understandable to a reader starting from zero, with
no knowledge of our conversation, no memory of earlier decisions and
no context beyond the file in front of them.

Every financial decision carries a comment explaining the economic
reasoning, not the mechanics of the code.
  bad:  multiply by 4
  good: Hays reports fees for the full financial year, so the interim
        figure is annualised before it is compared with the prior year

Abbreviations are allowed only where they are standard in the industry,
and are expanded on first use in a file.

Do not use em dashes or arrows. Use a plain hyphen.
Do not use capitals for emphasis.

When code changes, verify that the comments above it still describe
the truth. A stale comment is worse than no comment.

## Language
The entire repository is in English: code, comments, logs, README and
architecture/index.html.

## Logging
One log file per run at logs/run-YYYYMMDD-HHMMSS.log.
Line format: "ISO timestamp | error_level | error_message" where
error_level is INFO, WARNING or ERROR.
Never log API keys or file paths containing a user home directory.

## Reproducibility
forecast.py is the single entry point and processes all four companies.
Each company is processed independently and caches its result under
artifacts/, so a failure on the third company does not lose the first
two. Model names, temperature and seeds are set explicitly and recorded.
Every metric has a deterministic fallback that requires no model call.
A missing figure scores the maximum penalty, so a cell is never left
empty, even if the value is weak.

## Competition rules that constrain the code
Nothing in this repository may be built from work prepared before
16 August 2026 for this challenge. Secrets never enter the repository,
the logs, the architecture page or the entry record. Commit often, so
the history shows when the work was done.