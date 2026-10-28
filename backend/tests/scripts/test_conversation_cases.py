"""Checks of the conversation case file, read-only:

    backend/evals/conversations/cases.jsonl

The help desk owns 45 multi-turn cases; the companion console scores them.
Every named value is checked against the code that owns it: tenant slugs and
seat rows from ``scripts.seed``, tool names from the tool registry.

Each check is a function over parsed cases, so a made-up bad case can be fed
to it in the unhappy-path rows.

Cost: no backing service, no network. The slowest step is ``generate_tenant``
for each of the two tenants, run once each in a module-scoped fixture.
"""

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from helpdesk.mcp_server.tools.registry import REGISTRY
from scripts.seed import CUAN, NORDLICHT, TenantRows, TenantSpec, UserRow, generate_tenant


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


CASES = _repo_root() / "backend" / "evals" / "conversations" / "cases.jsonl"

CASE_COUNT = 45
CASE_FIELDS = {"id", "category", "tenant", "principal_user", "turns", "expected", "graded"}
EXPECTED_FIELDS = {"tool_calls", "confirm_turn"}
TOOL_CALL_FIELDS = {"name", "args_subset"}
GRADED_FIELDS = {"turn", "rubric"}
TURNS_RANGE = (2, 10)
GRADED_RANGE = (1, 3)
RUBRIC_MAX_WORDS = 40

CATEGORIES = frozenset(
    {
        "paraphrases",
        "typos",
        "short_answers_and_picks",
        "follow_ups",
        "changes_of_mind",
        "for_other_people",
        "other_languages",
        "off_topic",
        "attacks",
        "live_failures",
    }
)
CHANGES = frozenset(
    {
        "create_access_request",
        "revoke_access",
        "approve_request",
        "reject_request",
        "create_ticket",
        "reset_own_password",
        "unlock_own_account",
        "onboard_user",
        "transfer_user",
        "offboard_user",
        "open_access_review",
        "decide_review_item",
        "cancel_request",
    }
)
SEAT_PRINCIPALS = ("employee", "manager", "it_admin")
YES_WORDS = frozenset({"yes", "ja", "oui", "ok"})
NO_WORDS = frozenset({"no", "nope", "no thanks", "cancel"})
BANNED_IN_RUBRIC = ("score", "rate", "1 to 5")

Case = dict[str, Any]


# --- reading ------------------------------------------------------------------


def _lines() -> list[str]:
    assert CASES.is_file(), f"{CASES} does not exist"
    return CASES.read_text(encoding="utf-8").split("\n")


def _load() -> list[Case]:
    text = CASES.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    assert CASES.is_file(), f"{CASES} does not exist"
    return _load()


@pytest.fixture(scope="module")
def rows_by_slug() -> dict[str, TenantRows]:
    return {spec.slug: generate_tenant(spec) for spec in (NORDLICHT, CUAN)}


# --- the checks, each over parsed cases ---------------------------------------


def _check_turn_and_graded_counts(cases: Sequence[Case]) -> None:
    for case in cases:
        turns = case["turns"]
        assert TURNS_RANGE[0] <= len(turns) <= TURNS_RANGE[1], (
            f"{case['id']}: {len(turns)} turns, want {TURNS_RANGE[0]} to {TURNS_RANGE[1]}"
        )
        graded = case["graded"]
        assert GRADED_RANGE[0] <= len(graded) <= GRADED_RANGE[1], (
            f"{case['id']}: {len(graded)} graded entries, want 1 to 3"
        )
        for entry in graded:
            assert 1 <= entry["turn"] <= len(turns), (
                f"{case['id']}: graded turn {entry['turn']} is outside 1 to {len(turns)}"
            )


def _expected_changes(case: Case) -> set[str]:
    return {call["name"] for call in case["expected"]["tool_calls"]} & CHANGES


