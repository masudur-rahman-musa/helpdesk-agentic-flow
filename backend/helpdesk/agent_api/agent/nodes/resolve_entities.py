"""`resolve_entities`: the user's wording turned into directory rows.

Candidates are read through the tenant-scoped repository, so only the
caller's own tenant's systems and roles can match. A system or role is never
guessed: one match resolves, several ask, none asks a clarifying question,
or, right after one, answers with no tools.
System and role are resolved on their own and the stricter answer wins. Roles
are matched across the tenant when no system is named, and only inside the
system when one system was named: "wiki editor" must not offer every editor.
The resolved role's siblings on its system are read from the same rows, so
the reply step can offer another role without a second read.
"""

import logging
import uuid
from collections.abc import Sequence

from helpdesk.agent_api.agent import gates, matching
from helpdesk.agent_api.agent.matching import Candidate
from helpdesk.agent_api.agent.state import ResolvedEntities, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
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

    logger.info(
        "resolve_entities terms system=%r role=%r matches system=%d role=%d",
        system_term,
        role_term,
        len(system_matches),
        len(role_matches),
    )
    for term, matches in ((system_term, system_matches), (role_term, role_matches)):
        if not term:
            continue
        outcome = gates.candidate_outcome(len(matches))
        if outcome is TurnOutcome.CLARIFY and state.asked_before:
            return state.model_copy(update={"outcome": TurnOutcome.ANSWER, "best_reading": True})
        if outcome is TurnOutcome.CLARIFY:
            return state.model_copy(update={"outcome": outcome, "reply": NOTHING_MATCHED_QUESTION})
        if outcome is TurnOutcome.DISAMBIGUATE:
            options = tuple(option_label(candidate) for candidate in gates.capped_options(matches))
            return state.model_copy(
                update={
                    "outcome": outcome,
                    "options": options,
                    "reply": f"{DISAMBIGUATION_PREFIX} {'; '.join(options)}",
                }
            )

    resolved_system = system_matches[0] if len(system_matches) == 1 else None
    resolved_role = role_matches[0] if len(role_matches) == 1 else None
    if resolved_system is None and resolved_role is not None and not system_term:
        owner_id = next(r.system_id for r in all_roles if r.id == resolved_role.id)
        resolved_system = next((s for s in systems if s.id == owner_id), None)
    return state.model_copy(
        update={
            "outcome": TurnOutcome.CONTINUE,
            "resolved": ResolvedEntities(
                system_id=resolved_system.id if resolved_system else None,
                system_slug=resolved_system.slug if resolved_system else None,
                system_name=resolved_system.name if resolved_system else None,
                role_id=resolved_role.id if resolved_role else None,
                role_slug=resolved_role.slug if resolved_role else None,
                role_name=resolved_role.name if resolved_role else None,
                other_role_names=other_role_names(all_roles, resolved_role),
            ),
        }
    )


def roles_in_system(roles: Sequence[Role], system_matches: Sequence[Candidate]) -> Sequence[Role]:
    """The roles of the one system the user named; all roles when no single
    system was named (none, or several that are asked about first)."""
    if len(system_matches) != 1:
        return roles
    system_id: uuid.UUID = system_matches[0].id
    return [role for role in roles if role.system_id == system_id]


def other_role_names(roles: Sequence[Role], resolved: Candidate | None) -> tuple[str, ...]:
    """The names of the other roles on the resolved role's system, sorted by
    slug; empty when no role was resolved. Pinned by
    `test_other_role_names_leave_out_the_asked_role`."""
    if resolved is None:
        return ()
    owner_id = next(role.system_id for role in roles if role.id == resolved.id)
    siblings = [role for role in roles if role.system_id == owner_id and role.id != resolved.id]
    return tuple(role.name for role in sorted(siblings, key=lambda role: role.slug))


def _candidates(rows: Sequence[System] | Sequence[Role]) -> tuple[Candidate, ...]:
    """Directory rows reduced to what the pure matcher takes."""
    return tuple(Candidate(id=row.id, slug=row.slug, name=row.name) for row in rows)
