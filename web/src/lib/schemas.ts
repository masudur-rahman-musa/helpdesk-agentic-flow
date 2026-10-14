/**
 * The one login schema, shared by the client form and the route handler
 *.
 *
 * The two widths are the columns' own: `tenants.slug` is `String(64)` and
 * `users.email` is `String(320)` in `helpdesk/models/directory.py`. Pinned at
 * those exact numbers by `tests/schemas.test.ts` ("pins the tenant slug width
 * at exactly 64", "pins the email width at exactly 320").
 *
 * Both patterns are anchored at both ends. An unanchored pattern matches a
 * SUBSTRING, so `nordlicht\n` would pass — the same trap a project rule
 * records on the Python side. Killed by `tests/schemas.test.ts`'s rejection
 * table (trailing newline, trailing CRLF, trailing space) and by its
 * "anchors SLUG_PATTERN at both ends" case.
 *
 * Neither value lands in a store from here: the pre-check exists so the form
 * renders its own messages and so an oversized body never reaches agent-api
 * (`tests/login-route.test.ts`, "never calls fetch for ..."). It is not a
 * control — any other client can still post an uncapped body, which is
 * an earlier change decision E and row 27's work.
 */

import { z } from "zod";

export const SLUG_MAX_LENGTH = 64;
export const EMAIL_MAX_LENGTH = 320;

export const SLUG_PATTERN = /^[a-z0-9-]+$/;

/**
 * Deliberately simpler than a full address grammar: one `@`, a dot in the
 * domain, no whitespace anywhere. Anchored, so a trailing newline is refused
 * here for the same reason it is in the slug.
 */
export const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/** One message for every refusal, so nothing names which input was wrong. */
export const SIGN_IN_FAILED_MESSAGE = "Sign-in failed. Check your details and try again.";

export const loginSchema = z.object({
  tenant_slug: z.string().min(1).max(SLUG_MAX_LENGTH).regex(SLUG_PATTERN),
  email: z.string().min(1).max(EMAIL_MAX_LENGTH).regex(EMAIL_PATTERN),
  password: z.string().min(1),
});

export type LoginInput = z.infer<typeof loginSchema>;
