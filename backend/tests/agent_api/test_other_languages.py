"""Other languages: reject reasons kept as given, names matched without accents.

Cost and isolation. Pure: plain values and one tiny fake directory. No
Postgres, no gateway, no socket, no waiting.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence

import pytest

from helpdesk.agent_api.agent.loop_tools import DepartmentRow, PersonRow, find_person
from helpdesk.agent_api.agent.matching import normalise
from helpdesk.agent_api.agent.tool_args import RejectRequestArgs

# --- a reject's reason ------------------------------------------------------------


def _reject(reason: str) -> RejectRequestArgs:
    return RejectRequestArgs(
        request_id=uuid.UUID(int=7), reason=reason, idempotency_key="fake-key-1"
    )


@pytest.mark.parametrize(
    "reason", ["no budget left this quarter", "pas de budget", "ja, aber nein"]
)
def test_a_reject_with_a_real_reason_is_accepted(reason: str) -> None:
    """Catches a reason check that refuses anything starting with or holding a
    yes or no word: only a reason made of nothing but those words is bad."""
    assert _reject(reason).reason == reason


# --- names with accents -----------------------------------------------------------


class _Directory:
    def __init__(self, people: Sequence[PersonRow]) -> None:
        self._people = people

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]:
        return {}

    async def people(self) -> Sequence[PersonRow]:
        return self._people

    async def departments(self) -> Sequence[DepartmentRow]:
        return []


def _person(number: int, name: str) -> PersonRow:
    return PersonRow(id=uuid.UUID(int=number), display_name=name, department="Sales")


def test_a_person_search_ignores_accents() -> None:
    """Catches a search that compares accents: "Kristian Muhle" would not find
    "Kristian Mühle", and the other way round for "José Núñez"."""
    directory = _Directory([_person(1, "Kristian Mühle"), _person(2, "Jose Nunez")])
    found = asyncio.run(find_person("Kristian Muhle", directory=directory))
    assert [row["name"] for row in found] == ["Kristian Mühle"]

    found = asyncio.run(find_person("José Núñez", directory=directory))
    assert [row["name"] for row in found] == ["Jose Nunez"]


def test_a_person_search_with_accents_on_both_sides_finds_the_person() -> None:
    """Catches a fix that strips accents on the typed side only: both sides
    carry accents here and must still meet."""
    directory = _Directory([_person(1, "Kristian Mühle")])
    found = asyncio.run(find_person("Kristián Mühle", directory=directory))
    assert [row["name"] for row in found] == ["Kristian Mühle"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Mühle", "muhle"), ("Straße", "strasse"), ("Billing-Admin", "billing admin")],
)
def test_normalise_takes_accents_off(text: str, expected: str) -> None:
    """Catches a normaliser that keeps accents or leaves "ß" alone, and one that
    breaks the old rule of turning a hyphen into a space."""
    assert normalise(text) == expected
