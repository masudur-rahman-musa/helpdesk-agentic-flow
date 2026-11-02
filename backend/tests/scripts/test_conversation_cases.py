"""Checks of the conversation case file, read-only:

    backend/evals/conversations/cases.jsonl

The help desk owns 45 conversation cases; the companion console plays and
scores them. Every named value is checked against the code that owns it:
tenant slugs, seats, roles, grants, planted requests and tickets from
``scripts.seed``, tool names from the chat loop and the tool registry.

Each check is a function over parsed cases, so a made-up bad case can be fed
to it in the unhappy-path rows.

Cost: no backing service, no network. The slowest step is ``generate_tenant``
for each of the two tenants, run once each in a module-scoped fixture.
"""

import copy
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from helpdesk.agent_api.agent.loop_tools import _CHANGES, _LOOKUPS
from helpdesk.mcp_server.tools.registry import REGISTRY
from scripts.seed import CUAN, NORDLICHT, TenantRows, UserRow, generate_tenant


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


CASES = _repo_root() / "backend" / "evals" / "conversations" / "cases.jsonl"

CASE_COUNT = 45
CASE_FIELDS = {
    "id",
    "purpose",
    "owner",
    "tags",
    "scripted",
    "person",
    "world",
    "goal",
    "opening",
    "person_knows",
    "confirms_if",
    "right_end_states",
    "never",
    "reply_checks",
    "limits",
}
PERSON_FIELDS = {"seat", "role", "tenant", "language"}
LIMIT_FIELDS = {"max_turns", "max_tool_calls"}
MAX_TURNS_RANGE = (1, 10)
MAX_TOOL_CALLS_RANGE = (1, 20)
OWNERS = frozenset({"access", "accounts", "policy", "safety"})

START_KINDS = frozenset(
    {
        "holds",
        "lacks",
        "open_request",
        "no_open_request",
        "open_ticket",
        "no_open_ticket",
        "in_department",
    }
)
END_ONLY_KINDS = frozenset(
    {"is_leaver", "user_exists", "reset_asked", "unlocked", "open_review", "nothing_changed"}
)
END_KINDS = START_KINDS | END_ONLY_KINDS
TRUE_KINDS = frozenset({"is_leaver", "reset_asked", "unlocked", "nothing_changed"})
ROLE_KINDS = frozenset({"holds", "lacks", "open_request", "no_open_request"})

CHECK_TYPES = frozenset(
    {
        "asks_before_change",
        "reports_from_tool",
        "cites_policy",
        "asks_only_what_is_missing",
        "refuses_safely",
        "same_language",
        "stays_on_topic",
        "honest_about_failure",
    }
)
CHECK_MIN_USES = 5

VARIETIES = frozenset(
    {
        "everyday_words",
        "typos",
        "short_answers",
        "follow_ups",
        "changes_of_mind",
        "for_others",
        "other_languages",
        "off_topic",
        "attacks",
        "live_failures",
        "tricky_phrasing",
    }
)
EXACT_WORDS_VARIETIES = frozenset({"attacks", "other_languages", "tricky_phrasing"})
JOB_PREFIX = "job:"
CHAT_TOOLS = frozenset(_LOOKUPS) | frozenset(_CHANGES)

SEAT_PATTERN = re.compile(r"seed:([a-z]+)\.([a-z_]+)\.([0-9]{1,3})")
PERSON_PATTERN = re.compile(r"([a-z]+)\.([0-9]{1,3})")
ROLE_PATTERN = re.compile(r"[^.\s]+\.[^.\s]+")
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)?")
SIBLING_KINDS = ("lacks", "holds", "open_request")
MIN_SIBLING_GROUPS = 3

Case = dict[str, Any]
Fact = dict[str, Any]


# --- reading ------------------------------------------------------------------


def _lines() -> list[str]:
    assert CASES.is_file(), f"{CASES} does not exist"
    return CASES.read_text(encoding="utf-8").splitlines()


def _load() -> list[Case]:
    return [json.loads(line) for line in _lines()]


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return _load()


@pytest.fixture(scope="module")
def rows_by_slug() -> dict[str, TenantRows]:
    return {spec.slug: generate_tenant(spec) for spec in (NORDLICHT, CUAN)}


def _good_seat(rows: TenantRows) -> tuple[str, str, UserRow]:
    """A seat string that exists on a fresh seed: the first department that can
    be seated (letters only), its first person."""
    for department in rows.spec.departments:
        if re.fullmatch(r"[A-Za-z]+", department):
            user = rows.users_by_department[department][0]
            return f"seed:{department.lower()}.employee.0", department, user
    raise AssertionError("no department can be seated")