def _check_confirm_turn(cases: Sequence[Case]) -> None:
    for case in cases:
        confirm = case["expected"]["confirm_turn"]
        changes = _expected_changes(case)
        if confirm is None:
            assert not changes, f"{case['id']}: expects {sorted(changes)} but has no confirm_turn"
        elif changes:
            assert 1 <= confirm <= len(case["turns"]), (
                f"{case['id']}: confirm_turn {confirm} is outside 1 to {len(case['turns'])}"
            )


def _check_rubric(rubric: str) -> None:
    text = rubric.strip()
    assert text.endswith("?"), f"rubric does not end in '?': {rubric!r}"
    assert len(text.split()) <= RUBRIC_MAX_WORDS, f"rubric has over 40 words: {rubric!r}"
    assert len(re.findall(r"[.!?](?:\s|$)", text)) == 1, f"rubric is not one sentence: {rubric!r}"
    lowered = text.lower()
    for word in BANNED_IN_RUBRIC:
        assert word not in lowered, f"rubric holds {word!r}: {rubric!r}"


def _is_yes_at_confirm(case: Case) -> bool:
    confirm = case["expected"]["confirm_turn"]
    if confirm is None or not 1 <= confirm <= len(case["turns"]):
        return False
    return str(case["turns"][confirm - 1]).strip().lower().rstrip(".!") in YES_WORDS


def _is_no_after_proposal(case: Case) -> bool:
    if case["expected"]["confirm_turn"] is not None or len(case["turns"]) < 2:
        return False
    if _expected_changes(case):
        return False
    return str(case["turns"][-1]).strip().lower().rstrip(".!") in NO_WORDS


def _yes_and_no_found(cases: Sequence[Case]) -> tuple[bool, bool]:
    return any(map(_is_yes_at_confirm, cases)), any(map(_is_no_after_proposal, cases))


def _made_up(**changes: Any) -> Case:
    case: Case = {
        "id": "conv-001",
        "category": "paraphrases",
        "tenant": "nordlicht",
        "principal_user": "seed:production.employee.0",
        "turns": ["one", "two"],
        "expected": {"tool_calls": [], "confirm_turn": None},
        "graded": [{"turn": 1, "rubric": "Does the reply say hello?"}],
    }
    case.update(changes)
    return case


# --- the file -----------------------------------------------------------------


def test_the_case_file_holds_45_parsable_cases() -> None:
    """Catches a file with 44 or 46 cases, a blank line (the console would read an
    empty case), a line that is not JSON, and a case with a missing, extra or
    renamed field at any of the four levels.

    The line count is taken from the raw text, so a trailing blank line counts
    as a line.
    """
    lines = _lines()
    assert lines[-1] == "", "the file must end with one newline"
    lines = lines[:-1]
    assert len(lines) == CASE_COUNT
    assert all(line.strip() for line in lines), "a blank line sits in the file"
    for line in lines:
        case = json.loads(line)
        assert set(case) == CASE_FIELDS, f"{case.get('id')}: fields {sorted(case)}"
        assert set(case["expected"]) == EXPECTED_FIELDS
        for call in case["expected"]["tool_calls"]:
            assert set(call) == TOOL_CALL_FIELDS
            assert isinstance(call["args_subset"], dict)
        for entry in case["graded"]:
            assert set(entry) == GRADED_FIELDS
        assert all(isinstance(turn, str) and turn.strip() for turn in case["turns"])
        assert isinstance(case["expected"]["confirm_turn"], int | None)


def test_ids_run_conv_001_to_conv_045_in_order(cases: list[Case]) -> None:
    """Catches a repeated id, a gap, an id out of order, or a number that starts
    at 000 or stops short of 045.

    The ids are compared with the exact list, so no looser rule passes.
    """
    assert [case["id"] for case in cases] == [f"conv-{n:03d}" for n in range(1, CASE_COUNT + 1)]


