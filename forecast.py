"""Entry point for the final run.

    poetry run python forecast.py

Processes all four companies and writes their workbooks to submission/. Exits
with a non-zero code if any of the four workbooks is missing at the end, so a
failed run cannot be mistaken for a successful one.
"""

from __future__ import annotations

import sys

from src.domain.spec import ChallengeSpec, SpecError, default_spec
from src.run.logging import RunLogger, relative_path
from src.run.pipeline import run

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_NO_SPEC = 2
EXIT_PLACEHOLDERS = 3


def missing_workbooks(spec: ChallengeSpec) -> list[str]:
    """Names of the workbooks the specification requires but cannot find.

    The check is made against the file on disk rather than against what the
    pipeline reported, because the upload needs a file that exists.
    """
    return [path.name for path in spec.output_paths() if not path.is_file()]


def main() -> int:
    try:
        spec = default_spec()
    except SpecError as error:
        # The logger is not started for this: without a specification there is
        # no run to record.
        print(f"cannot start: {error}", file=sys.stderr)
        return EXIT_NO_SPEC

    with RunLogger() as logger:
        result = run(logger=logger, spec=spec)

        missing = missing_workbooks(spec)
        for name in missing:
            logger.error(f"workbook missing from submission | {name}")

        if missing or not result.ok:
            logger.error(
                f"run incomplete | workbooks saved {len(result.saved)} of "
                f"{len(spec.output_paths())} | metrics failed {result.failed}"
            )
            exit_code = EXIT_INCOMPLETE
        elif not result.is_forecast:
            # The workbooks are structurally valid and the organisers' check
            # accepts them, so only the exit code separates this from a
            # finished run. Without it a run made entirely of placeholders
            # would report success and could be uploaded by mistake.
            logger.error(
                f"workbooks written but not forecast | {result.placeholders} "
                f"placeholder figures | do not upload"
            )
            exit_code = EXIT_PLACEHOLDERS
        else:
            logger.info(
                f"run complete | {len(result.saved)} workbooks ready for manual upload"
            )
            exit_code = EXIT_OK

        log_path = relative_path(logger.path)

    print(f"log: {log_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