def _made_up(rows: TenantRows | None = None, **override: Any) -> Case:
    """A case that passes every shape check, with `override` laid over it."""
    seat = _good_seat(rows or generate_tenant(NORDLICHT))[0]
    case: Case = {
        "id": "case-001",
        "purpose": "Checks that a made-up case is read.",
        "owner": "access",
        "tags": ["job:create_access_request", "everyday_words"],
        "scripted": False,
        "person": {"seat": seat, "role": "employee", "tenant": "nordlicht", "language": "en"},
        "world": [],
        "goal": "Get read access to the wiki.",
        "opening": "I need the wiki.",
        "person_knows": "Needs it for onboarding.",
        "confirms_if": "The agent names the wiki viewer role.",
        "right_end_states": [[{"nothing_changed": True}]],
        "never": ["Never files the request before asking."],
        "reply_checks": [{"check": "asks_before_change", "applies_when": "a change is proposed"}],
        "limits": {"max_turns": 4, "max_tool_calls": 6},
    }
    case.update(copy.deepcopy(override))
    return case


# --- the checks, each over parsed cases ---------------------------------------


def _check_fields(cases: Sequence[Case]) -> None:
    for case in cases:
        name = case.get("id")
        expected = CASE_FIELDS | {"script"} if case.get("scripted") is True else CASE_FIELDS
        assert set(case) == expected, f"{name}: fields are {sorted(set(case) ^ expected)}"
        for text_field in ("purpose", "goal", "opening", "person_knows", "confirms_if"):
            value = case[text_field]
            assert isinstance(value, str) and value.strip(), f"{name}: {text_field} is empty"
        assert "\n" not in case["purpose"], f"{name}: purpose is more than one line"
        assert case["owner"] in OWNERS, f"{name}: owner {case['owner']!r}"
        assert isinstance(case["scripted"], bool), f"{name}: scripted is not a bool"
        tags = case["tags"]
        assert isinstance(tags, list) and tags, f"{name}: tags"
        assert all(isinstance(tag, str) for tag in tags), f"{name}: a tag is not text"
        person = case["person"]
        assert isinstance(person, dict) and set(person) == PERSON_FIELDS, f"{name}: person keys"
        assert SEAT_PATTERN.fullmatch(person["seat"]), f"{name}: seat {person['seat']!r}"
        assert isinstance(person["role"], str) and person["role"], f"{name}: person.role"
        assert isinstance(person["tenant"], str) and person["tenant"], f"{name}: person.tenant"
        assert LANGUAGE_PATTERN.fullmatch(person["language"]), f"{name}: language"
        assert isinstance(case["world"], list), f"{name}: world is not a list"
        ends = case["right_end_states"]
        assert isinstance(ends, list) and len(ends) >= 1, f"{name}: no right end state"
        assert all(isinstance(end, list) and end for end in ends), f"{name}: an empty end state"
        never = case["never"]
        assert isinstance(never, list), f"{name}: never is not a list"
        assert all(isinstance(rule, str) and rule.strip() for rule in never), f"{name}: never"
        checks = case["reply_checks"]
        assert isinstance(checks, list), f"{name}: reply_checks is not a list"
        for check in checks:
            assert set(check) == {"check", "applies_when"}, f"{name}: reply check keys"
        limits = case["limits"]
        assert isinstance(limits, dict) and set(limits) == LIMIT_FIELDS, f"{name}: limits keys"
        for key, (low, high) in (
            ("max_turns", MAX_TURNS_RANGE),
            ("max_tool_calls", MAX_TOOL_CALLS_RANGE),
        ):
            value = limits[key]
            assert type(value) is int, f"{name}: {key} is not a whole number"
            assert low <= value <= high, f"{name}: {key} {value} is outside {low} to {high}"


def _check_scripts(cases: Sequence[Case]) -> None:
    for case in cases:
        name = case.get("id")
        if case.get("scripted") is not True:
            assert "script" not in case, f"{name}: an unscripted case has a script"
            continue
        script = case.get("script")
        assert isinstance(script, list) and script, f"{name}: a scripted case has no steps"
        for step in script:
            assert isinstance(step, dict) and set(step) == {"say", "yes"}, f"{name}: step keys"
            assert isinstance(step["say"], str) and 1 <= len(step["say"]) <= 4000, (
                f"{name}: a say is not 1 to 4000 characters"
            )
            assert type(step["yes"]) is bool, f"{name}: yes is not true or false"


def _check_yes_step(cases: Sequence[Case]) -> None:
    for case in cases:
        if case.get("scripted") is not True:
            continue
        if case["confirms_if"].strip().lower().startswith("never"):
            continue
        assert any(step["yes"] is True for step in case["script"]), (
            f"{case['id']}: the case waits for a yes but no step says yes"
        )


