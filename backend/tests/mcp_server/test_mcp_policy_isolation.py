"""The tenant marker, at the TOOL level.

The claim is that one tenant's corpus is unreachable from another's,
and the RAG layer already pins it in `test_rag_isolation.py`.
This file asserts it where a CLIENT can reach it: two real tokens, the real MCP
transport, the real Qdrant, and a marker sentence ingested only into tenant B's
collection through `vectors.upsert_chunks`.

Surface under test: `mcp_server.tools.policy_search.search_policy` and
`mcp_server.core.rag_clients` exactly as `test_mcp_search_policy.py` describes them.

The two callers are the seeded cross-tenant pair `mcp_fixtures` reads back
(`tenant_a`, `tenant_b`), and both the planting and the assertions resolve their
slugs from those handles rather than from a slug pinned by name here:
the pair is whichever two tenants the seed produced, and a file that
planted into one while asserting on the other would be right half the time.

Nothing is rebuilt per test. The collections are the session's
(`mcp_policy_collections`, dropped at both ends), Postgres and Redis are the
session's, and the per-test namespace is a uuid-suffixed document slug.

No model is loaded: `RecordingEmbedder` returns one all-positive vector for
every text, so every planted chunk is a near neighbour of every query and what
comes back is decided by the COLLECTION — which is the thing under test — and
never by whether a fake similarity cleared the refusal floor.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from typing import Any

import pytest

from helpdesk.mcp_server.core import rag_clients
from helpdesk.mcp_server.services.rag.vectors import collection_name, create_client
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from tests.support.mcp import (
    RecordingEmbedder,
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    plant_policy_document,
    token_for,
)

SEARCH = "search_policy"

# One sentence, so it survives any splitter intact and can be searched for as an
# exact phrase. Obviously fake, and nothing else in this repo's
# seeds, fixtures or corpus contains it.
MARKER = "Fake tenant B keeps its spare badges in the marmalade cupboard."

# Tenant A's own chunk shares wording with tenant B's, so tenant A's collection
# genuinely holds near neighbours of the query: an EMPTY page would report "no
# marker" whether the rule held or not.
TENANT_A_TEXT = "Fake spare badges are issued by reception and logged on collection."

# The query is the marker itself: the best possible question for finding it.
QUERY = MARKER


def _plant(env: SeededMcp, user: SeededUser, slug: str, text: str, title: str) -> None:
    """One document in this tenant's Postgres rows AND its real collection."""

    async def scenario() -> None:
        engine = create_engine(env.database_url)
        client = create_client(load_settings())
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, user.tenant_id) as session:
                await plant_policy_document(
                    session,
                    tenant_slug=user.tenant_slug,
                    tenant_id=user.tenant_id,
                    slug=slug,
                    texts=(text,),
                    title=title,
                    client=client,
                )
        finally:
            await client.close()
            await engine.dispose()

    asyncio.run(scenario())


def _search(user: SeededUser) -> dict[str, Any]:
    """One `search_policy` call for this tenant's token, over the transport."""
    result = call_tool(bearer(token_for(user)), SEARCH, {"query": QUERY, "top_k": 50})
    assert result.is_error is False, error_code(result)
    assert result.structured_content is not None
    payload: dict[str, Any] = result.structured_content
    return payload


def test_tenant_bs_marker_is_returned_to_tenant_b_and_never_to_tenant_a(
    mcp_env: SeededMcp,
    mcp_policy_collections: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a handler that names the searched collection from anything but
    the token's tenant — an argument, a default, a single shared collection.

    One collection per tenant IS the isolation: there is no second
    guard behind it, so a tool searching the wrong name returns the other
    tenant's policy text to a user with a perfectly valid token, and no
    permission check anywhere in this repo would refuse it.

    The tenant B half is what makes the tenant A half evidence rather than an
    empty result: the same marker, the same query, the same fake embedder, and
    the ONLY difference is which token was used.

    Both collections' names are asserted to differ first
    (read off the fixture): a seed that gave the two tenants one slug would make
    every assertion below pass for the wrong reason.
    """
    monkeypatch.setattr(rag_clients, "embedder", lambda: RecordingEmbedder())
    name_a, name_b = mcp_policy_collections
    assert name_a != name_b, (name_a, name_b)
    assert name_a == collection_name(mcp_env.tenant_a.tenant_slug)
    assert name_b == collection_name(mcp_env.tenant_b.tenant_slug)

    suffix = uuid.uuid4().hex[:8]
    _plant(
        mcp_env,
        mcp_env.tenant_b,
        f"fake-physical-access-{suffix}",
        MARKER,
        "Fake Physical Access Policy",
    )
    _plant(
        mcp_env,
        mcp_env.tenant_a,
        f"fake-access-control-{suffix}",
        TENANT_A_TEXT,
        "Fake Access Control Policy",
    )

    page_b = _search(mcp_env.tenant_b)
    page_a = _search(mcp_env.tenant_a)

    # The premise, read off the run rather than assumed: the marker really is
    # retrievable by this query, through this tool, for the tenant that owns it.
    assert page_b["refused"] is False, page_b
    assert any(MARKER in citation["text"] for citation in page_b["citations"]), page_b

    # And tenant A's page is not empty, so "no marker" is isolation and not a
    # collection that answered nothing at all.
    assert page_a["refused"] is False, page_a
    assert page_a["citations"], page_a
    assert not any(MARKER in citation["text"] for citation in page_a["citations"]), page_a
    assert all(
        citation["document"] != f"fake-physical-access-{suffix}" for citation in page_a["citations"]
    ), page_a
