"""Request models for the admin audit routes."""

from pydantic import BaseModel, ConfigDict


class SearchAuditArgs(BaseModel):
    """`search_audit`'s arguments, carrying ONLY the filters that were asked for.

    Declared with no fields and `extra="allow"` rather than with eight
    nullable ones, because `model_dump` carries every declared field: a
    `limit: int | None = None` would put `limit: null` on the wire against a
    bound of `1`-`200`, and an `event_type: str | None = None` would send
    `event_type: null` where the tool's absent-filter meaning is "every event
    type" (23c's reason). An absent filter therefore sends no key at all, and
    the tool's own `SEARCH_LIMIT_DEFAULT` applies when `limit` is absent.

    Deviation from section 6, recorded in 24a's Deviations: 23c, 23h and this
    plan's section 6 describe a SUBCLASS per shape, which works at one and two
    optional arguments and does not at eight — the eight filters have 256
    combinations. This is the same guarantee written once. Pinned by
    `test_audit_routes.py::test_the_search_route_sends_no_argument_at_all_when_no_filter_is_asked_for`
    and `::test_each_filter_present_sends_exactly_its_own_key_and_no_other`,
    which assert the arguments dict for the empty shape and for each of the
    eight single-filter ones.
    """

    model_config = ConfigDict(extra="allow")