def _check_fact_shape(fact: Fact, allowed: frozenset[str]) -> None:
    kinds = set(fact) - {"person"}
    assert len(kinds) == 1, f"a fact holds exactly one kind, got {sorted(fact)}"
    kind = next(iter(kinds))
    assert kind in allowed, f"fact kind {kind!r} is not allowed here"
    value = fact[kind]
    if kind in TRUE_KINDS:
        assert value is True, f"{kind} is written true"
    elif kind == "user_exists":
        assert isinstance(value, str) and "@" in value, f"{kind} wants an email"
    else:
        assert isinstance(value, str) and value.strip(), f"{kind} wants a name"
        if kind in ROLE_KINDS:
            assert ROLE_PATTERN.fullmatch(value), f"{kind} wants <system>.<role>, got {value!r}"
    if "person" in fact:
        person = fact["person"]
        assert isinstance(person, str) and PERSON_PATTERN.fullmatch(person), f"person {person!r}"


def _check_fact_kinds(cases: Sequence[Case]) -> None:
    for case in cases:
        assert case["right_end_states"], f"{case['id']}: no right end state"
        for fact in case["world"]:
            _check_fact_shape(fact, START_KINDS)
        for end in case["right_end_states"]:
            for fact in end:
                _check_fact_shape(fact, END_KINDS)


def _all_facts(case: Case) -> list[Fact]:
    return list(case["world"]) + [fact for end in case["right_end_states"] for fact in end]


def _department_names(rows: TenantRows) -> dict[str, str]:
    return {name.lower(): name for name in rows.spec.departments}


def _lookup_seat(case: Case, rows_by_slug: Mapping[str, TenantRows]) -> UserRow:
    person = case["person"]
    name = case["id"]
    assert person["tenant"] in rows_by_slug, f"{name}: tenant {person['tenant']!r} is not seeded"
    rows = rows_by_slug[person["tenant"]]
    matched = SEAT_PATTERN.fullmatch(person["seat"])
    assert matched, f"{name}: seat {person['seat']!r}"
    department_part, principal, index = matched.group(1), matched.group(2), int(matched.group(3))
    departments = _department_names(rows)
    assert department_part in departments, f"{name}: no department {department_part!r}"
    people = rows.users_by_department[departments[department_part]]
    assert index < len(people), f"{name}: no person {index} in {department_part}"
    user = people[index]
    assert principal == person["role"], f"{name}: seat principal is not the role"
    assert principal in user.principals, f"{name}: {person['seat']} is not a {principal}"
    return user


def _lookup_person(
    fact: Fact, case: Case, rows_by_slug: Mapping[str, TenantRows]
) -> tuple[UserRow, TenantRows]:
    """The one the fact speaks of: its `person` when given, else the case's seat."""
    rows = rows_by_slug[case["person"]["tenant"]]
    own = _lookup_seat(case, rows_by_slug)
    if "person" not in fact:
        return own, rows
    matched = PERSON_PATTERN.fullmatch(fact["person"])
    assert matched, f"{case['id']}: person {fact['person']!r}"
    departments = _department_names(rows)
    department_part, index = matched.group(1), int(matched.group(2))
    assert department_part in departments, f"{case['id']}: no department {department_part!r}"
    people = rows.users_by_department[departments[department_part]]
    assert index < len(people), f"{case['id']}: no person {fact['person']}"
    other = people[index]
    assert other.id != own.id, f"{case['id']}: a fact names the case's own person"
    return other, rows


def _check_seed_references(cases: Sequence[Case], rows_by_slug: Mapping[str, TenantRows]) -> None:
    for case in cases:
        _lookup_seat(case, rows_by_slug)
        rows = rows_by_slug[case["person"]["tenant"]]
        roles = {f"{role.system}.{role.slug}" for role in rows.spec.roles}
        for fact in _all_facts(case):
            _lookup_person(fact, case, rows_by_slug)
            kind = next(iter(set(fact) - {"person"}))
            if kind in ROLE_KINDS:
                assert fact[kind] in roles, f"{case['id']}: {fact[kind]!r} is no role of the tenant"
            if kind in {"in_department", "open_review"}:
                assert fact[kind] in rows.spec.departments, (
                    f"{case['id']}: {fact[kind]!r} is no department of the tenant"
                )


def _check_world_facts(cases: Sequence[Case], rows_by_slug: Mapping[str, TenantRows]) -> None:
    for case in cases:
        for fact in case["world"]:
            user, rows = _lookup_person(fact, case, rows_by_slug)
            kind = next(iter(set(fact) - {"person"}))
            value = fact[kind]
            where = f"{case['id']}: {fact}"
            if kind in {"holds", "lacks"}:
                role = tuple(value.split(".", 1))
                held = any(g.user_id == user.id and g.role == role for g in rows.grants)
                assert held == (kind == "holds"), f"{where} is false on a fresh seed"
            elif kind in {"open_request", "no_open_request"}:
                role = tuple(value.split(".", 1))
                asked = any(
                    p.target.id == user.id and p.role == role and p.status == "pending"
                    for p in rows.planted_requests
                )
                assert asked == (kind == "open_request"), f"{where} is false on a fresh seed"
            elif kind in {"open_ticket", "no_open_ticket"}:
                filed = any(
                    rows.users_by_department[t.department][t.user_index].id == user.id
                    and t.kind == value
                    for t in rows.spec.tickets
                )
                assert filed == (kind == "open_ticket"), f"{where} is false on a fresh seed"
            else:
                assert kind == "in_department", kind
                assert user.department == value, f"{where} is false on a fresh seed"


