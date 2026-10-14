"""`resolve_entities`: the user's wording turned into directory rows (§10.1 step 4).

The candidate list is read through the tenant-scoped repository, so it is the
caller's OWN tenant's systems and roles and nothing else — plan §5's third
risk, pinned by
`test_agent_turn.py::test_a_candidate_in_another_tenant_is_not_a_candidate`,
whose other tenant owns a system with the same slug.

The read stays here and the ranking stays in `matching.py` (decision G): the
matcher takes candidates as data, which is what lets every ordering and cutoff
case run with no database at all.

A system or role is NEVER guessed. One candidate resolves; two or more ask,
offering at most three; none asks a clarifying question instead. The branch is
computed by `gates.candidate_outcome` — pure code, no graph — and recorded as
`outcome` for the conditional edge to read (the design).

`system` and `role` are resolved independently and the STRICTER answer wins:
an ambiguous role does not become a resolved turn because the system was
unambiguous. Roles are matched across the tenant rather than within the
resolved system, because a user who names a role and not a system is exactly
the case a system-first lookup cannot answer.
"""

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
"""What the turn asks when the term matched no candidate at all.

Named separately from the extraction gate's question because the two say
different things: that one means "I am not sure what you want", this one means
"I know what you want and no such thing exists in your company".
"""

DISAMBIGUATION_PREFIX = "Which one do you mean?"


def option_label(candidate: Candidate) -> str:
    """One offered option: the slug first, then the display name.

    The slug leads because two rows may share a display name — that is exactly
    what makes a turn ambiguous — and options that read identically are not a
    question anyone can answer. Pinned by
    `test_agent_turn.py::test_two_matching_systems_ask_for_disambiguation`.
    """
    return f"{candidate.slug} ({candidate.name})"


async def resolve_entities(state: TurnState, deps: TurnDeps) -> TurnState:
    """Resolve the named system and role, or ask which one was meant."""
    extraction = state.extraction
    if extraction is None:  # pragma: no cover - the graph never routes here
        return state

    systems = _candidates(await Repository(deps.db, System).list())
    roles = _candidates(await Repository(deps.db, Role).list())

    system_term = (extraction.entities.system or "").strip()
    role_term = (extraction.entities.role or "").strip()

    system_matches = matching.match_candidates(system_term, systems) if system_term else ()
    role_matches = matching.match_candidates(role_term, roles) if role_term else ()

    for term, matches in ((system_term, system_matches), (role_term, role_matches)):
        if not term:
            continue
        outcome = gates.candidate_outcome(len(matches))
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
    return state.model_copy(
        update={
            "outcome": TurnOutcome.CONTINUE,
            "resolved": ResolvedEntities(
                system_id=resolved_system.id if resolved_system else None,
                system_slug=resolved_system.slug if resolved_system else None,
                role_id=resolved_role.id if resolved_role else None,
                role_slug=resolved_role.slug if resolved_role else None,
            ),
        }
    )


def _candidates(rows: Sequence[System] | Sequence[Role]) -> tuple[Candidate, ...]:
    """Directory rows reduced to what the pure matcher takes.

    ORM instances stop here: `matching.py` is framework-free by decision G,
    and handing it a row would drag a session into every test of the ranking.
    """
    return tuple(Candidate(id=row.id, slug=row.slug, name=row.name) for row in rows)
