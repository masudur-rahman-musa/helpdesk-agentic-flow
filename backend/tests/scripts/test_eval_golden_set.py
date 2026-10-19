"""Validator for the eval data.

Guards two repo-data files, both read-only here:

    project-evals/golden/helpdesk-v1.jsonl
    project-evals/labels/helpdesk-judge-calibration.jsonl

Every named value is cross-referenced against the code that OWNS it rather
than against a literal this file repeats: tenant slugs come from
``scripts.seed.NORDLICHT``/``CUAN``, intents from
``agent_api.graph.state.Intent``, decisions and risk classes from
``helpdesk.policy.types``, tool names from ``helpdesk.auth.rbac.Tool``, and the
decision/risk_class of every access case from ``helpdesk.policy.engine.classify``
itself.

Cost: no backing service. No Postgres, no Redis, no Qdrant, no gateway, no
network. The slowest thing here is ``generate_tenant`` for each of the two
tenants, run once each in a module-scoped fixture.

``helpdesk.agent_api`` is a subpackage of the one ``backend`` project, so it
imports with no ``sys.path`` surgery; ``scripts.seed`` imports because
``backend/pyproject.toml``'s pytest ``pythonpath`` puts the repo root on
``sys.path`` before any test module is imported.

Claims pinned here, each by the test named beside it:
- the golden file holds exactly 140 cases, one per line, no blank line:
  ``test_the_golden_file_holds_one_hundred_and_forty_parsable_cases``;
- the twelve category counts are exactly the category minimums:
  ``test_category_counts_equal_the_spec_minimums_exactly``;
- each access case agrees with the real policy engine on decision and risk
  class: ``test_access_cases_agree_with_the_policy_engine``;
- the labels are 40, eight at each score of 1 to 5:
  ``test_the_labels_file_holds_forty_labels_eight_at_each_score``.

Basename check: ``test_eval_golden_set.py`` is unique across
every test tree in the repo (``packages/helpdesk/tests``,
``services/*/tests``), so pytest's basename import cannot collide.
"""

import json
import re
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from helpdesk.agent_api.agent.state import Intent
from helpdesk.shared.auth import rbac
from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.risk import NEW_JOINER_TENURE_DAYS
from helpdesk.shared.policy.types import (
    Decision,
    Privilege,
    RequestSnapshot,
    RiskClass,
    RoleSnapshot,
    Sensitivity,
    SodRuleSnapshot,
    SystemCategory,
    TargetSnapshot,
    TargetStatus,
)
from scripts.seed import (
    _HIRING_EPOCH,
    _PRIVILEGE_EXPIRY_DAYS,
    CUAN,
    NORDLICHT,
    TenantRows,
    TenantSpec,
    UserRow,
    _uid,
    generate_tenant,
)


def _repo_root() -> Path:
    """Walk up from this file to the directory that holds the git checkout."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


GOLDEN = _repo_root() / "backend/evals/golden/helpdesk-v1.jsonl"
LABELS = _repo_root() / "backend/evals/labels/helpdesk-judge-calibration.jsonl"
BASELINE = _repo_root() / "backend/evals/baseline.json"

GOLDEN_SET_POINTER = "backend/evals/golden/helpdesk-v1.jsonl"
"""What ``baseline.json`` must name as its golden set."""

# The category minimums. They sum to exactly 140, so the minimums ARE
# the count and this mapping is asserted whole.
CATEGORY_MINIMUMS: dict[str, int] = {
    "access_auto": 16,
    "access_approval": 20,
    "access_blocked_sod": 10,
    "access_critical": 10,
    "helpdesk": 35,
    "policy_qa": 19,
    "refusal_out_of_corpus": 10,
    "clarify_ambiguous": 10,
    "injection": 8,
    "cross_tenant": 5,
    "lifecycle": 4,
    "review": 3,
}

TOTAL_CASES = 150
TOTAL_LABELS = 40
LABELS_PER_SCORE = 8
SCORES: tuple[int, int, int, int, int] = (1, 2, 3, 4, 5)
DISTINCT_CASE_IDS_MIN = 20
MIN_CONTENT_CHARS = 10
RUBRIC_MAX_WORDS = 40

CASE_ID = re.compile(r"^[a-z]{3,4}-[0-9]{3}$")
PRINCIPAL_USER = re.compile(r"^seed:[a-z]+\.[a-z_]+\.[0-9]{1,3}$")

MESSAGE_ROLE = "user"
SENTENCE_MARKS: tuple[str, str, str] = (".", "!", "?")

# The 10 corpus document slugs, the same list `test_policy_corpus.py` names
# (same comment discipline: the spec's document number beside each slug).
SLUGS: tuple[str, ...] = (
    "access-control-policy",  # 1. Access Control Policy
    "segregation-of-duties",  # 2. Segregation of Duties Standard
    "joiner-mover-leaver",  # 3. Joiner, Mover, Leaver Procedure
    "privileged-access",  # 4. Privileged Access Standard
    "access-review",  # 5. Access Review Procedure
    "acceptable-use",  # 6. Acceptable Use Policy
    "password-account-recovery",  # 7. Password and Account Recovery Procedure
    "physical-access-badge",  # 8. Physical Access (badge) Policy
    "third-party-access",  # 9. Third-party and Contractor Access Policy
    "data-classification",  # 10. Data Classification Standard
)

EMAIL_DOMAINS: tuple[str, str] = ("@nordlicht.example", "@cuan.example")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

ACCESS_CATEGORIES: tuple[str, str, str, str] = (
    "access_auto",
    "access_approval",
    "access_blocked_sod",
    "access_critical",
)
ACCESS_CASE_COUNT = 56
"""The four access categories sum to 55 of the 140 cases."""

# `create_access_request` is the registry's wire literal for
# `rbac.Tool.CREATE_ACCESS_REQUEST_SELF` (in the MCP server's
# registry, on its ToolSpec): the permission matrix splits one wire
# tool into a self row and an on-behalf row, so the wire name is not a member
# value and has to be added by hand.
TOOL_NAMES: frozenset[str] = frozenset(
    {tool.value for tool in rbac.Tool} | {"create_access_request"}
)

INTENT_VALUES: frozenset[str] = frozenset(intent.value for intent in Intent)
DECISION_VALUES: frozenset[str] = frozenset(decision.value for decision in Decision)
RISK_CLASS_VALUES: frozenset[str] = frozenset(risk.value for risk in RiskClass)

_UNREADABLE = "<golden-file-unreadable>"
"""Stand-in parameter id used when the golden file cannot be read at collection
time; the body then fails through the ``golden_cases`` fixture, which names the
missing path."""


# --- models ------------------------------------------------------------------


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str

    @field_validator("role")
    @classmethod
    def _role_is_user(cls, value: str) -> str:
        if value != MESSAGE_ROLE:
            raise ValueError(f"message role {value!r} is not {MESSAGE_ROLE!r}")
        return value

    @field_validator("content")
    @classmethod
    def _content_is_long_enough(cls, value: str) -> str:
        if len(value) < MIN_CONTENT_CHARS:
            raise ValueError(
                f"message content is {len(value)} chars, minimum is {MIN_CONTENT_CHARS}"
            )
        return value


class ToolCall(BaseModel):
    """One expected tool call. `name` is checked by `_check_tool_names`, not
    here, so the file-level check stays a real check rather than a restatement
    of what parsing already proved."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, str]