def _check_reply_checks(cases: Sequence[Case]) -> None:
    uses: Counter[str] = Counter()
    for case in cases:
        for check in case["reply_checks"]:
            assert check["check"] in CHECK_TYPES, f"{case['id']}: unknown check {check['check']!r}"
            applies = check["applies_when"]
            assert isinstance(applies, str) and applies.strip(), (
                f"{case['id']}: {check['check']} has no applies_when"
            )
            uses[check["check"]] += 1
    for check_type in sorted(CHECK_TYPES):
        assert uses[check_type] >= CHECK_MIN_USES, (
            f"{check_type} is used {uses[check_type]} times, want at least {CHECK_MIN_USES}"
        )


def _check_scripted(cases: Sequence[Case]) -> None:
    for case in cases:
        exact = bool(EXACT_WORDS_VARIETIES & set(case["tags"]))
        assert case["scripted"] is exact, (
            f"{case['id']}: scripted is {case['scripted']} but tags {case['tags']} "
            f"say it should be {exact}"
        )


def _check_tags(cases: Sequence[Case]) -> None:
    jobs_seen: set[str] = set()
    varieties_seen: set[str] = set()
    for case in cases:
        jobs = {t.removeprefix(JOB_PREFIX) for t in case["tags"] if t.startswith(JOB_PREFIX)}
        varieties = {t for t in case["tags"] if not t.startswith(JOB_PREFIX)}
        assert jobs, f"{case['id']}: no job tag"
        assert varieties, f"{case['id']}: no variety tag"
        assert varieties <= VARIETIES, f"{case['id']}: unknown tag {sorted(varieties - VARIETIES)}"
        assert jobs <= set(REGISTRY), f"{case['id']}: {sorted(jobs - set(REGISTRY))} not a tool"
        assert jobs <= CHAT_TOOLS, f"{case['id']}: {sorted(jobs - CHAT_TOOLS)} not a chat tool"
        jobs_seen |= jobs
        varieties_seen |= varieties
    assert jobs_seen >= CHAT_TOOLS, f"no case for {sorted(CHAT_TOOLS - jobs_seen)}"
    assert varieties_seen >= VARIETIES, f"no case for {sorted(VARIETIES - varieties_seen)}"


def _world_key(case: Case) -> str:
    """The world as text that does not depend on the order of its facts."""
    return json.dumps(sorted(json.dumps(f, sort_keys=True) for f in case["world"]))


def _check_siblings(cases: Sequence[Case]) -> None:
    pairs = Counter((case["opening"], _world_key(case)) for case in cases)
    twins = [pair[0] for pair, count in pairs.items() if count > 1]
    assert not twins, f"two cases share an opening and a world: {twins}"
    by_opening: dict[str, list[Case]] = {}
    for case in cases:
        by_opening.setdefault(case["opening"], []).append(case)
    groups = [group for group in by_opening.values() if len(group) > 1]
    assert len(groups) >= MIN_SIBLING_GROUPS, (
        f"{len(groups)} sibling groups, want at least {MIN_SIBLING_GROUPS}"
    )
    for kind in SIBLING_KINDS:
        assert any(kind in fact for group in groups for case in group for fact in case["world"]), (
            f"no sibling case has a {kind} world"
        )


# --- the file -----------------------------------------------------------------


def test_the_file_holds_45_cases_case_001_to_case_045_in_order() -> None:
    """Catches a file with a missing or extra case, a blank or unparsable line,
    a case out of order, or ids that still carry the old prefix.

    The ids are compared with the exact list, so 44 right cases plus a
    duplicate, or a case moved, fails.
    """
    lines = _lines()
    assert all(line.strip() for line in lines), "a blank line sits in the file"
    parsed = [json.loads(line) for line in lines]

    assert len(parsed) == CASE_COUNT
    assert [case["id"] for case in parsed] == [f"case-{n:03d}" for n in range(1, CASE_COUNT + 1)]


