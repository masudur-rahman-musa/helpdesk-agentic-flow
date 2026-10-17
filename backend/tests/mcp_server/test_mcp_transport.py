"""The MCP transport itself.

Surface under test (written before it exists):

``mcp_server.main``
    ``create_app() -> FastAPI`` — a FRESH lowlevel MCP server and Starlette
    app per call. Called per test on purpose: the SDK's
    ``StreamableHTTPSessionManager`` refuses a second ``run()`` on one
    instance and each test drives its own ``asyncio.run`` loop. The backing
    services it talks to stay session-scoped.
    ``app`` — a module-level application for uvicorn.
    ``run_stdio() -> None`` — the stdio entrypoint (local debugging).
    Routes: ``GET /healthz`` -> ``{"status": "ok"}``;
    ``GET /readyz`` -> ``{"status": "ready"|"not_ready",
    "checks": {"postgres": bool, "redis": bool}}`` with 200/503; the MCP
    streamable HTTP transport at ``POST /mcp``.

``mcp_server.api.routes.health``
    ``check_postgres()`` / ``check_redis()``, mirroring ``agent_api.readiness``
    with no qdrant. ``/readyz`` calls them through the module attribute so a
    test can fake one check.

``mcp_server.tools.registry.REGISTRY``
    The wire-name -> ``ToolSpec`` mapping ``tools/list`` is built from.

No pytest-asyncio in this project: every case is a sync function driving one
``asyncio.run`` scenario.
"""

import asyncio
import os
import sys
from typing import Any

import mcp.types as mcp_types
import pytest
from mcp import Client, StdioServerParameters, stdio_client
from mcp.server.transport_security import TransportSecuritySettings

from helpdesk.mcp_server.tools.registry import REGISTRY
from helpdesk.shared.core import health
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    http_get,
    list_tools,
    mcp_env,
    mcp_pg,
    mcp_post,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
    transport_security_of_a_fresh_app,
)

# The two read tools, the three helpdesk
# mutating tools and the two access read tools. Pinned as a
# literal, not derived from REGISTRY, so adding a tool without deciding it
# belongs on the wire fails.
EXPECTED_TOOL_NAMES = {
    "get_me",
    "list_my_access",
    "reset_own_password",
    "unlock_own_account",
    "create_ticket",
    "classify_access_request",
    "list_my_requests",
    "create_access_request",
    # The approval queue and the two deciding tools.
    "list_pending_approvals",
    "approve_request",
    "reject_request",
    # The one tool that takes access away.
    "revoke_access",
    # The joiner, mover and leaver tools.
    # They were added together, so this literal went stale for all three at once
    # when only each tool's own tests were run.
    "onboard_user",
    "transfer_user",
    "offboard_user",
    # Opening an access review and reading its items.
    "open_access_review",
    "list_review_items",
    # The keep-or-revoke half, the only path from a review to a
    # change in access (this literal is part of every diff
    # that adds a REGISTRY entry).
    "decide_review_item",
    # The undo, the first tool of the Admin group.
    "rollback_request",
    # The audit log read back under a filter, the Admin group's
    # read half.
    "search_audit",
    # The per-tool emergency stop, the last of the Admin
    # group. Registered like any other tool, so this literal has to name it
    # or the wire surface and the registry disagree.
    "set_kill_switch",
    # The policy corpus read, and the only tool that reaches Qdrant.
    "search_policy",
    # The review QUEUE — the read that answers "which reviews are
    # waiting on me", which the three id-taking review tools cannot (this
    # literal is the wire surface, and it is part of every diff
    # adding a REGISTRY entry).
    "list_reviews",
    # The mailbox that notices fill gets a door: the
    # person's own list and the stamp that marks one read. Twenty-three names
    # become twenty-five, one read and one mutation (this
    # literal is the wire surface, and it is part of every diff adding a
    # REGISTRY entry).
    "list_notifications",
    "mark_notification_read",
    # The admin board's read half. Twenty-five names become
    # twenty-six, the new one a READ — `set_kill_switch` could stop a tool but
    # nothing could say which tools were stopped (this
    # literal is the wire surface, and it is part of every diff adding a
    # REGISTRY entry).
    "list_kill_switches",
    # The SoD rules board. Twenty-six names become twenty-seven,
    # the new one a READ - the rules could be judged against but never listed,
    # so `/admin/rules` had nothing to render (this literal
    # is the wire surface, and it is part of every diff adding a REGISTRY
    # entry).
    "list_sod_rules",
    # The policy documents board. Twenty-seven names become
    # twenty-eight, the new one a READ - the corpus could be SEARCHED
    # (`search_policy`) but never listed, so `/admin/policies` had no way to
    # say which documents and versions a tenant holds (this
    # literal is the wire surface, and it is part of every diff adding a
    # REGISTRY entry).
    "list_policy_documents",
    # The re-ingest, the first MUTATION on the policy corpus.
    # Twenty-eight names become twenty-nine - an edited corpus file could be
    # made live only from a shell on the ingest host, so `/admin/policies`
    # could show a stale version and do nothing about it (this
    # literal is the wire surface, and it is part of every diff adding a
    # REGISTRY entry).
    "reingest_policy_document",
}