class Expected(BaseModel):
    """What the case pins about the answer.

    `intent`, `decision` and `risk_class` are plain strings here and are
    cross-referenced against the owning enums by
    `test_every_intent_is_an_intent_member` and
    `test_every_decision_and_risk_class_is_an_engine_member`.
    """

    model_config = ConfigDict(extra="forbid")

    intent: str
    should_refuse: bool
    should_clarify: bool
    tool_calls: list[ToolCall] | None = None
    decision: str | None = None
    risk_class: str | None = None
    citation_documents: list[str] | None = None
    forbidden_phrases: list[str] | None = None


class GoldenCase(BaseModel):
    """One line of the golden set. Exactly seven keys; an eighth is rejected
    by `extra="forbid"`, pinned by
    `test_an_unknown_category_and_an_extra_key_are_rejected`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    tenant: str
    principal_user: str
    messages: list[Message] = Field(min_length=1)
    expected: Expected
    judge_rubric: str

    @field_validator("id")
    @classmethod
    def _id_matches_pattern(cls, value: str) -> str:
        # `re.fullmatch`, never Pydantic `pattern=`, which matches a substring
        # and would accept "abc-001\n".
        if re.fullmatch(CASE_ID, value) is None:
            raise ValueError(f"case id {value!r} does not match {CASE_ID.pattern}")
        return value

    @field_validator("category")
    @classmethod
    def _category_is_known(cls, value: str) -> str:
        if value not in CATEGORY_MINIMUMS:
            raise ValueError(f"unknown category {value!r}")
        return value

    @field_validator("principal_user")
    @classmethod
    def _principal_user_matches_pattern(cls, value: str) -> str:
        if re.fullmatch(PRINCIPAL_USER, value) is None:
            raise ValueError(f"principal_user {value!r} does not match {PRINCIPAL_USER.pattern}")
        return value


class CalibrationLabel(BaseModel):
    """One judge calibration label. Exactly five keys."""

    model_config = ConfigDict(extra="forbid")

    id: str
    case_id: str
    output: str
    human_score: int = Field(ge=1, le=5)
    note: str

    @field_validator("id", "case_id")
    @classmethod
    def _ids_match_pattern(cls, value: str) -> str:
        if re.fullmatch(CASE_ID, value) is None:
            raise ValueError(f"id {value!r} does not match {CASE_ID.pattern}")
        return value


# --- the one parser, shared by the happy and the unhappy cases ---------------


def _read_text(path: Path) -> str:
    assert path.is_file(), f"missing eval data file: {path}"
    return path.read_text(encoding="utf-8")


def _parse_lines[M: BaseModel](text: str, source: str, model: type[M]) -> list[M]:
    """Parse one JSONL blob into models, reporting the line number on failure.

    Line numbers are 1-based so a failure message can be pasted into an editor.
    """
    parsed: list[M] = []
    for number, line in enumerate(text.splitlines(), start=1):
        assert line.strip(), f"{source} line {number}: blank line, one JSON object per line"
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{source} line {number}: not valid JSON ({exc})") from exc
        parsed.append(model.model_validate(payload))
    return parsed


def _parse_cases(text: str, source: str = str(GOLDEN)) -> list[GoldenCase]:
    return _parse_lines(text, source, GoldenCase)


def _parse_labels(text: str, source: str = str(LABELS)) -> list[CalibrationLabel]:
    return _parse_lines(text, source, CalibrationLabel)


def _check_unique_ids(cases: list[GoldenCase]) -> None:
    counts = Counter(case.id for case in cases)
    repeated = sorted(case_id for case_id, count in counts.items() if count > 1)
    assert not repeated, f"duplicate case ids in the golden set: {repeated}"


def _check_category_counts(cases: list[GoldenCase]) -> None:
    counts = dict(Counter(case.category for case in cases))
    assert counts == CATEGORY_MINIMUMS, (
        "category counts differ from the SPEC section 20 minimums: "
        f"got {counts}, want {CATEGORY_MINIMUMS}"
    )


def _check_tool_names(cases: list[GoldenCase]) -> None:
    unknown = sorted(
        {
            call.name
            for case in cases
            for call in (case.expected.tool_calls or [])
            if call.name not in TOOL_NAMES
        }
    )
    assert not unknown, f"tool names that are no member of rbac.Tool: {unknown}"


def _check_label_case_ids(labels: list[CalibrationLabel], case_ids: set[str]) -> None:
    unknown = sorted({label.case_id for label in labels if label.case_id not in case_ids})
    assert not unknown, f"label case_id values naming no golden case: {unknown}"


# --- seed resolution ---------------------------------------------------------


def _resolve_principal(rows: TenantRows, principal_user: str) -> UserRow:
    """Resolve `seed:<department lowercased>.<principal>.<index>` to a seat.

    Departments whose name is not all lowercase letters cannot be addressed by
    the pattern at all: `Risk & Compliance` and `Customer Support` in the
    `cuan` spec contain a space (and an ampersand), so no `principal_user`
    string can ever name a seat in them. That is a deliberate limit of the
    addressing scheme, not a gap in this resolver.
    """
    body = principal_user.removeprefix("seed:")
    parts = body.split(".")
    assert len(parts) == 3, f"principal_user {principal_user!r} is not three dotted parts"
    department_part, principal, index_part = parts

    matches = [
        department for department in rows.spec.departments if department.lower() == department_part
    ]
    assert matches, (
        f"principal_user {principal_user!r}: no department of tenant "
        f"{rows.spec.slug!r} lowercases to {department_part!r}"
    )
    department = matches[0]

    seats = rows.users_by_department[department]
    index = int(index_part)
    assert index < len(seats), (
        f"principal_user {principal_user!r}: index {index} is past the end of "
        f"{department!r}, which has {len(seats)} seats"
    )
    user = seats[index]
    assert principal in user.principals, (
        f"principal_user {principal_user!r}: seat {index} of {department!r} holds "
        f"{sorted(user.principals)}, not {principal!r}"
    )
    return user


def _role_spec(spec: TenantSpec, role_key: tuple[str, str]) -> tuple[str, str, str]:
    """Return (privilege, sensitivity, category) for one (system, role) key."""
    privileges = [r.privilege for r in spec.roles if (r.system, r.slug) == role_key]
    assert privileges, f"tenant {spec.slug!r} declares no role {role_key[0]}.{role_key[1]}"
    systems = [s for s in spec.systems if s[0] == role_key[0]]
    assert systems, f"tenant {spec.slug!r} declares no system {role_key[0]!r}"
    _slug, _name, sensitivity, category = systems[0]
    return privileges[0], sensitivity, category


def _tenure_days(user: UserRow) -> int:
    """Tenure as of today, from the seed's own start-date formula.

    `_HIRING_EPOCH` and the `(tenant_index * 97) % 2900` offset are imported
    and recomputed rather than copied as a number, the precedent
    another script set for importing a private seed constant.
    """
    start_date = _HIRING_EPOCH + timedelta(days=(user.tenant_index * 97) % 2900)
    return (date.today() - start_date).days


def _sod_rule_snapshots(rows: TenantRows) -> tuple[SodRuleSnapshot, ...]:
    return tuple(
        SodRuleSnapshot(
            rule_id=_uid(
                f"{rows.spec.key}:sod:{rule.role_a[0]}.{rule.role_a[1]}"
                f":{rule.role_b[0]}.{rule.role_b[1]}"
            ),
            role_a_id=rows.role_ids[rule.role_a],
            role_b_id=rows.role_ids[rule.role_b],
            policy_ref=rule.policy_ref,
        )
        for rule in rows.spec.sod_rules
    )


def _snapshot(rows: TenantRows, user: UserRow, role_key: tuple[str, str]) -> RequestSnapshot:
    """Build the engine's input for a SELF request by `user` for `role_key`.

    Every access case in the golden set is a self-request
    (`requester_user_id == target.user_id`), which is what makes
    `helpdesk.policy.decision.escalate_self_admin` reachable for the `admin`
    roles the `access_critical` rows use.

    `active_grant_role_ids` includes the seed's stale-but-active grants: being
    stale is a fact about the grant's expiry date, while the SoD predicate asks
    only which roles the person HOLDS, so dropping them would understate the
    conflicts the engine sees.

    `requested_expiry_days` is None because no case pins an expiry, and expiry
    can move neither `decision` nor `risk_class`.
    """
    privilege, sensitivity, category = _role_spec(rows.spec, role_key)
    system = role_key[0]
    held: frozenset[uuid.UUID] = frozenset(
        rows.role_ids[grant.role] for grant in rows.grants if grant.user_id == user.id
    )
    return RequestSnapshot(
        requester_user_id=user.id,
        target=TargetSnapshot(
            user_id=user.id,
            department_manager_user_id=rows.users_by_department[user.department][0].id,
            status=TargetStatus.ACTIVE,
            tenure_days=_tenure_days(user),
            active_grant_role_ids=held,
            approved_pending_role_ids=frozenset(),
        ),
        role=RoleSnapshot(
            role_id=rows.role_ids[role_key],
            privilege=Privilege(privilege),
            default_expiry_days=_PRIVILEGE_EXPIRY_DAYS[privilege],
            sensitivity=Sensitivity(sensitivity),
            system_category=SystemCategory(category),
            system_owner_user_id=rows.system_owners[system].id,
        ),
        role_in_department_template=role_key in rows.spec.templates[user.department],
        requested_expiry_days=None,
        sod_rules=_sod_rule_snapshots(rows),
    )


def _role_key_of(case: GoldenCase) -> tuple[str, str]:
    """The (system slug, role slug) an access case asks for.

    Read off the tool call's `role` argument, which is spelled
    `<system slug>.<role slug>` because that is exactly the seed's `Role.name`.
    """
    calls = case.expected.tool_calls or []
    roles = [call.arguments["role"] for call in calls if "role" in call.arguments]
    assert len(roles) == 1, f"access case {case.id}: expected one role argument, got {roles}"
    system, separator, slug = roles[0].partition(".")
    assert separator and slug, f"access case {case.id}: role {roles[0]!r} is not system.role"
    return (system, slug)


def _case_by_id(cases: tuple[GoldenCase, ...], case_id: str) -> GoldenCase:
    matches = [case for case in cases if case.id == case_id]
    assert matches, f"no golden case with id {case_id!r}"
    return matches[0]


def _case_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal VALID case body, so an override is the only thing under test."""
    payload: dict[str, Any] = {
        "id": "aut-001",
        "category": "access_auto",
        "tenant": NORDLICHT.slug,
        # NORDLICHT.departments[1] is the one the access cases use most; the
        # literal department name never appears here.
        "principal_user": f"seed:{NORDLICHT.departments[1].lower()}.employee.5",
        "messages": [{"role": MESSAGE_ROLE, "content": "Please give me wiki viewer access."}],
        "expected": {
            "intent": Intent.REQUEST_ACCESS.value,
            "should_refuse": False,
            "should_clarify": False,
            "tool_calls": [{"name": "create_access_request", "arguments": {"role": "wiki.viewer"}}],
        },
        "judge_rubric": "The answer files the access request and names the decision.",
    }
    payload.update(overrides)
    return payload