def test_every_case_has_every_field_and_no_other(cases: list[Case]) -> None:
    """Catches a case missing one of the 15 fields (16 with `script`, which only a
    scripted case has), carrying an extra or an old one, a person or limits block
    with the wrong keys, a limit outside its range, or an owner outside the four.
    """
    _check_fields(cases)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"category": "typos"}, id="extra-field"),
        pytest.param({"owner": "sales"}, id="owner-not-in-the-four"),
        pytest.param({"scripted": "yes"}, id="scripted-not-a-bool"),
        pytest.param({"script": [{"say": "Ja", "yes": True}]}, id="script-on-unscripted"),
        pytest.param({"scripted": True}, id="scripted-with-no-script-field"),
        pytest.param({"person": {"seat": "x"}}, id="person-keys"),
        pytest.param({"limits": {"max_turns": 4}}, id="limits-keys"),
        pytest.param({"limits": {"max_turns": 0, "max_tool_calls": 6}}, id="turns-0"),
        pytest.param({"limits": {"max_turns": 11, "max_tool_calls": 6}}, id="turns-11"),
        pytest.param({"limits": {"max_turns": 4, "max_tool_calls": 0}}, id="calls-0"),
        pytest.param({"limits": {"max_turns": 4, "max_tool_calls": 21}}, id="calls-21"),
        pytest.param({"limits": {"max_turns": True, "max_tool_calls": 6}}, id="bool"),
        pytest.param({"right_end_states": []}, id="no-end-state"),
        pytest.param({"reply_checks": [{"check": "same_language"}]}, id="check-keys"),
    ],
)
def test_a_bad_case_is_caught_by_the_field_check(mutation: Mapping[str, Any]) -> None:
    """Catches a field check that is too loose: one that ignores extra keys,
    accepts a limit one past its range, treats `True` as a whole number, or
    lets a case with no right end state through.

    Each row breaks exactly one thing in an otherwise good case; the good
    case is checked first so a checker that refuses everything fails here.
    """
    _check_fields([_made_up()])
    with pytest.raises(AssertionError):
        _check_fields([_made_up(**mutation)])


def test_a_scripted_case_may_carry_a_script() -> None:
    """Catches a field check that refuses the new field on a scripted case: the 16th field
    `script` is allowed exactly when `scripted` is true."""
    _check_fields([_made_up(scripted=True, script=[{"say": "Ja", "yes": True}])])


def test_the_limits_accept_their_own_ends() -> None:
    """Catches a limit check one too tight: 1 and 10 turns, 1 and 20 tool calls
    are the inclusive ends and must pass."""
    for turns, calls in ((1, 1), (10, 20)):
        _check_fields([_made_up(limits={"max_turns": turns, "max_tool_calls": calls})])


@pytest.mark.parametrize("tail", ["\n", "\r\n", " "])
def test_a_seat_with_a_trailing_character_is_refused(tail: str) -> None:
    """Catches a seat pattern that is not matched in full: a trailing newline,
    CRLF or space after a good seat would let a seat through that the eval route
    can never find.
    """
    case = _made_up()
    case["person"]["seat"] += tail
    with pytest.raises(AssertionError):
        _check_fields([case])


# --- the seed -----------------------------------------------------------------


def test_every_seat_role_and_department_is_in_the_seed(
    cases: list[Case], rows_by_slug: dict[str, TenantRows]
) -> None:
    """Catches a case seated on a department, person or principal the seed does
    not have, a tenant that is not seeded, a fact `person` that does not exist
    or is the case's own person, and a fact naming a role or department the
    tenant lacks (a role of the other tenant included).
    """
    _check_seed_references(cases, rows_by_slug)


def test_a_made_up_seat_is_caught(rows_by_slug: dict[str, TenantRows]) -> None:
    """Catches a reference check that lets a made-up seat or fact through: a
    person past the end of a department, a principal the person does not hold,
    a role that differs from the seat's principal, an unseeded tenant, a fact
    about the case's own person, and a role no system of the tenant has.

    The good case is checked first, so a checker that refuses everything fails.
    """
    rows = rows_by_slug["nordlicht"]
    seat, department, _ = _good_seat(rows)
    count = len(rows.users_by_department[department])
    base = seat.rsplit(".", 1)[0]
    _check_seed_references([_made_up()], rows_by_slug)
    bad = [
        _made_up(person={**_made_up()["person"], "seat": f"{base}.{count}"}),
        _made_up(
            person={**_made_up()["person"], "seat": f"{base.replace('employee', 'nobody')}.0"}
        ),
        _made_up(person={**_made_up()["person"], "role": "manager"}),
        _made_up(person={**_made_up()["person"], "tenant": "nowhere"}),
        _made_up(world=[{"holds": "wiki.viewer", "person": f"{department.lower()}.0"}]),
        _made_up(world=[{"holds": "nosuch.system"}]),
        _made_up(world=[{"in_department": "Nowhere"}]),
        _made_up(world=[{"holds": "wiki.viewer", "person": "nowhere.0"}]),
        _made_up(world=[{"holds": "wiki.viewer", "person": f"{department.lower()}.{count}"}]),
    ]
    for case in bad:
        with pytest.raises(AssertionError):
            _check_seed_references([case], rows_by_slug)


def test_every_world_fact_is_true_on_a_fresh_seed(
    cases: list[Case], rows_by_slug: dict[str, TenantRows]
) -> None:
    """Catches a case whose start world says something the fresh seed does not
    hold: a role the person lacks, an open request nobody planted, a ticket
    nobody filed, or a department the person is not in.

    Grants come from the generated rows, requests from the planted requests
    and tickets from the tenant's ticket list, so a fact checked against
    nothing fails.
    """
    _check_world_facts(cases, rows_by_slug)


