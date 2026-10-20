"""The three cut-offs and the eval switch are settings.

Written before the implementation exists. Surface under test:

``helpdesk.shared.core.config.Settings`` gains four fields:
    ``intent_confidence_min`` (env ``INTENT_CONFIDENCE_MIN``, default 0.7),
    ``role_suggestion_floor`` (``ROLE_SUGGESTION_FLOOR``, default 0.70),
    ``policy_refusal_floor`` (``POLICY_REFUSAL_FLOOR``, default 0.35), each a
    float that must lie in 0 to 1 inclusive, and ``eval_routes_enabled``
    (``EVAL_ROUTES_ENABLED``, default false).
``helpdesk.agent_api.agent.gates.needs_clarification_at_extraction`` and
    ``extraction_outcome`` take a keyword ``threshold`` that defaults to
    ``CONFIDENCE_THRESHOLD``.
``helpdesk.mcp_server.services.rag.role_suggestions.suggest`` takes a keyword
    ``floor`` that defaults to ``SUGGESTION_FLOOR``.
``helpdesk.mcp_server.services.rag.retrieval.should_refuse`` takes a keyword
    ``floor`` that defaults to ``REFUSAL_THRESHOLD``.
The old constants stay as the defaults, so their own tests are unchanged.

No backing service: settings load from the test's own env (``_env_file=None``)
and the three decisions are pure functions fed hand-made numbers.
"""

import math
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from helpdesk.agent_api.agent.gates import extraction_outcome, needs_clarification_at_extraction
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    TurnOutcome,
)
from helpdesk.mcp_server.services.rag.embedding import SparseVector
from helpdesk.mcp_server.services.rag.retrieval import SearchHit, should_refuse
from helpdesk.mcp_server.services.rag.role_suggestions import (
    RoleRow,
    TextVectorCache,
    suggest,
)
from helpdesk.shared.core.config import Settings

# Obviously fake, tests only.
REQUIRED_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://fake:fake@db.invalid:5432/fake",
    "REDIS_URL": "redis://redis.invalid:6379/0",
    "QDRANT_URL": "http://qdrant.invalid:6333",
    "JWT_SECRET": "cutoff-settings-test-jwt-secret-0123456789",
}

CUTOFF_NAMES: tuple[str, ...] = (
    "intent_confidence_min",
    "role_suggestion_floor",
    "policy_refusal_floor",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the four required names are set, so no earlier case's env leaks in."""
    for name in (*CUTOFF_NAMES, "eval_routes_enabled"):
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_the_cutoffs_default_to_the_planned_values() -> None:
    """Catches a default that drifted from the numbers the golden set was written
    against: intent 0.7, role suggestion 0.70, policy refusal 0.35, and the eval
    routes OFF (an eval route that defaults on would answer in production).

    Equalities, not ranges, so 0.69 or 0.36 fails.
    """
    settings = _settings()

    assert settings.intent_confidence_min == 0.7
    assert settings.role_suggestion_floor == 0.70
    assert settings.policy_refusal_floor == 0.35
    assert settings.eval_routes_enabled is False


@pytest.mark.parametrize("name", CUTOFF_NAMES)
@pytest.mark.parametrize("value", [0.0, 1.0])
def test_a_cutoff_at_zero_and_one_is_accepted(name: str, value: float) -> None:
    """Catches an exclusive range check (`0 < x < 1`): the limits themselves are
    valid, since a cut-off of 0 means "never" and 1 means "only certain".
    """
    assert getattr(_settings(**{name: value}), name) == value


@pytest.mark.parametrize("name", CUTOFF_NAMES)
@pytest.mark.parametrize("value", [-0.01, 1.01, -1.0, 2.0, math.inf, -math.inf, math.nan])
def test_a_cutoff_outside_zero_to_one_is_refused(name: str, value: float) -> None:
    """Catches a missing or one-sided range check: -0.01 and 1.01 are one hundredth
    outside each limit, so a check against 0 only, or 1 only, passes one of
    the two. A refusal must name the field, so a mistyped env value is found.

    Infinity and NaN are in the table because every comparison with NaN is
    false, which lets it through a check written as `not (x < 0 or x > 1)`.
    """
    with pytest.raises(ValidationError) as excinfo:
        _settings(**{name: value})

    assert name in {str(error["loc"][0]) for error in excinfo.value.errors()}


@pytest.mark.parametrize("name", CUTOFF_NAMES)
def test_a_cutoff_from_the_environment_is_checked_too(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Catches a range check that only runs on keyword values: the app loads
    these from env, so `INTENT_CONFIDENCE_MIN=1.01` must fail at startup."""
    monkeypatch.setenv(name.upper(), "1.01")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv(name.upper(), "0.5")
    assert getattr(Settings(_env_file=None), name) == 0.5  # type: ignore[call-arg]


# --- a tuned cut-off changes the decision --------------------------------------


class _Embedder:
    """One query vector and one role vector, so the cosine is the case's own."""

    def __init__(self, role_text: str, role_vector: tuple[float, float]) -> None:
        self._vectors = {"fake query": (1.0, 0.0), role_text: role_vector}

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vectors[text] for text in texts]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        raise AssertionError("suggestions never use the sparse model")

    def count_tokens(self, text: str) -> int:
        raise AssertionError("suggestions never count tokens")


def _extraction(confidence: float) -> IntentExtraction:
    return IntentExtraction(
        intent=Intent.REQUEST_ACCESS,
        entities=IntentEntities(
            system="wiki",
            role=None,
            target_user=None,
            department=None,
            expiry_days=None,
            justification=None,
        ),
        confidence=confidence,
        clarifying_question=None,
        second_request=None,
    )


def _page(dense_score: float) -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=uuid.UUID(int=1),
            document_id=uuid.UUID(int=2),
            document_slug="fake-policy",
            version="v1",
            heading_path=("1",),
            text="Fake passage.",
            score=0.03,
            dense_score=dense_score,
        )
    ]


