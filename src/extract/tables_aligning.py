"""Turn the markdown tables already present in a document into an even grid.

The corpus was converted from published documents, and the conversion left the
tables ragged. A currency symbol sits in a cell of its own, spacing cells are
empty, and a data row can be twice as wide as the header above it:

    | in millions, except per share data | May 3,2026 | | May 4,2025 | | % Change |
    | Net sales | $ | 41,765 | | | $ | 39,856 | | | 4.8 | % |

Six cells against eleven. Reading a value by column position fails on that.
Dropping the empty cells and the lone currency symbols leaves four against
four, and the columns line up again:

    [in millions, except per share data] [May 3,2026] [May 4,2025] [% Change]
    [Net sales]                          [41,765]     [39,856]     [4.8]

That is all this module does. It knows what a table looks like and nothing
about what any of the numbers mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Characters used only for spacing. Deere pads its tables with zero width
# spaces, which python does not count as whitespace, so a padding cell would
# survive and push every later column out of place.
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00a0\u2060]")

# Cells that carry no information once a row is read as a list of values. A
# currency symbol alone in a cell belongs to the number in the next one, and a
# per cent sign alone belongs to the number in the previous one.
_NOISE_CELL = re.compile(r"^[$£€%\s]*$")

# A row of dashes separating a markdown header from its body.
_SEPARATOR_ROW = re.compile(r"^[\s|:-]+$")

# The scale is stated either in words, "in millions", or as a currency
# shorthand printed beside a year, as Hays does with "2019<br>£m".
_SCALE_HINT = re.compile(
    r"\bin\s+(thousands|millions|billions)\b"
    r"|\b(thousands|millions|billions)\s+of\b"
    r"|[$£€]\s?(m|bn|k)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Row:
    """One aligned table row, with the line it came from."""

    cells: tuple[str, ...]
    line_number: int
    raw: str

    @property
    def label(self) -> str:
        return self.cells[0] if self.cells else ""


@dataclass(frozen=True)
class Table:
    """One markdown table from a document, with its surrounding context."""

    rows: tuple[Row, ...]
    caption: str
    start_line: int
    scale_hint: str
    # Several lines of ordinary text above the table. A statement of earnings
    # states its scale and the period it covers in the paragraph introducing
    # it as often as inside the table, so both places are kept.
    context: str = ""

    def rows_matching(self, predicate) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if predicate(row.label))


def compact(raw_row: str) -> tuple[str, ...]:
    """Split a markdown row into cells and drop the ones carrying no value.

    A lone per cent sign is joined onto the number before it rather than
    thrown away. It is the only mark that tells a percentage apart from a
    money amount when both appear in the same row, as they do in a gross
    margin line that prints the profit and the margin side by side.
    """
    stripped = raw_row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    kept: list[str] = []
    for cell in (_INVISIBLE.sub("", part).strip() for part in stripped.split("|")):
        if cell == "%" and kept:
            kept[-1] = f"{kept[-1]}%"
            continue
        if _NOISE_CELL.match(cell):
            continue
        kept.append(cell)
    return tuple(kept)


def tables(text: str) -> tuple[Table, ...]:
    """Every markdown table in a document, in the order they appear.

    A caption is the nearest non-empty line of ordinary text above the table.
    It is what separates a statement of earnings from a table of executive pay
    that happens to mention the same words.
    """
    lines = text.splitlines()
    found: list[Table] = []
    index = 0

    while index < len(lines):
        if not lines[index].strip().startswith("|"):
            index += 1
            continue

        start = index
        block: list[Row] = []
        while index < len(lines) and lines[index].strip().startswith("|"):
            raw = lines[index]
            if not _SEPARATOR_ROW.match(raw.strip()):
                cells = compact(raw)
                if cells:
                    block.append(Row(cells=cells, line_number=index + 1, raw=raw.strip()))
            index += 1

        if block:
            context = _context_above(lines, start)
            found.append(
                Table(
                    rows=tuple(block),
                    caption=_caption_above(lines, start),
                    start_line=start + 1,
                    scale_hint=_scale_for(block, context),
                    context=context,
                )
            )

    return tuple(found)


def _caption_above(lines: list[str], start: int) -> str:
    """The nearest line of ordinary text above a table.

    Up to six lines are searched. Beyond that the text usually belongs to
    whatever came before the table rather than describing it.
    """
    for offset in range(1, 7):
        position = start - offset
        if position < 0:
            break
        candidate = lines[position].strip().lstrip("#").strip()
        if candidate and not candidate.startswith("|"):
            return candidate
    return ""


def _context_above(lines: list[str], start: int, depth: int = 10) -> str:
    """The ordinary text shortly above a table, oldest line first.

    A statement often names its scale and its period in the sentence that
    introduces the table rather than inside it, and a converted slide puts the
    heading of the chart there. Reading a short run of lines recovers both
    without pulling in the previous section.
    """
    collected: list[str] = []
    position = start - 1
    while position >= 0 and len(collected) < depth:
        candidate = lines[position].strip().lstrip("#").strip()
        if candidate and not candidate.startswith("|"):
            collected.append(candidate)
        position -= 1
    return " ".join(reversed(collected))


def _scale_for(rows: list[Row], context: str) -> str:
    """The phrase stating the scale of the table's figures, if it has one.

    The scale is stated either inside the table, usually in the first cell of
    the header row as "in millions, except per share data", or on the line
    above it as "(In thousands)". Both places have to be read: the same
    company states revenue in thousands in one document and in millions in
    another, and reading the wrong one is a thousandfold error.
    """
    for row in rows:
        for cell in row.cells:
            if _SCALE_HINT.search(cell):
                return cell
    match = _SCALE_HINT.search(context)
    if match:
        # Only the phrase itself is kept. The surrounding sentence would drag
        # unrelated words into the record of where the scale came from.
        start = max(0, match.start() - 20)
        return context[start:match.end() + 10].strip()
    return ""
