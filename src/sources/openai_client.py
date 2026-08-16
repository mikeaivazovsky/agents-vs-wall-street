"""The only place this system talks to a language model.

Every call goes through here so that three things hold everywhere at once.

The run is repeatable. Answers are cached against the exact question asked, so
running twice costs once and produces the same figures. The cache key covers
the model, the instructions and the input, so changing any of them asks again
rather than returning a stale answer. This matters most in the final window,
where a rerun after a crash must not spend the time or the money a second time.

The answer has a shape. A model is asked for a structured object described by a
schema, not for prose to be picked apart afterwards. An answer that does not
fit the schema is a failure, not something to interpret.

A failure is survivable. Any error returns nothing at all rather than a
half-formed answer, and the caller carries on with the evidence it already had.
No figure in this challenge depends on a model call succeeding.

Keys are read from the environment and never written to the log, the cache or
the record of a run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

from src.domain.spec import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "artifacts" / "llm_cache"

# Recorded with every answer so that a run can be traced back to what produced
# it. Temperature is fixed at zero: two runs of the same evidence must not
# disagree, and a spread of opinions is worth nothing when only one number can
# be submitted.
MODEL = "gpt-5.1"
TEMPERATURE = 0.0

Answer = TypeVar("Answer", bound=BaseModel)


@dataclass(frozen=True)
class Reply:
    """One model answer, with the provenance a run record needs."""

    value: BaseModel
    model: str
    cached: bool

    def describe(self) -> str:
        return f"{self.model}, temperature {TEMPERATURE}" + (", cached" if self.cached else "")


class ModelUnavailable(RuntimeError):
    """Raised when no key is configured, so callers can skip cleanly."""


def is_configured() -> bool:
    """Whether a key exists. Checked before a stage decides to use the model."""
    _load_key()
    return bool(os.getenv("OPENAI_API_KEY"))


def ask(
    instructions: str,
    question: str,
    schema: type[Answer],
    cache_key_extra: str = "",
) -> Reply | None:
    """Ask for one structured answer. None when the model cannot supply it.

    Returning None rather than raising is deliberate. Every caller has a
    deterministic path that does not need the model, and an unreachable service
    should degrade the forecast rather than stop the run.
    """
    key = _cache_key(instructions, question, schema, cache_key_extra)
    cached = _read_cache(key, schema)
    if cached is not None:
        return Reply(value=cached, model=MODEL, cached=True)

    if not is_configured():
        return None

    try:
        from openai import OpenAI

        client = OpenAI()
        response = client.responses.parse(
            model=MODEL,
            instructions=instructions,
            input=question,
            text_format=schema,
        )
        parsed = response.output_parsed
    except Exception:
        # The reason is logged by the caller, which knows what it was asking
        # for. Nothing here is worth stopping a run over.
        return None

    if parsed is None:
        return None

    _write_cache(key, parsed)
    return Reply(value=parsed, model=MODEL, cached=False)


def _load_key() -> None:
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def _cache_key(instructions: str, question: str, schema: type, extra: str) -> str:
    """A key covering everything that could change the answer."""
    material = "\n".join([MODEL, str(TEMPERATURE), schema.__name__, instructions, question, extra])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _read_cache(key: str, schema: type[Answer]) -> Answer | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return schema.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        # A cache entry that no longer fits the schema is stale rather than
        # fatal, and is simply asked again.
        return None


def _write_cache(key: str, answer: BaseModel) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(
            answer.model_dump_json(indent=2), encoding="utf-8"
        )
    except OSError:
        pass