DEPENDENCIES: tuple[str, str, str] = ("postgres", "redis", "qdrant")

# The stdio entrypoint, driven as a real subprocess.
_STDIO_CODE = "from helpdesk.mcp_server.main import run_stdio; run_stdio()"


def test_registry_registers_exactly_the_expected_tools() -> None:
    assert set(REGISTRY) == EXPECTED_TOOL_NAMES


def test_tools_list_over_the_transport_returns_exactly_the_registry_names(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Driven through a real MCP client session, not by asserting on a route."""
    result = list_tools(bearer(token_for(mcp_env.tenant_a)))

    assert {tool.name for tool in result.tools} == set(REGISTRY)
    assert {tool.name for tool in result.tools} == EXPECTED_TOOL_NAMES


def test_each_advertised_schema_is_its_registry_models_schema(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """A schema drifting from the model it validates is a lie to every client."""
    result = list_tools(bearer(token_for(mcp_env.tenant_a)))
    advertised = {tool.name: tool for tool in result.tools}

    for name, spec in REGISTRY.items():
        tool = advertised[name]
        assert tool.input_schema == spec.input_model.model_json_schema(), name
        assert tool.output_schema == spec.output_model.model_json_schema(), name
        assert tool.description == spec.description, name


def test_stdio_entrypoint_starts_and_lists_the_same_tools(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The error contract local-debugging entrypoint, driven as a real subprocess."""
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": mcp_env.database_url,
            "PYTHONUNBUFFERED": "1",
        }
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-c", _STDIO_CODE], env=environment
    )

    async def scenario() -> mcp_types.ListToolsResult:
        async with Client(stdio_client(params)) as client:
            return await client.list_tools()

    result = asyncio.run(scenario())

    assert {tool.name for tool in result.tools} == set(REGISTRY)


# --- /healthz and /readyz ------------------------------------------------------


def _make_check(result: bool) -> Any:
    async def _check(*_: Any) -> bool:
        return result

    return _check


def _fake_checks(monkeypatch: pytest.MonkeyPatch, results: dict[str, bool]) -> None:
    # Patch the shared ping functions so register_health's build_checks returns
    # the configured values. `_build_mcp_checks` in mcp_server/main.py accesses
    # them through the `_health` module attribute, which is the same object as
    # `health` here, so the patch takes effect.
    for dependency, healthy in results.items():
        monkeypatch.setattr(health, f"ping_{dependency}", _make_check(healthy))


def test_healthz_answers_without_touching_any_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_checks(monkeypatch, dict.fromkeys(DEPENDENCIES, False))

    response = http_get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_postgres_and_redis_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_checks(monkeypatch, dict.fromkeys(DEPENDENCIES, True))

    response = http_get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": True, "redis": True, "qdrant": True},
    }


