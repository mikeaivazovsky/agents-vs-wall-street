"""Catalogue of the historical document corpus.

Answers one question: which documents exist for a company, of what kind and
from what date. It reads only the yaml front matter at the top of each file and
never looks at the body, so listing the whole corpus stays cheap.

The mapping from a company to its folder is discovered by reading the ticker
out of a document in each folder rather than written down here, so a renamed
folder does not need a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from src.domain.domain_values import Company, DocumentType
from src.domain.spec import PROJECT_ROOT

CORPUS_DIR = PROJECT_ROOT / "challenge" / "offline-data"

# Document kinds that carry financial tables. Call transcripts contain no
# tables at all in this corpus, so they hold no reported figure that can be
# read from one.
TABULAR_KINDS = (DocumentType.FILING, DocumentType.SLIDE)

_FRONT_MATTER_FENCE = "---"


class CorpusError(RuntimeError):
    """Raised when the corpus is missing or cannot be catalogued."""


@dataclass(frozen=True)
class Document:
    """One document, described by its front matter alone."""

    path: Path
    company: Company
    published_at: date
    document_type: DocumentType
    declared_period: str | None

    @property
    def name(self) -> str:
        return self.path.name

    def relative_name(self) -> str:
        """Path from the corpus root, which is what a citation records."""
        try:
            return str(self.path.relative_to(CORPUS_DIR))
        except ValueError:
            return self.path.name

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="ignore")


def read_front_matter(path: Path) -> dict:
    """Parse the yaml block at the top of a document.

    Only the opening block is read. A document whose front matter is missing or
    unreadable returns an empty mapping and is skipped by the caller rather
    than stopping the run, because one malformed file should not cost us the
    other eleven hundred.
    """
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            if handle.readline().strip() != _FRONT_MATTER_FENCE:
                return {}
            lines: list[str] = []
            for line in handle:
                if line.strip() == _FRONT_MATTER_FENCE:
                    break
                lines.append(line)
    except OSError:
        return {}

    try:
        parsed = yaml.safe_load("".join(lines))
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_date(value: object) -> date | None:
    """Read a publication date from front matter.

    The corpus quotes its dates, so yaml hands them back as text rather than as
    dates, and both forms are accepted.
    """
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


@lru_cache(maxsize=1)
def _folder_by_company() -> dict[Company, Path]:
    """Work out which corpus folder belongs to which company.

    The ticker is read from the first document found in each folder. Every
    document in the corpus declares it, so no folder name has to be known in
    advance.
    """
    if not CORPUS_DIR.is_dir():
        raise CorpusError(f"the document corpus is missing at {CORPUS_DIR.name}")

    found: dict[Company, Path] = {}
    for folder in sorted(p for p in CORPUS_DIR.iterdir() if p.is_dir()):
        for document in sorted(folder.rglob("*.md")):
            ticker = read_front_matter(document).get("ticker")
            if not ticker:
                continue
            try:
                found.setdefault(Company(ticker), folder)
            except ValueError:
                # A company outside the challenge would be ignored rather than
                # treated as an error.
                pass
            break

    if not found:
        raise CorpusError("no company folder in the corpus declares a known ticker")
    return found


def folder_for(company: Company) -> Path:
    folders = _folder_by_company()
    if company not in folders:
        raise CorpusError(f"the corpus has no folder for {company.value}")
    return folders[company]


def documents(
    company: Company,
    kinds: tuple[DocumentType, ...] = TABULAR_KINDS,
    since: date | None = None,
) -> tuple[Document, ...]:
    """List a company's documents, newest first.

    Documents whose front matter cannot be read are left out. Their absence is
    visible in the count, which the caller logs.
    """
    wanted = {kind.value for kind in kinds}
    found: list[Document] = []

    for path in folder_for(company).rglob("*.md"):
        meta = read_front_matter(path)
        raw_type = meta.get("document_type")
        if raw_type not in wanted:
            continue

        published = _as_date(meta.get("published_at"))
        if published is None:
            continue
        if since is not None and published < since:
            continue

        found.append(
            Document(
                path=path,
                company=company,
                published_at=published,
                document_type=DocumentType(raw_type),
                declared_period=meta.get("period"),
            )
        )

    found.sort(key=lambda item: (item.published_at, item.path.name), reverse=True)
    return tuple(found)