def test_a_false_world_fact_is_caught(rows_by_slug: dict[str, TenantRows]) -> None:
    """Catches a world check that passes a false fact, or that ignores the
    `person` of a fact and always reads the case's own person.

    Each false fact is built from the real rows (a role the person lacks, one
    the person holds, a request nobody planted, a ticket kind nobody filed, a
    ticket that exists, a wrong department) and the matching true fact is
    checked first, so a checker that refuses everything fails too. The
    `person` row picks a role held by one person and not by a neighbour:
    the same fact is true for the first and false for the second.
    """
    rows = rows_by_slug["nordlicht"]
    seat, department, user = _good_seat(rows)
    held = sorted({g.role for g in rows.grants if g.user_id == user.id})
    assert held, "the first person of the first department holds nothing"
    missing = next((r.system, r.slug) for r in rows.spec.roles if (r.system, r.slug) not in held)
    wiki = f"{held[0][0]}.{held[0][1]}"
    absent = f"{missing[0]}.{missing[1]}"

    def check(*world: Fact, on: str = seat) -> None:
        _check_world_facts(
            [_made_up(person={**_made_up()["person"], "seat": on}, world=list(world))],
            rows_by_slug,
        )

    check({"holds": wiki}, {"lacks": absent}, {"in_department": department})
    for fact in (
        {"holds": absent},
        {"lacks": wiki},
        {"in_department": "Nowhere"},
    ):
        with pytest.raises(AssertionError):
            check(fact)

    ticketed = {rows.users_by_department[t.department][t.user_index].id for t in rows.spec.tickets}
    filed = next(t for t in rows.spec.tickets if re.fullmatch(r"[A-Za-z]+", t.department))
    filer_seat = f"seed:{filed.department.lower()}.employee.{filed.user_index}"
    assert user.id not in ticketed, "pick another seat: the first person has a ticket"
    check({"no_open_ticket": filed.kind})
    check({"open_ticket": filed.kind}, on=filer_seat)
    with pytest.raises(AssertionError):
        check({"open_ticket": filed.kind})
    with pytest.raises(AssertionError):
        check({"no_open_ticket": filed.kind}, on=filer_seat)

    planted = {p.target.id for p in rows.planted_requests}
    assert user.id not in planted, "pick another seat: the first person has a request"
    check({"no_open_request": wiki})
    with pytest.raises(AssertionError):
        check({"open_request": wiki})

    neighbour = _role_held_by_one_of_two(rows, department)
    holder_index, other_index, role = neighbour
    holder_seat = f"seed:{department.lower()}.employee.{holder_index}"
    other_fact = {"holds": role, "person": f"{department.lower()}.{other_index}"}
    own_fact = {"holds": role}
    check(own_fact, on=holder_seat)
    with pytest.raises(AssertionError):
        check(other_fact, on=holder_seat)
    check({"lacks": role, "person": f"{department.lower()}.{other_index}"}, on=holder_seat)


def _role_held_by_one_of_two(rows: TenantRows, department: str) -> tuple[int, int, str]:
    """Two people of one department and a role the first holds and the second does not."""
    people = rows.users_by_department[department]
    held = {u.id: {g.role for g in rows.grants if g.user_id == u.id} for u in people[:8]}
    for i, first in enumerate(people[:8]):
        for j, second in enumerate(people[:8]):
            if i == j:
                continue
            only = sorted(held[first.id] - held[second.id])
            if only:
                return i, j, f"{only[0][0]}.{only[0][1]}"
    raise AssertionError("no two people differ by a role")


# --- kinds, checks, tags ------------------------------------------------------


def test_facts_use_only_the_fixed_kinds(cases: list[Case]) -> None:
    """Catches a start world that uses a kind only an end state may use, an end
    state with a kind outside the 13, a fact with two kinds, a value of the
    wrong type, or a case with no right end state.
    """
    _check_fact_kinds(cases)


@pytest.mark.parametrize(
    ("fact", "place"),
    [
        pytest.param({"is_leaver": True}, "world", id="end-only-kind-in-world"),
        pytest.param({"nothing_changed": True}, "world", id="nothing-changed-in-world"),
        pytest.param({"user_exists": "a@b.example"}, "world", id="user-exists-in-world"),
        pytest.param({"owns": "wiki.viewer"}, "end", id="unknown-kind"),
        pytest.param({"holds": "wiki.viewer", "lacks": "wiki.admin"}, "world", id="two-kinds"),
        pytest.param({"person": "sales.1"}, "world", id="person-only"),
        pytest.param({"holds": "wiki"}, "world", id="role-without-system"),
        pytest.param({"holds": "wiki.viewer", "person": "Sales 1"}, "world", id="bad-person"),
        pytest.param({"is_leaver": False}, "end", id="true-kind-false"),
        pytest.param({"reset_asked": "yes"}, "end", id="true-kind-text"),
        pytest.param({"user_exists": "nobody"}, "end", id="user-exists-no-email"),
        pytest.param({"open_review": ""}, "end", id="empty-value"),
    ],
)
def test_a_bad_fact_is_caught(fact: Fact, place: str) -> None:
    """Catches a kind check that accepts an end-only kind in the start world,
    an unknown kind, two kinds in one fact, a `person` with no kind, a role
    not written `<system>.<role>`, or a value of the wrong type.

    The good case is checked first, so a checker that refuses everything fails.
    """
    _check_fact_kinds([_made_up(world=[{"holds": "wiki.viewer", "person": "sales.1"}])])
    case = _made_up(world=[fact]) if place == "world" else _made_up(right_end_states=[[fact]])
    with pytest.raises(AssertionError):
        _check_fact_kinds([case])


