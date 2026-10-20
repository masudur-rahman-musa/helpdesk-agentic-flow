"""`resolve_entities`: the user's wording turned into directory rows.

Candidates are read through the tenant-scoped repository, so only the
caller's own tenant's systems and roles can match. A system or role is never
guessed: one match resolves, several ask, and none offers the closest roles
by meaning (`suggest_roles`) or, when none is close or the search fails, asks
for the exact name. Each is asked once in a row: right after a suggestion a
miss asks for the exact name, and right after that it answers with no tools.
Any other question before does not stop a suggestion.
System and role are resolved on their own and the stricter answer wins. Roles
are matched across the tenant when no system is named, and only inside the
system when one system was named: "wiki editor" must not offer every editor.
The resolved role's siblings on its system are read from the same rows, so
the reply step can offer another role without a second read.

A request is checked for its details only once the role is found, so the
person is asked how long and why about a real role. Right after the details
question a still missing reason is asked for alone, by role name, and a still
missing length takes the role's default (`test_question_kinds.py`).
"""

import logging
import uuid
from collections.abc import Sequence

from helpdesk.agent_api.agent import gates, matching
from helpdesk.agent_api.agent.clarify_cap import QuestionKind, details_question, reason_question
from helpdesk.agent_api.agent.matching import Candidate
from helpdesk.agent_api.agent.state import Intent, ResolvedEntities, TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import (
    SUGGEST_ROLES,
    SuggestedRole,
    SuggestRolesArgs,
    SuggestRolesResult,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Role, System

NOTHING_MATCHED_QUESTION = (
    "I could not find anything by that name here. Could you give the exact "
    "name as it appears in your access list?"
)
"""Asked when nothing matched. Unlike the extraction question, this means
"I know what you want, but nothing by that name exists here"."""

logger = logging.getLogger(__name__)

DISAMBIGUATION_PREFIX = "Which one do you mean?"
SUGGESTION_PREFIX = "Did you mean one of these?"


def suggestion_label(role: SuggestedRole) -> str:
    """One suggested role: the full `system.role` slug first, then its names."""
    return f"{role.system_slug}.{role.role_slug} ({role.system_name} / {role.role_name})"


async def _suggest_or_ask(state: TurnState, deps: TurnDeps, words: str) -> TurnState:
    """Nothing matched the typed words: offer the closest roles by meaning, or
    ask for the exact name when none is close or the search fails. A failed
    search is never a failed turn: the person can still type the name."""
    try:
        payload = await deps.tools.call_tool(SUGGEST_ROLES, SuggestRolesArgs(query=words))
        roles = SuggestRolesResult.model_validate(dict(payload)).roles
    except ToolError:
        roles = []
    if not roles:
        return _ask_exact_name(state)
    options = tuple(suggestion_label(role) for role in gates.capped_options(roles))
    return state.model_copy(
        update={
            "outcome": TurnOutcome.DISAMBIGUATE,
            "options": options,
            "reply": f"{SUGGESTION_PREFIX} {'; '.join(options)}",
            "question_kind": QuestionKind.SUGGEST,
        }
    )


def _ask_exact_name(state: TurnState) -> TurnState:
    """Ask for the exact name, once: the next miss answers with no tools."""
    return state.model_copy(
        update={
            "outcome": TurnOutcome.CLARIFY,
            "reply": NOTHING_MATCHED_QUESTION,
            "question_kind": QuestionKind.NAME,
        }
    )


async def _on_a_miss(state: TurnState, deps: TurnDeps, words: str) -> TurnState:
    """Nothing matched: suggest, then the exact-name question, then stop."""
    if state.asked_kind is QuestionKind.NAME:
        return state.model_copy(update={"outcome": TurnOutcome.ANSWER, "best_reading": True})
    if state.asked_kind is QuestionKind.SUGGEST:
        return _ask_exact_name(state)
    return await _suggest_or_ask(state, deps, words)


def _ask_details(state: TurnState, resolved: ResolvedEntities) -> TurnState | None:
    """The details question for a request whose role is found, or `None` when
    nothing is missing. Right after the details question, only a missing
    reason is asked again, alone and by name."""
    extraction = state.extraction
    if extraction is None or extraction.intent is not Intent.REQUEST_ACCESS:
        return None
    missing = gates.missing_required_entities(extraction) - {"system"}
    if state.asked_kind is QuestionKind.DETAILS:
        if not missing:
            return None
        reply = reason_question(resolved.role_name or resolved.system_name or "this access")
    elif missing or gates.length_missing(extraction):
        reply = details_question(missing, length_missing=gates.length_missing(extraction))
    else:
        return None
    return state.model_copy(
        update={
            "outcome": TurnOutcome.CLARIFY,
            "reply": reply,
            "resolved": resolved,
            "question_kind": QuestionKind.DETAILS,
        }
    )


def option_label(candidate: Candidate) -> str:
    """One offered option, slug first: two rows can share a display name, and
    identical options are no question at all."""
    return f"{candidate.slug} ({candidate.name})"


async def resolve_entities(state: TurnState, deps: TurnDeps) -> TurnState:
    """Resolve the named system and role, or ask which one was meant."""
    extraction = state.extraction
    if extraction is None:  # pragma: no cover - the graph never routes here
        return state

    systems = _candidates(await Repository(deps.db, System).list())
    all_roles = await Repository(deps.db, Role).list()

    system_term = (extraction.entities.system or "").strip()
    role_term = (extraction.entities.role or "").strip()

    system_matches = matching.match_candidates(system_term, systems) if system_term else ()
    roles = _candidates(roles_in_system(all_roles, system_matches))
    role_matches = matching.match_candidates(role_term, roles) if role_term else ()

    # The model often puts a whole "wiki editor" into the system field. Role
    # names carry the system ("wiki.editor"), so try the phrase as a role.
    if system_term and not system_matches and not role_term:
        role_matches = matching.match_candidates(system_term, _candidates(all_roles))
        if role_matches:
            role_term, system_term = system_term, ""
    # "payroll read" also names the system `payroll` by its words. Inside that
    # one system, the extra words may still name a role.
    elif (
        len(system_matches) == 1
        and not role_term
        and not _names_exactly(system_term, system_matches[0])
    ):
        role_matches = matching.match_candidates(system_term, roles)
        if role_matches:
            role_term = system_term

    logger.info(
        "resolve_entities terms system=%r role=%r matches system=%d role=%d",
        system_term,
        role_term,
        len(system_matches),
        len(role_matches),
    )
    unmatched = " ".join(
        term
        for term, matches in ((system_term, system_matches), (role_term, role_matches))
        if term and not matches
    )
    for term, matches in ((system_term, system_matches), (role_term, role_matches)):
        if not term:
            continue
        outcome = gates.candidate_outcome(len(matches))
        if outcome is TurnOutcome.CLARIFY:
            return await _on_a_miss(state, deps, unmatched)
        if outcome is TurnOutcome.DISAMBIGUATE:
            options = tuple(option_label(candidate) for candidate in gates.capped_options(matches))
            return state.model_copy(
                update={
                    "outcome": outcome,
                    "options": options,
                    "reply": f"{DISAMBIGUATION_PREFIX} {'; '.join(options)}",
                    "question_kind": QuestionKind.OTHER,
                }
            )

    resolved_system = system_matches[0] if len(system_matches) == 1 else None
    resolved_role = role_matches[0] if len(role_matches) == 1 else None
    if resolved_system is None and resolved_role is not None and not system_term:
        owner_id = next(r.system_id for r in all_roles if r.id == resolved_role.id)
        resolved_system = next((s for s in systems if s.id == owner_id), None)
    resolved = ResolvedEntities(
        system_id=resolved_system.id if resolved_system else None,
        system_slug=resolved_system.slug if resolved_system else None,
        system_name=resolved_system.name if resolved_system else None,
        role_id=resolved_role.id if resolved_role else None,
        role_slug=resolved_role.slug if resolved_role else None,
        role_name=resolved_role.name if resolved_role else None,
        other_role_names=other_role_names(all_roles, resolved_role),
    )
    asked = _ask_details(state, resolved)
    if asked is not None:
        return asked
    return state.model_copy(update={"outcome": TurnOutcome.CONTINUE, "resolved": resolved})


def roles_in_system(roles: Sequence[Role], system_matches: Sequence[Candidate]) -> Sequence[Role]:
    """The roles of the one system the user named; all roles when no single
    system was named (none, or several that are asked about first)."""
    if len(system_matches) != 1:
        return roles
    system_id: uuid.UUID = system_matches[0].id
    return [role for role in roles if role.system_id == system_id]


def other_role_names(roles: Sequence[Role], resolved: Candidate | None) -> tuple[str, ...]:
    """The names of the other roles on the resolved role's system, sorted by
    slug; empty when no role was resolved."""
    if resolved is None:
        return ()
    owner_id = next(role.system_id for role in roles if role.id == resolved.id)
    siblings = [role for role in roles if role.system_id == owner_id and role.id != resolved.id]
    return tuple(role.name for role in sorted(siblings, key=lambda role: role.slug))


def _names_exactly(term: str, candidate: Candidate) -> bool:
    """Whether `term` is the candidate's own name, with nothing added."""
    return matching.normalise(term) in matching.aliases_for(candidate)


def _candidates(rows: Sequence[System] | Sequence[Role]) -> tuple[Candidate, ...]:
    """Directory rows reduced to what the pure matcher takes."""
    return tuple(Candidate(id=row.id, slug=row.slug, name=row.name) for row in rows)