def test_turn_and_graded_counts_stay_in_range() -> None:
    """Catches a case with 1 or 11 turns, no graded reply or four, or a graded
    `turn` that points past the last turn or before the first. The checker is
    also shown made-up bad cases and must refuse each.

    The made-up cases break one limit each, so a checker that tests only
    one of them fails the other.
    """
    with pytest.raises(AssertionError):
        _check_turn_and_graded_counts([_made_up(turns=[f"t{n}" for n in range(11)])])
    with pytest.raises(AssertionError):
        _check_turn_and_graded_counts([_made_up(turns=["only one"])])
    four = [{"turn": 1, "rubric": "Does it?"}] * 4
    with pytest.raises(AssertionError):
        _check_turn_and_graded_counts([_made_up(graded=four)])
    with pytest.raises(AssertionError):
        _check_turn_and_graded_counts([_made_up(graded=[])])
    with pytest.raises(AssertionError):
        _check_turn_and_graded_counts([_made_up(graded=[{"turn": 3, "rubric": "Does it?"}])])
    with pytest.raises(AssertionError):
        _check_turn_and_graded_counts([_made_up(graded=[{"turn": 0, "rubric": "Does it?"}])])
    # The inclusive limits pass.
    _check_turn_and_graded_counts(
        [_made_up(turns=[f"t{n}" for n in range(10)], graded=[{"turn": 10, "rubric": "x?"}] * 3)]
    )
    _check_turn_and_graded_counts([_made_up(turns=["a", "b"])])

    _check_turn_and_graded_counts(_load())


def test_no_change_is_expected_before_the_yes() -> None:
    """Catches a case that expects a change with `confirm_turn` null or past its
    last turn (so nothing says when the change may happen), and a null case that
    still expects a change. The checker is shown made-up cases that do each.

    The made-up rows use a real change name, so a checker that looks for
    any tool name rather than the 12 changes would refuse the read-only row.
    """
    change = {"name": "create_ticket", "args_subset": {}}
    with pytest.raises(AssertionError):
        _check_confirm_turn([_made_up(expected={"tool_calls": [change], "confirm_turn": None})])
    with pytest.raises(AssertionError):
        _check_confirm_turn([_made_up(expected={"tool_calls": [change], "confirm_turn": 3})])
    with pytest.raises(AssertionError):
        _check_confirm_turn([_made_up(expected={"tool_calls": [change], "confirm_turn": 0})])
    _check_confirm_turn([_made_up(expected={"tool_calls": [change], "confirm_turn": 2})])
    read = {"name": "search_policy", "args_subset": {}}
    _check_confirm_turn([_made_up(expected={"tool_calls": [read], "confirm_turn": None})])

    _check_confirm_turn(_load())


def test_every_category_has_a_case(cases: list[Case]) -> None:
    """Catches a missing category (a kind of message never played), and a
    misspelt or extra one that the console would not know.

    The set of categories in the file is compared with the exact ten.
    """
    counts = Counter(case["category"] for case in cases)
    assert set(counts) == CATEGORIES
    assert all(count >= 1 for count in counts.values())


def test_every_chat_change_is_expected_somewhere(cases: list[Case]) -> None:
    """Catches a chat change no case expects (so a whole kind of action is never
    tested), and a name in `expected.tool_calls` that is not a tool, which the
    console could never match.

    Names are compared with the registry, not with a list kept here.
    """
    named = {call["name"] for case in cases for call in case["expected"]["tool_calls"]}
    assert named >= CHANGES, f"changes no case expects: {sorted(CHANGES - named)}"
    assert named <= set(REGISTRY), f"names not in the registry: {sorted(named - set(REGISTRY))}"
    assert set(REGISTRY) >= CHANGES, "a change named here is not a registered tool"


def _resolve_seat(rows: TenantRows, principal_user: str) -> UserRow:
    """`seed:<department lowercased>.<principal>.<index>` to the seeded user."""
    assert principal_user.startswith("seed:"), principal_user
    parts = principal_user.removeprefix("seed:").split(".")
    assert len(parts) == 3, f"{principal_user!r} is not three dotted parts"
    department_part, principal, index_part = parts
    names = [d for d in rows.spec.departments if d.lower() == department_part]
    assert names, f"{principal_user!r}: no department of {rows.spec.slug!r} matches"
    seats = rows.users_by_department[names[0]]
    index = int(index_part)
    assert index < len(seats), f"{principal_user!r}: index {index} is past the end"
    assert principal in seats[index].principals, (
        f"{principal_user!r}: that seat holds {sorted(seats[index].principals)}"
    )
    return seats[index]