def test_a_tuned_cutoff_changes_the_decision() -> None:
    """Catches a cut-off that is a setting in name only: each function must
    read the number it is handed, not its old constant.

    Each row sits between the two values: confidence 0.65 is below 0.7 and above
    0.6; a suggestion scored 0.66 is below 0.70 and above 0.65; a best cosine
    of 0.30 is below 0.35 and above 0.25. Handing in the tuned value flips the
    answer, and omitting it keeps the old constant, so a hard-coded constant
    fails the first assertion of each pair and an ignored default fails the
    last. The tuned numbers come out of `Settings`, the way the app passes them.
    """
    # Intent: 0.65 asks at the default 0.7 and goes on at a tuned 0.6.
    extraction = _extraction(0.65)
    tuned_intent = _settings(intent_confidence_min=0.6).intent_confidence_min
    assert needs_clarification_at_extraction(extraction) is True
    assert needs_clarification_at_extraction(extraction, threshold=0.7) is True
    assert needs_clarification_at_extraction(extraction, threshold=tuned_intent) is False
    assert extraction_outcome(extraction, False, threshold=0.7) is TurnOutcome.CLARIFY
    assert extraction_outcome(extraction, False, threshold=tuned_intent) is TurnOutcome.CONTINUE

    # Role suggestion: cosine 0.66 shows at a floor of 0.65 only.
    row = RoleRow(system_slug="wiki", system_name="Wiki", role_slug="viewer", role_name="Viewer")
    embedder = _Embedder("Wiki Viewer", (0.66, math.sqrt(1 - 0.66**2)))
    tuned_floor = _settings(role_suggestion_floor=0.65).role_suggestion_floor
    assert suggest(embedder, TextVectorCache(), "fake query", [row]) == []
    assert suggest(embedder, TextVectorCache(), "fake query", [row], floor=0.70) == []
    shown = suggest(embedder, TextVectorCache(), "fake query", [row], floor=tuned_floor)
    assert [item.row.role_slug for item in shown] == ["viewer"]

    # Policy refusal: best cosine 0.30 refuses at 0.35 and answers at 0.25.
    tuned_refusal = _settings(policy_refusal_floor=0.25).policy_refusal_floor
    assert should_refuse(_page(0.30)) is True
    assert should_refuse(_page(0.30), floor=0.35) is True
    assert should_refuse(_page(0.30), floor=tuned_refusal) is False
