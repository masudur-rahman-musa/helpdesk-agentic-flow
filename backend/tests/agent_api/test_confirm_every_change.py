"""Every change gets a fixed-words summary before it runs.

Surface: in `confirmation.py`, `change_summary(calls, results, resolved)`
(code-written text per tool, ending `CHANGE_LINE`). That the summary is held
and runs once on a yes is the loop's to show (`test_agent_loop.py`); here only
the words are pinned.

Cost and isolation. Pure: no Postgres, no socket, no gateway.
"""

import uuid
from typing import Any

from helpdesk.agent_api.agent.confirmation import change_summary, confirm_facts
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, ValidatedCall

RESET = "reset_own_password"
UNLOCK = "unlock_own_account"
TICKET = "create_ticket"
LIST_APPROVALS = "list_pending_approvals"
APPROVE = "approve_request"
REVOKE = "revoke_access"
LIST_MY_ACCESS = "list_my_access"
LIST_PERSON_ACCESS = "list_person_access"

REQ_6 = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000006")
REQ_13 = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000013")
REQ_19 = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000019")

LISTED = (
    (REQ_6, "REQ-0006", "engineer"),
    (REQ_13, "REQ-0013", "analyst"),
    (REQ_19, "REQ-0019", "admin"),
)
"""The three requests waiting on the fake manager: id, number, role slug."""

CHANGE_LINE = "Say yes to go ahead, or no to stop."
RESET_SUMMARY = f"I'll reset your password. {CHANGE_LINE}"
UNLOCK_SUMMARY = f"I'll unlock your account. {CHANGE_LINE}"


def approval_results() -> tuple[ToolResult, ...]:
    """What `list_pending_approvals` answered, as a turn holds it: three
    requests, as the server shapes them."""
    rows = [
        {
            "request_id": str(request_id),
            "request_number": number,
            "target_display_name": f"Fake Person {number}",
            "system_name": "Data Warehouse",
            "system_slug": "data-warehouse",
            "role_name": f"data-warehouse.{slug}",
            "role_slug": slug,
            "status": "pending",
        }
        for request_id, number, slug in LISTED
    ]
    return (ToolResult(tool=LIST_APPROVALS, ok=True, payload={"approvals": rows}),)


# --- the fixed summaries, one per tool (direct) -----------------------------------------


def _call(tool: str, **arguments: Any) -> ValidatedCall:
    return ValidatedCall(tool=tool, arguments={"idempotency_key": "fake-key", **arguments})


def test_summaries_are_the_exact_fixed_words() -> None:
    """Catches a summary reworded, or missing the closing line: each tool's
    summary is pinned word for word, including the ticket title, because the
    person says yes to exactly this text."""
    no_results: tuple[ToolResult, ...] = ()
    resolved = ResolvedEntities()

    assert change_summary([_call(RESET)], no_results, resolved) == RESET_SUMMARY
    assert change_summary([_call(UNLOCK)], no_results, resolved) == UNLOCK_SUMMARY
    ticket = _call(TICKET, kind="general", title="Remove VPN access", body="Fake.")
    assert (
        change_summary([ticket], no_results, resolved)
        == f"I'll open a ticket: Remove VPN access. {CHANGE_LINE}"
    )


def test_the_approval_summary_counts_and_names_each_listed_request() -> None:
    """Catches a count taken from the listing instead of the held calls: two
    approvals are held out of three listed, so the summary must say 2 and name
    only REQ-0006 and REQ-0019."""
    calls = [
        _call(APPROVE, request_id=str(REQ_6)),
        _call(APPROVE, request_id=str(REQ_19)),
    ]

    summary = change_summary(calls, approval_results(), ResolvedEntities())

    assert summary.startswith("I'll approve these 2 requests: ")
    assert "REQ-0006 Data Warehouse engineer" in summary
    assert "REQ-0019 Data Warehouse admin" in summary
    assert "REQ-0013" not in summary
    assert summary.endswith(CHANGE_LINE)


# --- a held revoke names whose access it is ----------------------------------------------

GRANT_ID = uuid.UUID("bbbbbbbb-0000-5000-8000-000000000001")
OTHER_GRANT_ID = uuid.UUID("bbbbbbbb-0000-5000-8000-000000000002")
NATALIE = "Natalie Deegan"


def _access_result(tool: str) -> tuple[ToolResult, ...]:
    """What an access lookup answered: two live grants, as the server shapes them."""
    rows = [
        {
            "grant_id": str(GRANT_ID),
            "role_slug": "developer",
            "role_name": "github.developer",
            "privilege": "write",
            "system_slug": "github",
            "system_name": "GitHub",
            "granted_at": "2026-01-01T09:00:00Z",
            "expires_at": None,
        },
        {
            "grant_id": str(OTHER_GRANT_ID),
            "role_slug": "viewer",
            "role_name": "crm.viewer",
            "privilege": "read",
            "system_slug": "crm",
            "system_name": "CRM",
            "granted_at": "2026-01-01T09:00:00Z",
            "expires_at": None,
        },
    ]
    return (ToolResult(tool=tool, ok=True, payload={"grants": rows}),)


def test_a_revoke_for_someone_else_names_them() -> None:
    """Catches a summary that still says "your" for a colleague's grant, or one
    that cannot name the role because it reads grant names only from
    `list_my_access` rows: the grant here comes from `list_person_access`, so
    the role is found only if that listing is read too. The person is passed
    as the name the turn found for them."""
    summary = change_summary(
        [_call(REVOKE, grant_id=str(GRANT_ID))],
        _access_result(LIST_PERSON_ACCESS),
        ResolvedEntities(),
        NATALIE,
    )

    assert summary == f"I'll remove {NATALIE}'s access to GitHub developer. {CHANGE_LINE}"


def test_a_revoke_of_own_access_still_says_your() -> None:
    """Catches a change that puts a name (or "None's") into the line for the
    person's own grant: with no other person and the grant listed by
    `list_my_access`, the old words stay exactly as they were."""
    summary = change_summary(
        [_call(REVOKE, grant_id=str(OTHER_GRANT_ID))],
        _access_result(LIST_MY_ACCESS),
        ResolvedEntities(),
    )

    assert summary == f"I'll remove your access to CRM viewer. {CHANGE_LINE}"


def test_the_question_must_show_a_role_listed_by_the_person_lookup() -> None:
    """Catches `confirm_facts` that reads grant names from `list_my_access`
    only: for a grant listed by `list_person_access` the role would be no
    fact, and a written question that dropped it would pass."""
    facts = confirm_facts(
        [_call(REVOKE, grant_id=str(GRANT_ID))],
        _access_result(LIST_PERSON_ACCESS),
        ResolvedEntities(),
        NATALIE,
    )

    assert [fact.text for fact in facts if "GitHub" in fact.text] == ["GitHub developer"]