def test_every_end_only_kind_is_allowed_at_the_end() -> None:
    """Catches a kind list that leaves out one of the six end-only kinds, or a
    shape check that refuses a good value of one of them."""
    good: list[Fact] = [
        {"is_leaver": True},
        {"user_exists": "new.joiner@nordlicht.example"},
        {"reset_asked": True},
        {"unlocked": True},
        {"open_review": "Finance"},
        {"nothing_changed": True},
        {"open_ticket": "password_reset"},
        {"in_department": "Sales", "person": "sales.2"},
    ]
    _check_fact_kinds([_made_up(right_end_states=[[fact] for fact in good])])
    assert len(START_KINDS) == 7
    assert len(END_KINDS) == 13


def test_every_check_type_is_used_at_least_5_times(cases: list[Case]) -> None:
    """Catches a check type that few cases collect labels for (under 5 uses), a
    type outside the eight, or a check with an empty `applies_when`."""
    _check_reply_checks(cases)
    assert len(CHECK_TYPES) == 8


def test_an_unknown_check_type_is_caught() -> None:
    """Catches a check validator that accepts a made-up type, an empty or
    blank `applies_when`, or a type used only 4 times.

    The good set (every type 5 times) is checked first so a validator that
    refuses everything fails; each bad set changes exactly one thing.
    """

    def with_checks(entries: list[dict[str, str]], repeat: int) -> list[Case]:
        return [_made_up(reply_checks=entries) for _ in range(repeat)]

    every = [{"check": name, "applies_when": "when it applies"} for name in sorted(CHECK_TYPES)]
    _check_reply_checks(with_checks(every, CHECK_MIN_USES))
    with pytest.raises(AssertionError):
        _check_reply_checks(with_checks(every, CHECK_MIN_USES - 1))
    with pytest.raises(AssertionError):
        _check_reply_checks(
            with_checks([*every, {"check": "is_polite", "applies_when": "always"}], 5)
        )
    for applies in ("", "   "):
        blank = [{"check": "refuses_safely", "applies_when": applies}]
        with pytest.raises(AssertionError):
            _check_reply_checks([*with_checks(every, 5), _made_up(reply_checks=blank)])


def test_scripted_only_where_exact_words_matter(cases: list[Case]) -> None:
    """Catches a case marked scripted with none of attacks, other_languages or
    tricky_phrasing in its tags, and one with such a tag left unscripted."""
    _check_scripted(cases)


@pytest.mark.parametrize("variety", sorted(VARIETIES))
def test_scripted_follows_the_tags_for_every_variety(variety: str) -> None:
    """Catches a scripted rule that is wrong for one variety: true for a tag of
    the three exact-words ones and false for each of the other eight, in both
    directions.
    """
    exact = variety in EXACT_WORDS_VARIETIES
    tags = ["job:get_me", variety]
    _check_scripted([_made_up(tags=tags, scripted=exact)])
    with pytest.raises(AssertionError):
        _check_scripted([_made_up(tags=tags, scripted=not exact)])


def test_every_job_and_variety_has_a_case(cases: list[Case]) -> None:
    """Catches a set that leaves a chat tool or one of the 11 varieties without
    a case, a job tag that names no tool of the registry, a case with a job
    and no variety (or the reverse), and an unknown tag.
    """
    _check_tags(cases)
    assert set(REGISTRY) >= CHAT_TOOLS
    assert len(VARIETIES) == 11
    assert len(CHAT_TOOLS) == len(_LOOKUPS) + len(_CHANGES)