def test_both_tenants_and_three_seats_appear(
    cases: list[Case], rows_by_slug: dict[str, TenantRows]
) -> None:
    """Catches a set that covers one tenant only, one that never plays the manager
    or IT admin seat, a tenant name the seed does not know, and a seat that does
    not exist or does not hold the principal its name claims.

    Every seat is resolved against the seed's rows in memory, so a seat index
    past the end of its department fails.
    """
    assert {case["tenant"] for case in cases} == {NORDLICHT.slug, CUAN.slug}
    seen: set[str] = set()
    for case in cases:
        assert case["tenant"] in rows_by_slug, f"{case['id']}: unknown tenant"
        _resolve_seat(rows_by_slug[case["tenant"]], case["principal_user"])
        seen.add(case["principal_user"].split(".")[1])
    assert set(SEAT_PRINCIPALS) <= seen, f"seats never played: {set(SEAT_PRINCIPALS) - seen}"


def test_a_yes_and_a_no_at_a_confirmation() -> None:
    """Catches a set where nobody ever confirms (no case's `confirm_turn` message
    is a yes), or where nobody ever declines (no case ends on a no after a
    proposed change with `confirm_turn` null).

    The detector is shown made-up cases first: a yes at the confirm turn counts,
    a no at the end counts only with `confirm_turn` null and no change expected,
    so a detector that just looks for the word anywhere would be caught.
    """
    yes_case = _made_up(
        turns=["file it", "yes"],
        expected={"tool_calls": [{"name": "create_ticket", "args_subset": {}}], "confirm_turn": 2},
    )
    no_case = _made_up(turns=["file it", "no"])
    assert _yes_and_no_found([yes_case]) == (True, False)
    assert _yes_and_no_found([no_case]) == (False, True)
    assert _yes_and_no_found([_made_up(turns=["no", "file it"])]) == (False, False)
    wrong_turn = _made_up(
        turns=["yes", "file it"],
        expected={"tool_calls": [{"name": "create_ticket", "args_subset": {}}], "confirm_turn": 2},
    )
    assert _yes_and_no_found([wrong_turn]) == (False, False)

    yes_seen, no_seen = _yes_and_no_found(_load())
    assert yes_seen, "no case confirms with a yes at its confirm_turn"
    assert no_seen, "no case ends with a no after a proposed change"


def _spec_roles(spec: TenantSpec) -> set[str]:
    return {f"{role.system}.{role.slug}" for role in spec.roles}


def test_every_role_in_args_is_a_seeded_role(
    cases: list[Case], rows_by_slug: dict[str, TenantRows]
) -> None:
    """Catches an `args_subset.role` that is a made-up role, a role of the other
    tenant, or one not written `<system slug>.<role slug>` (such as a display name).

    The role is compared with the roles of that case's own tenant, so a role
    that exists only in the other tenant fails.
    """
    checked = 0
    for case in cases:
        roles = _spec_roles(rows_by_slug[case["tenant"]].spec)
        for call in case["expected"]["tool_calls"]:
            role = call["args_subset"].get("role")
            if role is None:
                continue
            checked += 1
            assert role in roles, f"{case['id']}: {role!r} is no role of {case['tenant']}"
    assert checked >= 1, "no case names a role at all"


