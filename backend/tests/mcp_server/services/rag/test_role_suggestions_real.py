"""Role suggestions with the REAL embedder, on the roles of the seed spec.

Surface under test (written before it exists): ``RoleRow.system_about`` and
``suggest`` scoring each role as the best cosine over ``role_texts``, from
``helpdesk.mcp_server.services.rag.role_suggestions``; ``TenantSpec.system_about``
(system slug to comma-separated phrases) in ``scripts.seed``.

No database: the rows are built from the spec. The embedder is the shared
session fixture in this folder's ``conftest.py``; each case gets its own cache.
"""

import pytest

from helpdesk.mcp_server.services.rag.embedding import Embedder
from helpdesk.mcp_server.services.rag.role_suggestions import (
    SUGGESTION_FLOOR,
    RoleRow,
    TextVectorCache,
    suggest,
)
from scripts.seed import CUAN, NORDLICHT, TenantSpec


def _rows(spec: TenantSpec) -> list[RoleRow]:
    """Every role of the tenant, as the MCP tool would read it."""
    names = {slug: name for slug, name, _sensitivity, _category in spec.systems}
    return [
        RoleRow(
            system_slug=role.system,
            system_name=names[role.system],
            role_slug=role.slug,
            role_name=f"{role.system}.{role.slug}",
            system_about=spec.system_about[role.system],
        )
        for role in spec.roles
    ]


@pytest.mark.parametrize("words", ["doc", "docs", "documentation", "guides"])
def test_real_words_find_the_wiki(embedder: Embedder, words: str) -> None:
    """Catches a ranker that still compares names only ("doc" scored 0.57
    against "Internal Wiki wiki.viewer", under the floor), and phrases that do
    not say what the wiki is for: the wiki must come first at the floor or
    above."""
    result = suggest(embedder, TextVectorCache(), words, _rows(NORDLICHT))

    assert result, f"nothing offered for {words!r}"
    assert result[0].row.system_slug == "wiki"
    assert result[0].score >= SUGGESTION_FLOOR


@pytest.mark.parametrize("spec", [NORDLICHT, CUAN], ids=lambda spec: spec.slug)
@pytest.mark.parametrize(
    "words",
    ["asdf qwer", "zzzz", "hello", "pizza", "lorem ipsum", "qwerty", "banana", "xyz"],
)
def test_real_nonsense_finds_nothing(embedder: Embedder, spec: TenantSpec, words: str) -> None:
    """Catches a floor lowered, or phrases so loose, that noise gets roles
    offered: the phrases that lift "doc" to the wiki must not lift nonsense
    over 0.70 for any role of either tenant."""
    result = suggest(embedder, TextVectorCache(), words, _rows(spec))

    assert result == []