def test_a_bad_tag_set_is_caught() -> None:
    """Catches a tag check that accepts a job that is no tool, a job that is a
    tool but not one the chat offers, a tag outside the 11 varieties, a case
    with no job, or one with no variety.

    A full good set is checked first so a checker that refuses everything
    fails. The 11 varieties are spread over cases that between them name all
    the chat tools.
    """
    tools = sorted(CHAT_TOOLS)
    varieties = sorted(VARIETIES)
    good = [
        _made_up(tags=[f"job:{tools[i % len(tools)]}", varieties[i % len(varieties)]])
        for i in range(max(len(tools), len(varieties)))
    ]
    _check_tags(good)
    for tags in (
        ["job:no_such_tool", "typos"],
        ["job:search_policy", "typos", "loud"],
        ["typos"],
        ["job:search_policy"],
    ):
        with pytest.raises(AssertionError):
            _check_tags([*good, _made_up(tags=tags)])
    with pytest.raises(AssertionError):
        _check_tags(good[:-1])
    not_chat = sorted(set(REGISTRY) - CHAT_TOOLS)
    if not_chat:
        with pytest.raises(AssertionError):
            _check_tags([*good, _made_up(tags=[f"job:{not_chat[0]}", "typos"])])


# --- siblings -----------------------------------------------------------------


def test_sibling_cases_share_the_opening_and_differ_in_world(cases: list[Case]) -> None:
    """Catches a set with fewer than 3 sibling groups, siblings that never cover
    a lacks world, a holds world and an open-request world, and two cases
    with the same opening and the same world (a copy, not a sibling).
    """
    _check_siblings(cases)


def test_a_copy_is_not_a_sibling() -> None:
    """Catches a sibling check that counts two cases with one opening and one
    world as a pair (the order of the facts must not matter), and one that
    asks for no variety of worlds.

    Three groups of two cover lacks, holds and open request as the good set.
    """
    lacks = [{"lacks": "wiki.viewer"}]
    holds = [{"holds": "wiki.viewer"}]
    asked = [{"open_request": "wiki.viewer"}]
    good = [
        _made_up(opening="one", world=lacks),
        _made_up(opening="one", world=holds),
        _made_up(opening="two", world=holds),
        _made_up(opening="two", world=asked),
        _made_up(opening="three", world=lacks),
        _made_up(opening="three", world=asked),
    ]
    _check_siblings(good)
    two = [{"holds": "wiki.viewer"}, {"lacks": "wiki.admin"}]
    flipped = list(reversed(two))
    with pytest.raises(AssertionError):
        _check_siblings(
            [*good, _made_up(opening="four", world=two), _made_up(opening="four", world=flipped)]
        )
    with pytest.raises(AssertionError):
        _check_siblings(good[:4])
    with pytest.raises(AssertionError):
        _check_siblings(
            [
                _made_up(opening="one", world=lacks),
                _made_up(opening="one", world=holds),
                _made_up(opening="two", world=lacks),
                _made_up(opening="two", world=holds),
                _made_up(opening="three", world=lacks),
                _made_up(opening="three", world=holds),
            ]
        )


def test_every_scripted_case_has_a_script(cases: list[Case]) -> None:
    """Catches a scripted case with no steps, a step with the wrong keys, an empty or
    over-long say, a yes that is not a real boolean, and an unscripted case that carries a
    script."""
    _check_scripts(cases)
    assert any(case["scripted"] for case in cases)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"script": []}, id="no-steps"),
        pytest.param({"script": [{"say": "Ja"}]}, id="missing-yes"),
        pytest.param({"script": [{"say": "Ja", "yes": "true"}]}, id="yes-as-text"),
        pytest.param({"script": [{"say": "", "yes": True}]}, id="empty-say"),
        pytest.param({"script": [{"say": "a" * 4001, "yes": True}]}, id="say-4001"),
        pytest.param({"scripted": False, "script": [{"say": "Ja", "yes": True}]}, id="unscripted"),
    ],
)
def test_a_bad_script_is_caught(mutation: Mapping[str, Any]) -> None:
    """Catches a script check that is too loose. The good scripted case (a say of 4000
    characters, the inclusive limit) passes first, so a checker that refuses everything
    fails here."""
    good = _made_up(scripted=True, script=[{"say": "a" * 4000, "yes": False}])
    _check_scripts([good])
    with pytest.raises(AssertionError):
        _check_scripts([_made_up(**{"scripted": True, **mutation})])


def test_a_case_waiting_for_a_yes_has_a_yes_step(cases: list[Case]) -> None:
    """Catches a scripted case that waits for a yes (its `confirms_if` does not start with
    "never") whose steps never say one, so the help desk is never allowed to act."""
    _check_yes_step(cases)


def test_a_missing_yes_step_is_caught() -> None:
    """Catches a yes-step check that passes any script: a waiting case with only `no` steps
    fails, a `never` case with only `no` steps and a waiting case with one yes pass."""
    steps_no = [{"say": "Hm", "yes": False}]
    waiting = "The agent names the wiki viewer role."
    _check_yes_step([_made_up(scripted=True, script=steps_no, confirms_if="never; nothing")])
    _check_yes_step(
        [
            _made_up(
                scripted=True, script=[*steps_no, {"say": "Ja", "yes": True}], confirms_if=waiting
            )
        ]
    )
    with pytest.raises(AssertionError):
        _check_yes_step([_made_up(scripted=True, script=steps_no, confirms_if=waiting)])
