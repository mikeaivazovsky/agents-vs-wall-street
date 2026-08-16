"""One log file per run.

Every line has the shape:

    ISO timestamp | error_level | error_message

where error_level is INFO, WARNING or ERROR. A traceback is written as several
lines, each carrying the same prefix, so that every line in the file can be
parsed the same way.

The log is part of the submitted evidence and is read by people who did not
watch the run, so it records what was written, where it went and how long it
took, in the order it happened.

Secrets and personal paths never reach the file. Values that look like keys are
masked and absolute paths are shown relative to the project root.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from types import TracebackType

from src.domain.domain_values import Company, Figure, MetricId
from src.domain.spec import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"

_LEVEL_NAMES = {logging.INFO: "INFO", logging.WARNING: "WARNING", logging.ERROR: "ERROR"}

# Names of environment variables whose values must never appear in the log.
_SECRET_NAME_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)

# Key shaped strings, caught even when they were never read from the
# environment, for example a key pasted into an error message by a library.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(
        r"(?i)\b(api[_\-]?key|access[_\-]?token|secret|password)\b\s*[:=]\s*\S+"
    ),
)

_REDACTED = "[redacted]"


def redact(message: str) -> str:
    """Remove secrets and home directory paths from a line of text."""
    for name, value in os.environ.items():
        # Short values are skipped: they are unlikely to be keys and masking a
        # common word would make the log harder to read than it is worth.
        if len(value) >= 8 and _SECRET_NAME_PATTERN.search(name):
            message = message.replace(value, _REDACTED)

    for pattern in _SECRET_VALUE_PATTERNS:
        message = pattern.sub(_REDACTED, message)

    home = str(Path.home())
    if home and home in message:
        message = message.replace(home, "~")
    return message


def relative_path(path: Path | str) -> str:
    """A path stated from the project root, so no home directory is exposed."""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return redact(str(candidate))


class _RunFormatter(logging.Formatter):
    """Formats one record as one or more prefixed lines."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="seconds"
        )
        level = _LEVEL_NAMES.get(record.levelno, record.levelname)
        prefix = f"{stamp} | {level} | "

        lines = [prefix + redact(record.getMessage())]
        if record.exc_info:
            traceback_text = self.formatException(record.exc_info)
            lines.extend(
                prefix + redact(line) for line in traceback_text.splitlines()
            )
        return "\n".join(lines)


class RunLogger:
    """The log for a single execution of the pipeline.

    Also records the wall clock duration of the run, which the summary line
    reports.
    """

    def __init__(self, log_dir: Path | None = None, echo: bool = True) -> None:
        started = datetime.now()
        directory = log_dir or LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)

        self._path = directory / f"{started:%Y%m%d-%H%M%S}.log"
        self._started_at = started
        self._monotonic_start = time.monotonic()

        self._logger = logging.getLogger(f"run.{started:%Y%m%d%H%M%S}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handlers: list[logging.Handler] = []

        file_handler = logging.FileHandler(self._path, encoding="utf-8")
        file_handler.setFormatter(_RunFormatter())
        self._add(file_handler)

        if echo:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(_RunFormatter())
            self._add(stream_handler)

        self.info(f"run started at {self._started_at.astimezone().isoformat(timespec='seconds')}")
        self.info(f"log file {relative_path(self._path)}")

    def _add(self, handler: logging.Handler) -> None:
        handler.setLevel(logging.INFO)
        self._logger.addHandler(handler)
        self._handlers.append(handler)

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is not None:
            self.exception("the run stopped on an unhandled error", exc)
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._monotonic_start

    def info(self, message: str) -> None:
        self._logger.info(message)

    def warning(self, message: str) -> None:
        self._logger.warning(message)

    def error(self, message: str) -> None:
        self._logger.error(message)

    def exception(self, message: str, error: BaseException) -> None:
        """Record a failure together with its traceback."""
        self._logger.error(
            f"{message}: {type(error).__name__}: {error}",
            exc_info=(type(error), error, error.__traceback__),
        )

    def metric_written(
        self,
        company: Company,
        metric_id: MetricId,
        figure: Figure,
        cell: str,
        workbook: str,
    ) -> None:
        self.info(
            f"{company.value} | {metric_id.value} | value {figure.value} "
            f"| unit {figure.unit.value} | cell {workbook}!{cell}"
        )

    def metric_failed(self, company: Company, metric_id: MetricId, reason: str) -> None:
        self.error(f"{company.value} | {metric_id.value} | not written | {reason}")

    def workbook_saved(self, path: Path) -> None:
        self.info(f"workbook saved | {relative_path(path)}")

    def retry(self, action: str, attempt: int, attempts: int, reason: str) -> None:
        self.warning(f"retry {attempt} of {attempts} | {action} | {reason}")

    def summary(self, written: int, failed: int) -> None:
        self.info(
            f"run finished in {self.elapsed_seconds:.1f}s "
            f"| metrics written {written} | metrics failed {failed}"
        )

    def close(self) -> None:
        for handler in self._handlers:
            handler.close()
            self._logger.removeHandler(handler)
        self._handlers.clear()
