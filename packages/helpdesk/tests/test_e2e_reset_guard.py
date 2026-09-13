"""An earlier change's input-validation criterion: the e2e reset refuses a non-local
database and an oversize tenant slug BEFORE it drops anything.

Expected implementation surface (`scripts/e2e_reset.py`, importable as
`scripts.e2e_reset` because `conftest.py` puts the repo root on `sys.path`):

    LOCAL_HOSTS: tuple[str, str]
        Exactly `("localhost", "127.0.0.1")`.
    TENANT_SLUG_CAP: int
        Read off `Tenant.__table__.c.slug.type.length`, so it cannot drift
        from the column it protects.
    class InvalidResetTargetError(ToolError)
        `code = ErrorCode.VALIDATION`.
    StatementRunner = Callable[[Sequence[str]], Awaitable[None]]
    def check_local_database_url(url: str) -> str
    def check_tenant_slug(slug: str) -> str
    DROP_STATEMENTS: tuple[str, ...]
    async def reset(
        database_url: str,
        tenant_slugs: Sequence[str],
        *,
        run_statements: StatementRunner,
    ) -> None

Pure: no database, no fixture, no compose stack. The guard is driven with
strings and a recording runner, and what is asserted is that the runner was
not reached.

The two constants are pinned to their exact values by
`::test_the_local_hosts_and_the_slug_cap_are_exactly_the_planned_values`, and `TENANT_SLUG_CAP` is pinned against the column itself
by `::test_the_slug_cap_is_the_tenants_slug_column_length`. The inclusive
boundary is `::test_a_slug_of_exactly_the_cap_is_accepted` against
`::test_a_slug_one_character_over_the_cap_is_refused`.

The load-bearing claim, that the checks run before the DDL, is
`::test_no_statement_is_run_when_the_database_url_is_refused` and
`::test_no_statement_is_run_when_a_tenant_slug_is_refused`: move either check
below the drop and both go red. Its other half is
`::test_an_accepted_target_runs_exactly_the_drop_statements`, without which
a reset that drops nothing at all would satisfy the pair above.
"""

import asyncio
from collections.abc import Sequence

import pytest
from helpdesk.errors import ErrorCode
from helpdesk.models.directory import Tenant
from sqlalchemy import String

from scripts.e2e_reset import (
    DROP_STATEMENTS,
    LOCAL_HOSTS,
    TENANT_SLUG_CAP,
    InvalidResetTargetError,
    check_local_database_url,
    check_tenant_slug,
    reset,
)

_LOCALHOST_URL = "postgresql+asyncpg://helpdesk:pw@localhost:5432/helpdesk"
_LOOPBACK_URL = "postgresql+asyncpg://helpdesk:pw@127.0.0.1:5432/helpdesk"
_REMOTE_HOST_URL = "postgresql+asyncpg://helpdesk:pw@db.example.com:5432/helpdesk"
_PRIVATE_IP_URL = "postgresql+asyncpg://helpdesk:pw@10.0.0.5:5432/helpdesk"
_NO_HOST_URL = "postgresql+asyncpg:///helpdesk"

_GOOD_SLUG = "nordlicht"


