/**
 * The readiness probe's timeout, in its own module.
 *
 * It lives here rather than beside the handler because a Next route module may
 * export only its handlers and a fixed set of config fields; a `READYZ_TIMEOUT_MS`
 * exported from `app/readyz/route.ts` fails `next build` with "not a valid Route
 * export field", which is how an earlier change found it. a project rule still wants the
 * value pinned by a test that asserts it exactly, and a test cannot import what
 * the route may not export, so the constant moves and both sides import it.
 */

/**
 * Two seconds on the readiness call.
 *
 * Far below `lib/agent-api.ts`'s ten, because this is not a user's request: a
 * kubelet re-probes on its own period, so an answer that arrives after that
 * period is worthless, and a probe with no timeout at all fails by HANGING past
 * the kubelet's deadline rather than by answering "not ready". Failing fast is
 * the correct reading of a socket that has not answered in two seconds.
 * Pinned, with its signal traced onto the outgoing call, by
 * `tests/health-routes.test.ts`'s "pins the readyz timeout at exactly 2000ms
 * and carries its signal on the outgoing call".
 */
export const READYZ_TIMEOUT_MS = 2_000;