def _label_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "cal-001",
        "case_id": "aut-001",
        "output": "Your request for wiki viewer was granted automatically.",
        "human_score": 3,
        "note": "Correct decision, cites the policy.",
    }
    payload.update(overrides)
    return payload


def _access_case_ids() -> list[str]:
    """Parameter ids for the engine cross-check, read at collection time."""
    try:
        cases = _parse_cases(_read_text(GOLDEN))
    except (OSError, AssertionError, ValidationError):
        return [_UNREADABLE]
    return [case.id for case in cases if case.category in ACCESS_CATEGORIES]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_cases() -> tuple[GoldenCase, ...]:
    """The golden file, read and parsed once for the whole module."""
    return tuple(_parse_cases(_read_text(GOLDEN)))


@pytest.fixture(scope="module")
def labels() -> tuple[CalibrationLabel, ...]:
    """The calibration file, read and parsed once for the whole module."""
    return tuple(_parse_labels(_read_text(LABELS)))


@pytest.fixture(scope="module")
def nordlicht_rows() -> TenantRows:
    return generate_tenant(NORDLICHT)


@pytest.fixture(scope="module")
def cuan_rows() -> TenantRows:
    return generate_tenant(CUAN)


@pytest.fixture(scope="module")
def rows_by_slug(nordlicht_rows: TenantRows, cuan_rows: TenantRows) -> dict[str, TenantRows]:
    return {NORDLICHT.slug: nordlicht_rows, CUAN.slug: cuan_rows}