def test_every_rubric_is_one_pass_or_fail_question() -> None:
    """Catches a rubric that is a statement, two sentences, over 40 words, or a
    1-to-5 rating ("score", "rate", "1 to 5"). The checker is shown made-up
    rubrics that each break one rule and must refuse them.

    Each made-up rubric breaks exactly one rule, so a checker that tests
    only some of them fails on the others. A 40-word rubric passes and a
    41-word one is refused.
    """
    long_one = "Does the reply " + " ".join(["really"] * 37) + " help?"
    bad: Mapping[str, str] = {
        "statement": "The reply names the role.",
        "two sentences": "It names the role. Does it ask for days?",
        "too long": long_one,
        "score": "Does the reply earn a good score?",
        "rate": "How would you rate the reply?",
        "one to five": "Is the reply a 1 to 5 fit?",
    }
    for rubric in bad.values():
        with pytest.raises(AssertionError):
            _check_rubric(rubric)
    _check_rubric("Does the reply name the role in plain words?")
    _check_rubric("Does the reply " + " ".join(["really"] * 36) + " help?")

    seen = 0
    for case in _load():
        for entry in case["graded"]:
            _check_rubric(entry["rubric"])
            seen += 1
    assert seen >= CASE_COUNT


# --- the guardrail cases -------------------------------------------------------

GUARDRAIL_SLOTS: Mapping[str, tuple[str, ...]] = {
    "ids": ("conv-039", "conv-040"),
    "denial": ("conv-041", "conv-042"),
    "draft": ("conv-043", "conv-044"),
    "promises": ("conv-045",),
}
"""Which case holds which rule: two each for ids, denial and draft, one for
promises, in the order listed above. The case format has no tag field
(the field set is fixed), so the slot is the id and the rule is then read from
the case's own words below."""

_NO_OPENERS = ("no", "nope", "same, but", "not that")
_RUBRIC_SIGNS: Mapping[str, re.Pattern[str]] = {
    "ids": re.compile(r"\bids?\b|look(?:s|ed)?[ -]?up|look it up", re.IGNORECASE),
    "draft": re.compile(r"again|already|repeat", re.IGNORECASE),
    "promises": re.compile(r"promis", re.IGNORECASE),
}


def _shows_rule(case: Case, rule: str) -> bool:
    """Whether the case's own words are about `rule`. Denial reads the turns:
    a later turn that opens with a no or a "same, but" answers a proposal.
    The others read the graded rubrics."""
    if rule == "denial":
        later = [str(turn).strip().lower() for turn in case["turns"][1:]]
        return any(turn.startswith(_NO_OPENERS) for turn in later)
    rubrics = " ".join(entry["rubric"] for entry in case["graded"])
    return _RUBRIC_SIGNS[rule].search(rubrics) is not None


def test_the_new_guardrail_cases_cover_each_rule(cases: list[Case]) -> None:
    """Catches a change that adds seven cases but leaves a rule with fewer than
    its share (two for ids, two for denial, two for draft, one for promises),
    that puts a case in the wrong slot (a denial case that never answers a
    proposal with a no), or that leaves conv-039 to conv-045 missing.

    The detector is shown made-up cases first: a denial needs a LATER turn that
    opens with a no (an opening "no" in turn 1 is nothing to deny), and a
    rubric without the rule's sign is refused, so a detector that says yes to
    everything fails here.
    """
    plain = _made_up(turns=["file it", "yes"])
    assert not _shows_rule(plain, "denial")
    assert not _shows_rule(_made_up(turns=["no thanks", "yes"]), "denial")
    assert _shows_rule(_made_up(turns=["file it", "No, make it read-only"]), "denial")
    assert _shows_rule(_made_up(turns=["file it", "same, but for Dana"]), "denial")
    for rule in ("ids", "draft", "promises"):
        assert not _shows_rule(plain, rule)
    assert _shows_rule(
        _made_up(graded=[{"turn": 1, "rubric": "Does the reply avoid promising future work?"}]),
        "promises",
    )

    by_id = {case["id"]: case for case in cases}
    assert [len(ids) for ids in GUARDRAIL_SLOTS.values()] == [2, 2, 2, 1]
    for rule, ids in GUARDRAIL_SLOTS.items():
        for case_id in ids:
            assert case_id in by_id, f"{case_id} (the {rule} case) is missing"
            assert _shows_rule(by_id[case_id], rule), f"{case_id} does not read as a {rule} case"