class _Recorder:
    """A `StatementRunner` that stores what it was asked to run and runs nothing.

    Recording the CALLS rather than a boolean, so
    `::test_an_accepted_target_runs_exactly_the_drop_statements` can assert the
    statements themselves and the refusal cases can assert an empty list.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, statements: Sequence[str]) -> None:
        self.calls.append(list(statements))


def test_the_local_hosts_and_the_slug_cap_are_exactly_the_planned_values() -> None:
    """The two constants, exactly.

    A band would admit a third host, and a cap of 63 or 65 would move the
    boundary the two slug cases pin.
    """
    assert LOCAL_HOSTS == ("localhost", "127.0.0.1")
    assert TENANT_SLUG_CAP == 64


def test_the_slug_cap_is_the_tenants_slug_column_length() -> None:
    """`TENANT_SLUG_CAP` is the column's own length, not a copy of it.

    Read off `tenants.slug` so a migration widening the column cannot leave
    this guard refusing a slug the store would accept.
    """
    column_type = Tenant.__table__.c.slug.type
    assert isinstance(column_type, String)
    assert column_type.length == TENANT_SLUG_CAP


def test_a_localhost_database_url_is_accepted_unchanged() -> None:
    """`localhost` passes and comes back byte for byte.

    Catches a check that normalises the URL on the way through: the caller
    hands the return value to the driver, so a rewritten port, user or
    database name would point the reset somewhere nobody asked for.
    """
    assert check_local_database_url(_LOCALHOST_URL) == _LOCALHOST_URL


def test_a_loopback_database_url_is_accepted_unchanged() -> None:
    """`127.0.0.1` passes too.

    Catches a check written against the literal `localhost` alone, which
    refuses the URL every compose file and CI runner actually produces.
    """
    assert check_local_database_url(_LOOPBACK_URL) == _LOOPBACK_URL


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("a remote hostname", _REMOTE_HOST_URL),
        ("a private address", _PRIVATE_IP_URL),
        ("no host at all", _NO_HOST_URL),
    ],
)
def test_a_database_url_that_is_not_local_is_refused(name: str, url: str) -> None:
    """Every non-local target is refused with the typed error.

    Catches a check that only rejects names it recognises: the private
    address is not `localhost` and not a public name, and the hostless URL
    resolves to the driver's default socket, which is a real database on the
    machine running this. All three answer `VALIDATION`, never a bare
    `ValueError`, so the boundary keeps SPEC section 8.1's vocabulary.
    """
    del name
    with pytest.raises(InvalidResetTargetError) as caught:
        check_local_database_url(url)
    assert caught.value.code is ErrorCode.VALIDATION


def test_a_slug_of_exactly_the_cap_is_accepted() -> None:
    """The inclusive boundary: a slug of exactly `TENANT_SLUG_CAP` passes.

    Catches an off-by-one written as `len(slug) >= TENANT_SLUG_CAP`, which
    refuses a slug the column stores happily.
    """
    at_cap = "n" * TENANT_SLUG_CAP
    assert check_tenant_slug(at_cap) == at_cap


def test_a_slug_one_character_over_the_cap_is_refused() -> None:
    """One character over is refused with the typed error.

    Catches a guard that has no length check at all: the column would raise
    somewhere far below, after the drop, with an untyped driver error.
    """
    with pytest.raises(InvalidResetTargetError) as caught:
        check_tenant_slug("n" * (TENANT_SLUG_CAP + 1))
    assert caught.value.code is ErrorCode.VALIDATION


def test_no_statement_is_run_when_the_database_url_is_refused() -> None:
    """A refused URL reaches no runner.

    Catches an implementation that drops first and validates afterwards:
    every assertion here still passes if the check is merely PRESENT, and
    only the empty call list fails when it is present but too late.
    """
    recorder = _Recorder()
    with pytest.raises(InvalidResetTargetError) as caught:
        asyncio.run(reset(_REMOTE_HOST_URL, [_GOOD_SLUG], run_statements=recorder))
    assert caught.value.code is ErrorCode.VALIDATION
    assert recorder.calls == []


def test_no_statement_is_run_when_a_tenant_slug_is_refused() -> None:
    """An oversize slug reaches no runner either.

    The URL here is local and the slug is the only fault, so this fails when
    the slug check alone is moved below the drop; the URL case cannot decide
    that, because a refusal there never reaches the slug check at all.
    """
    recorder = _Recorder()
    with pytest.raises(InvalidResetTargetError) as caught:
        asyncio.run(reset(_LOCALHOST_URL, ["n" * (TENANT_SLUG_CAP + 1)], run_statements=recorder))
    assert caught.value.code is ErrorCode.VALIDATION
    assert recorder.calls == []


def test_an_accepted_target_runs_exactly_the_drop_statements() -> None:
    """An accepted target reaches the runner with `DROP_STATEMENTS`, in order.

    The other half of the two cases above: a `reset` that dropped NOTHING
    would satisfy both of them and this one is the only thing that says so.
    Asserting the sequence rather than its length also catches a runner
    handed a re-ordered or partial copy.
    """
    recorder = _Recorder()
    asyncio.run(reset(_LOCALHOST_URL, [_GOOD_SLUG], run_statements=recorder))
    assert recorder.calls == [list(DROP_STATEMENTS)]