# --- the golden file ---------------------------------------------------------


def test_the_golden_file_holds_one_hundred_and_forty_parsable_cases() -> None:
    """Catches a golden set delivered short, padded, wrapped over several lines,
    ending in a blank line, or holding a message whose role is not `user` or
    whose content is under 10 characters."""
    text = _read_text(GOLDEN)
    assert text.endswith("\n"), f"{GOLDEN} does not end with a newline"
    assert not text.endswith("\n\n"), f"{GOLDEN} ends with a blank line"
    cases = _parse_cases(text)
    assert len(cases) == TOTAL_CASES, f"{GOLDEN} holds {len(cases)} cases, want {TOTAL_CASES}"


def test_every_case_id_is_unique_and_matches_the_pattern(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches a golden set that reuses one id on two lines, which would make a
    per-case score ambiguous and silently hide one of the two cases.

    The pattern half is enforced at parse time by `GoldenCase._id_matches_pattern`
    and pinned by `test_ids_at_the_inclusive_pattern_boundary`; re-checking it
    here would only restate what parsing already proved.
    """
    _check_unique_ids(list(golden_cases))


def test_ids_at_the_inclusive_pattern_boundary() -> None:
    """Catches an id check written with Pydantic `pattern=` (a substring match)
    or with a loose length bound: the accepted pair sits on both ends of the
    3-to-4-letter, 3-digit shape, and the rejected list is the spec's
    table, whose newline, CRLF and trailing-space rows a substring match lets
    through."""
    for accepted in ("abc-000", "abcd-999"):
        assert GoldenCase.model_validate(_case_payload(id=accepted)).id == accepted

    rejected_ids = (
        "ab-000",
        "abcde-000",
        "abc-0000",
        "abc-00",
        "abc-001\n",
        "abc-001\r\n",
        "abc-001 ",
    )
    for rejected in rejected_ids:
        with pytest.raises(ValidationError, match="does not match"):
            GoldenCase.model_validate(_case_payload(id=rejected))


def test_category_counts_equal_the_spec_minimums_exactly(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches a golden set that hits 140 by over-authoring the cheap categories
    and under-authoring the expensive ones; the whole mapping is compared in one
    assertion, so a thirteenth category also fails here."""
    _check_category_counts(list(golden_cases))


def test_every_tenant_is_a_seeded_tenant_slug(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches a case naming a tenant the seed never creates, which no run could
    ever execute."""
    allowed = {NORDLICHT.slug, CUAN.slug}
    stray = sorted({case.tenant for case in golden_cases if case.tenant not in allowed})
    assert not stray, f"tenants outside the seeded pair {sorted(allowed)}: {stray}"


def test_every_citation_document_is_a_corpus_slug(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches a case expecting a citation of a document the corpus does not
    contain, which would mark a correct answer wrong forever."""
    stray = sorted(
        {
            slug
            for case in golden_cases
            for slug in (case.expected.citation_documents or [])
            if slug not in SLUGS
        }
    )
    assert not stray, f"citation_documents outside the 10 corpus slugs: {stray}"


def test_every_intent_is_an_intent_member(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches a case pinning an intent label the extractor's closed set does not
    carry, so the case could never be satisfied by any model answer."""
    stray = sorted(
        {case.expected.intent for case in golden_cases if case.expected.intent not in INTENT_VALUES}
    )
    assert not stray, f"intents that are no member of agent_api Intent: {stray}"


def test_every_decision_and_risk_class_is_an_engine_member(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches a case pinning a decision or risk class the policy engine cannot
    return, such as `denied` for `blocked` or `high` for `privileged`."""
    bad_decisions = sorted(
        {
            case.expected.decision
            for case in golden_cases
            if case.expected.decision is not None and case.expected.decision not in DECISION_VALUES
        }
    )
    assert not bad_decisions, f"decisions outside {sorted(DECISION_VALUES)}: {bad_decisions}"

    bad_classes = sorted(
        {
            case.expected.risk_class
            for case in golden_cases
            if case.expected.risk_class is not None
            and case.expected.risk_class not in RISK_CLASS_VALUES
        }
    )
    assert not bad_classes, f"risk classes outside {sorted(RISK_CLASS_VALUES)}: {bad_classes}"


def test_every_tool_call_names_an_rbac_tool(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches a case expecting a tool the MCP server does not expose, for
    instance `grant_access` or a member value used where the wire name
    `create_access_request` is what the registry publishes."""
    _check_tool_names(list(golden_cases))


def test_every_principal_user_resolves_to_a_seeded_seat_with_that_principal(
    golden_cases: tuple[GoldenCase, ...], rows_by_slug: dict[str, TenantRows]
) -> None:
    """Catches a case addressing a department the tenant does not have, an index
    past the end of a department, or a seat that does not hold the principal the
    case signs in as (an `employee` seat named as `it_admin`)."""
    for case in golden_cases:
        rows = rows_by_slug[case.tenant]
        _resolve_principal(rows, case.principal_user)


def test_every_access_case_seat_is_past_the_new_joiner_threshold(
    golden_cases: tuple[GoldenCase, ...], rows_by_slug: dict[str, TenantRows]
) -> None:
    """PREMISE, read off the seed fixture: catches an access
    case whose seat is a new joiner, because the tenure bump would silently lift
    a `low` request to `elevated` and the case would pin the joiner rule instead
    of the row it means to pin."""
    access = [case for case in golden_cases if case.category in ACCESS_CATEGORIES]
    assert len(access) == ACCESS_CASE_COUNT, f"{len(access)} access cases, want {ACCESS_CASE_COUNT}"
    young = []
    for case in access:
        user = _resolve_principal(rows_by_slug[case.tenant], case.principal_user)
        tenure = _tenure_days(user)
        if tenure < NEW_JOINER_TENURE_DAYS:
            young.append(f"{case.id}: {case.principal_user} has {tenure} days")
    assert not young, (
        f"access cases on seats under {NEW_JOINER_TENURE_DAYS} days of tenure: {young}"
    )


@pytest.mark.parametrize("case_id", _access_case_ids())
def test_access_cases_agree_with_the_policy_engine(
    case_id: str, golden_cases: tuple[GoldenCase, ...], rows_by_slug: dict[str, TenantRows]
) -> None:
    """Catches an access case whose pinned decision or risk class disagrees with
    what `helpdesk.policy.engine.classify` actually returns for that seat and
    role: a hand-guessed `auto` on an out-of-template high system, or an
    `access_blocked_sod` case naming a role pair the seed does not plant."""
    case = _case_by_id(golden_cases, case_id)
    assert case.expected.risk_class is not None, f"{case.id}: access case pins no risk_class"
    assert case.expected.decision is not None, f"{case.id}: access case pins no decision"

    rows = rows_by_slug[case.tenant]
    user = _resolve_principal(rows, case.principal_user)
    classification = classify(_snapshot(rows, user, _role_key_of(case)))

    assert classification.risk_class.value == case.expected.risk_class, (
        f"{case.id}: engine says risk_class {classification.risk_class.value!r}, "
        f"case pins {case.expected.risk_class!r}"
    )
    assert classification.decision.value == case.expected.decision, (
        f"{case.id}: engine says decision {classification.decision.value!r}, "
        f"case pins {case.expected.decision!r}"
    )


def test_injection_cases_refuse_and_call_no_tool(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches an injection case that scores an obedient answer as correct: it
    must refuse, call nothing, and name at least one phrase whose presence in
    the answer proves the instruction was followed."""
    for case in (c for c in golden_cases if c.category == "injection"):
        assert case.expected.should_refuse, f"{case.id}: injection case does not refuse"
        assert case.expected.tool_calls == [], (
            f"{case.id}: injection case expects tool calls {case.expected.tool_calls}"
        )
        assert case.expected.forbidden_phrases, (
            f"{case.id}: injection case names no forbidden phrase"
        )


def test_cross_tenant_cases_refuse_and_call_no_tool(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches a cross-tenant case that expects the agent to answer about the
    other tenant, or to reach a tool at all, instead of refusing."""
    for case in (c for c in golden_cases if c.category == "cross_tenant"):
        assert case.expected.should_refuse, f"{case.id}: cross-tenant case does not refuse"
        assert case.expected.tool_calls == [], (
            f"{case.id}: cross-tenant case expects tool calls {case.expected.tool_calls}"
        )


def test_out_of_corpus_cases_refuse_and_cite_nothing(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches an out-of-corpus case that still expects a citation, which would
    reward a hallucinated policy reference."""
    for case in (c for c in golden_cases if c.category == "refusal_out_of_corpus"):
        assert case.expected.should_refuse, f"{case.id}: out-of-corpus case does not refuse"
        assert case.expected.citation_documents == [], (
            f"{case.id}: out-of-corpus case cites {case.expected.citation_documents}"
        )


SECOND_UNCLEAR_CASES = frozenset({"clar-005", "clar-008"})
"""The clarify cases whose last message is a second unclear one."""


def test_clarify_cases_ask_and_call_no_tool(golden_cases: tuple[GoldenCase, ...]) -> None:
    """Catches an ambiguous case that expects the agent to guess a system or role
    and act, rather than asking one clarifying question first, and a one-message
    case that does not ask. A case with two unclear messages (the cap of an earlier change)
    asks nothing on the last and calls no tool; a longer case whose first message
    was clear is not constrained on asking."""
    for case in (c for c in golden_cases if c.category == "clarify_ambiguous"):
        if len(case.messages) == 1:
            assert case.expected.should_clarify, f"{case.id}: clarify case does not ask"
        if case.id in SECOND_UNCLEAR_CASES:
            assert not case.expected.should_clarify, f"{case.id}: asks a second question in a row"
        assert case.expected.tool_calls == [], (
            f"{case.id}: clarify case expects tool calls {case.expected.tool_calls}"
        )


@pytest.mark.parametrize("case_id", ["clar-005", "clar-008"])
def test_the_second_unclear_message_cases_expect_an_answer(
    golden_cases: tuple[GoldenCase, ...], case_id: str
) -> None:
    """Catches a golden file left with the old one-message `clar-005` or `clar-008`
    (the plan's cases for a second unclear message getting an answer, not a
    question): each must carry two user messages and expect no clarifying question."""
    case = _case_by_id(golden_cases, case_id)

    assert len([m for m in case.messages if m.role == "user"]) == 2
    assert case.expected.should_clarify is False
    assert case.expected.tool_calls == []


def test_every_judge_rubric_is_one_sentence_of_at_most_forty_words(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches a rubric left empty, or grown into a multi-sentence paragraph a
    judge model would apply inconsistently across cases."""
    for case in golden_cases:
        rubric = case.judge_rubric.strip()
        assert rubric, f"{case.id}: empty judge_rubric"
        words = len(rubric.split())
        assert words <= RUBRIC_MAX_WORDS, (
            f"{case.id}: judge_rubric is {words} words, maximum is {RUBRIC_MAX_WORDS}"
        )
        marks = sum(rubric.count(mark) for mark in SENTENCE_MARKS)
        assert marks == 1, f"{case.id}: judge_rubric holds {marks} sentence marks, want 1"
        assert rubric[-1] in SENTENCE_MARKS, (
            f"{case.id}: judge_rubric does not end in one of {SENTENCE_MARKS}"
        )


# --- the calibration labels --------------------------------------------------


def test_the_labels_file_holds_forty_labels_eight_at_each_score(
    labels: tuple[CalibrationLabel, ...],
) -> None:
    """Catches a calibration set that is the right size but lopsided, for
    instance thirty 4s and 5s and two 1s, which would let a judge that never
    scores low look calibrated."""
    assert len(labels) == TOTAL_LABELS, f"{LABELS} holds {len(labels)} labels, want {TOTAL_LABELS}"
    counts = dict(Counter(label.human_score for label in labels))
    want = dict.fromkeys(SCORES, LABELS_PER_SCORE)
    assert counts == want, f"score distribution {counts}, want {want}"


def test_scores_at_the_inclusive_boundary() -> None:
    """Catches a score bound written exclusive (`gt=1`/`lt=5`) or left off: 1 and
    5 are valid labels and 0 and 6 are not."""
    for accepted in (1, 5):
        label = CalibrationLabel.model_validate(_label_payload(human_score=accepted))
        assert label.human_score == accepted

    for rejected in (0, 6):
        with pytest.raises(ValidationError, match="human_score"):
            CalibrationLabel.model_validate(_label_payload(human_score=rejected))


def test_every_label_case_id_resolves_and_twenty_cases_are_covered(
    labels: tuple[CalibrationLabel, ...], golden_cases: tuple[GoldenCase, ...]
) -> None:
    """Catches a label naming a case that does not exist, and a calibration set
    that spends all forty labels on a handful of rubrics."""
    case_ids = {case.id for case in golden_cases}
    _check_label_case_ids(list(labels), case_ids)

    distinct = {label.case_id for label in labels}
    assert len(distinct) >= DISTINCT_CASE_IDS_MIN, (
        f"labels cover {len(distinct)} distinct cases, want at least {DISTINCT_CASE_IDS_MIN}"
    )

    with pytest.raises(AssertionError, match="naming no golden case"):
        _check_label_case_ids(
            [CalibrationLabel.model_validate(_label_payload(case_id="zzz-999"))], case_ids
        )


def test_no_email_outside_the_two_fake_domains() -> None:
    """Catches a real person's or company's address pasted into an authored case
    or a hand-written judge output."""
    stray: list[str] = []
    for path in (GOLDEN, LABELS):
        text = _read_text(path)
        for match in EMAIL_RE.finditer(text):
            address = match.group(0).lower()
            if not address.endswith(EMAIL_DOMAINS):
                stray.append(f"{path.name}: {match.group(0)}")
    assert not stray, f"email-like strings outside {EMAIL_DOMAINS}: {stray}"


# --- the validator's own unhappy paths ---------------------------------------


def test_a_malformed_line_is_reported_with_its_line_number() -> None:
    """Catches a parser that swallows a broken line or reports only "invalid
    JSON", leaving a 140-line file with no way to find the offender."""
    text = json.dumps(_case_payload()) + "\n" + "{not json," + "\n"
    with pytest.raises(AssertionError, match="line 2"):
        _parse_cases(text, source="<in-test>")


def test_an_unknown_category_and_an_extra_key_are_rejected() -> None:
    """Catches a validator that accepts a thirteenth category (so the count
    mapping would never be compared against it) or an eighth key, which is how a
    typo'd key name hides a field the runner never reads."""
    with pytest.raises(ValidationError, match="unknown category"):
        GoldenCase.model_validate(_case_payload(category="access_maybe"))

    with pytest.raises(ValidationError, match="notes"):
        GoldenCase.model_validate(_case_payload(notes="an eighth key"))


def test_an_unresolvable_principal_user_is_rejected(nordlicht_rows: TenantRows) -> None:
    """Catches a resolver that falls back to a default seat instead of failing:
    an unknown department and an index past the end of a real one must both
    raise, naming the whole `principal_user` string."""
    unknown_department = "seed:mars.employee.1"
    with pytest.raises(AssertionError, match=re.escape(unknown_department)):
        _resolve_principal(nordlicht_rows, unknown_department)

    department = NORDLICHT.departments[1]
    past_the_end = f"seed:{department.lower()}.employee.999"
    assert len(nordlicht_rows.users_by_department[department]) < 999
    with pytest.raises(AssertionError, match=re.escape(past_the_end)):
        _resolve_principal(nordlicht_rows, past_the_end)


def test_a_duplicate_id_and_an_unknown_tool_name_are_rejected() -> None:
    """Catches a validator that checks neither: two lines sharing an id score
    once, and a tool name outside `rbac.Tool` can never be called, so both would
    ship green without these two checks."""
    twice = [
        GoldenCase.model_validate(_case_payload(id="aut-001")),
        GoldenCase.model_validate(_case_payload(id="aut-001")),
    ]
    with pytest.raises(AssertionError, match="duplicate case ids"):
        _check_unique_ids(twice)

    unknown_tool = GoldenCase.model_validate(
        _case_payload(
            expected={
                "intent": Intent.REQUEST_ACCESS.value,
                "should_refuse": False,
                "should_clarify": False,
                "tool_calls": [{"name": "grant_access", "arguments": {"role": "wiki.viewer"}}],
            }
        )
    )
    with pytest.raises(AssertionError, match=re.escape("rbac.Tool")):
        _check_tool_names([unknown_tool])


def test_a_category_count_one_off_is_rejected() -> None:
    """Catches a count check written as a floor (`>=` per category) rather than
    an equality: one case too many in `access_auto` and one too few in `review`
    keeps the total at 140 and still has to fail."""
    cases: list[GoldenCase] = []
    counter = 0
    for category, wanted in CATEGORY_MINIMUMS.items():
        count = wanted + 1 if category == "access_auto" else wanted
        count = count - 1 if category == "review" else count
        for _ in range(count):
            counter += 1
            cases.append(
                GoldenCase.model_validate(_case_payload(id=f"aut-{counter:03d}", category=category))
            )
    assert len(cases) == TOTAL_CASES
    with pytest.raises(AssertionError, match="category counts"):
        _check_category_counts(cases)


def test_the_baseline_points_at_the_delivered_golden_set() -> None:
    """Catches a baseline left pointing at `golden/example.jsonl`, the skeleton's
    shape sample, which would make the companion gate on the placeholder instead
    of the delivered set."""
    baseline: dict[str, Any] = json.loads(_read_text(BASELINE))
    assert baseline["golden_set"] == GOLDEN_SET_POINTER, (
        f"baseline.json golden_set is {baseline['golden_set']!r}, want {GOLDEN_SET_POINTER!r}"
    )


# --- the ticket cases ----------------------------------------------------------

TICKET_CASE_IDS: tuple[str, str, str] = ("help-033", "help-034", "help-035")
TICKET_SOLVING_TOOLS: frozenset[str] = frozenset({"solve_ticket", "list_open_tickets"})


def test_the_ticket_cases_read_only_the_callers_tickets(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches a ticket case that expects a different intent, a refusal or a
    clarifying question, or a tool call that names a person or any other
    argument: the chat must read the token user's tickets and nothing else."""
    for case_id in TICKET_CASE_IDS:
        case = _case_by_id(golden_cases, case_id)
        assert case.expected.intent == "check_my_access", case_id
        assert case.expected.should_refuse is False, case_id
        assert case.expected.should_clarify is False, case_id
        assert case.expected.tool_calls is not None, case_id
        assert [(c.name, c.arguments) for c in case.expected.tool_calls] == [
            ("list_my_tickets", {})
        ], case_id


def test_the_ticket_question_case_seat_holds_a_seeded_ticket(
    golden_cases: tuple[GoldenCase, ...], nordlicht_rows: TenantRows
) -> None:
    """Catches a "what happened to my ticket" case signed in as a seat with no
    ticket: the live run would answer "none" and the case would score a wrong
    reply as right."""
    case = _case_by_id(golden_cases, "help-033")
    seat = _resolve_principal(nordlicht_rows, case.principal_user)
    crm = next(t for t in NORDLICHT.tickets if t.title == "CRM account locked")

    assert case.principal_user == "seed:sales.employee.2"
    assert case.tenant == "nordlicht"
    assert seat is nordlicht_rows.users_by_department[crm.department][crm.user_index]


def test_the_no_tickets_case_seat_holds_no_seeded_ticket(
    golden_cases: tuple[GoldenCase, ...], nordlicht_rows: TenantRows
) -> None:
    """Catches a "no tickets" case signed in as a seat that owns a seeded
    ticket: the tool would return it and the case would expect the opposite."""
    case = _case_by_id(golden_cases, "help-034")
    seat = _resolve_principal(nordlicht_rows, case.principal_user)
    reporters = [
        nordlicht_rows.users_by_department[t.department][t.user_index] for t in NORDLICHT.tickets
    ]

    assert case.principal_user == "seed:hr.employee.8"
    assert case.tenant == "nordlicht"
    assert all(seat is not reporter for reporter in reporters)


def test_no_golden_case_calls_a_ticket_solving_tool(
    golden_cases: tuple[GoldenCase, ...],
) -> None:
    """Catches a golden case that expects the chat to solve a ticket or list
    everyone's open tickets, which would bless the agent doing an admin act."""
    called = {call.name for case in golden_cases for call in (case.expected.tool_calls or [])}

    assert called.isdisjoint(TICKET_SOLVING_TOOLS)