@pytest.mark.parametrize("failing", DEPENDENCIES)
def test_readyz_returns_503_naming_the_failing_dependency(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    _fake_checks(monkeypatch, {dep: dep != failing for dep in DEPENDENCIES})

    response = http_get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][failing] is False
    for dependency in DEPENDENCIES:
        if dependency != failing:
            assert body["checks"][dependency] is True


def test_readyz_against_the_live_compose_stack_is_ready(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The real check bodies, not the faked seam: a suite that only ever fakes
    readiness proves the route and nothing about the checks."""
    response = http_get("/readyz")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": True, "redis": True, "qdrant": True},
    }


# --- The Host header the transport accepts ---------------------------
#
# Every case here drives `mcp_post`, not the SDK `Client`: the client builds its
# own request from `mcp_fixtures._BASE_URL` and always sends the loopback name,
# so no existing case in this file can choose a `Host` header (found by searching
# the file for `Host`). `mcp_post` sends `content-type: application/json` for a reason that
# decides whether these cases mean anything at all:
# `TransportSecurityMiddleware.validate_request` checks Content-Type FIRST for a
# POST and answers 400, so a case omitting it would be "red" at HEAD on the
# Content-Type refusal and would stay red after the fix.
MISDIRECTED_REQUEST = 421
"""The status the SDK's `_validate_host` failure answers with, pinned exactly.

A `>= 400` band would let the Content-Type 400 and the
transport's own 406 satisfy a case about the Host header."""

COMPOSE_HOST = "helpdesk-mcp-server:8081"
"""The `Host` agent-api sends inside compose: the service name and the published
port, exactly as `MCP_URL`'s deployed value produces it."""


def test_the_compose_service_name_reaches_the_transport(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the original defect itself: a `create_app()` that lets the SDK
    INFER its transport security from `host="127.0.0.1"`, which builds an
    allow-list of the loopback three and nothing else, so the middleware refuses
    `Host: helpdesk-mcp-server:8081` with 421 before identity, RBAC or any
    project code is reached and no turn completes in the app profile.

    The assertion is "not 421" rather than "200" on purpose: this case is about
    the middleware letting the request THROUGH, and the response the transport
    then produces is every other case in this file's business. Pinning 200 here
    would make the case red for a body or an Accept-header reason that has
    nothing to do with the Host list.

    Nothing adjacent can decide this: the request carries a valid Content-Type
    (so the 400 clause passes), no `Origin` header (so `_validate_origin`
    returns True), and a well-formed JSON-RPC envelope. Removing
    `helpdesk-mcp-server:*` from the configured list is the only change that
    flips it.
    """
    response = mcp_post({"host": COMPOSE_HOST})

    assert response.status_code != MISDIRECTED_REQUEST, response.text


def test_a_loopback_host_still_reaches_the_transport(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a fix that REPLACES the loopback names with the deployed one
    instead of shipping both in the default.

    That is the plausible wrong fix: the defect is stated as "the service name
    is refused", and a default of `["helpdesk-mcp-server:*"]` alone closes it
    while breaking every other path at once - the host-process arrangement
    `uvicorn` serves on 127.0.0.1, this suite's own `_BASE_URL`, and the SDK
    `Client` every other case in this file drives. This case is the guard on
    the fix, and it is green before the fix by design: the loopback names were already allowed,
    and its job is to STAY green through the fix.
    """
    response = mcp_post({"host": "127.0.0.1:8081"})

    assert response.status_code != MISDIRECTED_REQUEST, response.text


def test_an_unlisted_host_is_refused_with_421(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a fix that DISABLES the protection rather than configuring it:
    `enable_dns_rebinding_protection=False`, an `allowed_hosts` of `["*"]`, or
    no `transport_security` at all with `host=` set to something other than a
    loopback name, which makes the SDK skip its auto-enable entirely. Every one
    of those turns the compose case above green while answering a
    DNS-rebinding attacker's forged `Host` just as happily.

    `evil.example` carries no port, so it cannot be matched by the `host:*`
    wildcard form of any legitimate entry, and it is not a prefix of one:
    `_validate_host`'s `startswith(base + ":")` needs the colon. The status is
    pinned exactly rather than as a band, so a 400 on Content-Type or a 406 on
    Accept cannot satisfy it on the Host check's behalf.
    """
    response = mcp_post({"host": "evil.example"})

    assert response.status_code == MISDIRECTED_REQUEST, response.text


def test_a_request_with_no_host_header_is_refused_with_421(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an allow-list check that treats a MISSING header as nothing to
    validate - `if host and host not in allowed: refuse`, which is the shape a
    hand-written check takes, and which lets an HTTP/1.0 client or a crafted
    ASGI scope past the protection entirely by simply omitting the header.

    The SDK's own `_validate_host` returns False on a falsy host, so this case
    pins that the fix does not replace the middleware with a check of its own,
    and pins it at the exact status. `drop_host=True` deletes the header off the
    built request rather than setting it empty, and the middleware logs
    "Missing Host header in request" for it, so the case really exercises the
    missing-header branch and not the unlisted-value one the case above covers.
    """
    response = mcp_post(drop_host=True)

    assert response.status_code == MISDIRECTED_REQUEST, response.text


def test_a_configured_list_replaces_the_default_set(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a helper that UNIONS the configured value with its default -
    `DEFAULT_MCP_ALLOWED_HOSTS + parsed` - which reads as harmless and makes
    the variable unable to NARROW anything.

    That matters because narrowing is the only reason the variable is
    per-deployment: an operator who sets `MCP_ALLOWED_HOSTS` to their own
    cluster name is stating that nothing else may reach the transport, and a
    union silently keeps this repository's own compose service name on the list
    of accepted hostnames in their production deployment.

    The two assertions are the kill in one case: the configured name being
    accepted alone is satisfied by a union too, and the compose name being
    refused alone would be satisfied by a broken parse that configured nothing.
    Only the pair separates a replace from a union. The environment is set with
    monkeypatch BEFORE `mcp_post` calls `create_app()`, because the helper reads
    the variable when the app is built, not per request.
    """
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.internal.example:*")

    configured = mcp_post({"host": "mcp.internal.example:8081"})
    assert configured.status_code != MISDIRECTED_REQUEST, configured.text

    default_name = mcp_post({"host": COMPOSE_HOST})
    assert default_name.status_code == MISDIRECTED_REQUEST, default_name.text


def test_the_transport_receives_explicit_security_settings(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the transport guard being satisfied by the SDK's INFERENCE rather than by
    an explicit pass: `streamable_http_app(host="127.0.0.1")` with no
    `transport_security` builds a `TransportSecuritySettings` of its own, and
    `allowed_hosts` is then the SDK's hardcoded loopback three, which no
    configuration can widen.

    Read off the constructed app rather than the behaviour, because behaviour
    alone cannot tell the two apart for the loopback case: the settings object
    is reached through the mount `create_app()` adds at `/`, that Starlette
    app's `/mcp` route and the `StreamableHTTPASGIApp` endpoint's own
    `session_manager.security_settings` (the union-typed
    attribute carries its narrowing annotation here).

    `allowed_hosts` is asserted as the CONFIGURED list, so the case also catches
    a diff that passes a `TransportSecuritySettings` built from a literal
    instead of from `mcp_allowed_hosts_from_env()` - that implementation
    satisfies the flag and the explicit pass while ignoring the variable
    entirely. `allowed_origins` is deliberately not asserted: the design
    configures hosts only, and `_validate_origin` returns True when no `Origin`
    header is present, which is every request agent-api sends.
    """
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.internal.example:*,127.0.0.1:*")

    settings = transport_security_of_a_fresh_app()

    assert isinstance(settings, TransportSecuritySettings), settings
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["mcp.internal.example:*", "127.0.0.1:*"]
