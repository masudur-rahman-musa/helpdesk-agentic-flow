/**
 * One read of one proxy route, for every client component that makes one
 *.
 *
 * The four-line shape — followed redirect, status before body, typed code,
 * `safeParse` — has been written seven times since an earlier change, once per board
 * that talks to `app/api/`, and each copy is a place the order of those four
 * lines can drift. It lands here as a module with `RulesBoard` as its only
 * caller; migrating the other seven is row 24i, which is why nothing in this
 * file knows any page's payload: the schema is a parameter and the sentence an
 * admin reads belongs to the caller.
 *
 * Pinned by `tests/wire-read.test.ts`, over the module alone with a stubbed
 * `fetch` and a two-field throwaway schema.
 *
 * The order of the four is the whole of it, and each step is pinned by a case
 * that fails when that step moves:
 *
 * 1. `redirected` FIRST, because a browser has already followed the proxy's
 *    redirect to `/login` by the time this code runs (22b decision E) and what
 *    it holds is a 200 carrying the login page. A reader checking `ok` or
 *    `status === 401` first sees success, the schema then refuses the HTML, and
 *    an expired session is reported as a broken read
 *    (`::assigns response.url and returns null for a redirected response`).
 * 2. the STATUS before the body, because the proxy's own `502
 *    {"code": "UPSTREAM_UNAVAILABLE"}` is valid JSON: a reader that parsed
 *    first would hand the caller an outage dressed as a payload
 *    (`::throws a plain error for a 502 carrying UPSTREAM_UNAVAILABLE`).
 * 3. the code checked against the design's vocabulary and not merely read, so a
 *    code from outside it is a failed read rather than a typed refusal
 *    (a project rule, `isTypedCode — the the design vocabulary and nothing
 *    else`).
 * 4. `safeParse` and never a cast, so a payload the page cannot read surfaces
 *    as a failed read rather than as a blank cell
 *    (`::throws a plain error for a 200 whose body the schema refuses`).
 *
 * Nothing here writes: it is a `GET` with no body, and every mutation in this
 * project is a typed MCP tool behind RBAC. It sends no tenant and
 * no user either — the bearer is an httpOnly cookie the proxy attaches, and
 * identity is the token's.
 */

import type { z } from "zod";

import { FAILURE_MESSAGES } from "@/lib/failure-messages";

/**
 * A read the wire REFUSED, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is the client's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal. A caller distinguishes this from a failed read by
 * the class alone — pinned by `tests/wire-read.test.ts::throws WireRefusedError
 * carrying $name`, which is parametrised over two codes so a module hardcoding
 * one is red.
 */
export class WireRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the read was refused");
    this.code = code;
  }
}

/**
 * Whether a body's `code` is one of the design's eight, which is what separates a
 * typed refusal from a failed read.
 *
 * `default` is `FAILURE_MESSAGES`' fallback KEY and never a wire value, so a
 * bare `code in FAILURE_MESSAGES` would dress a body carrying it as a refusal;
 * the `typeof` guard is what keeps `in` off a number. Both directions are
 * pinned by `tests/wire-read.test.ts::answers $typed for $name`, whose two true
 * rows exist so the predicate cannot be satisfied by one that always answers
 * false.
 */
export function isTypedCode(code: unknown): code is string {
  return typeof code === "string" && code !== "default" && code in FAILURE_MESSAGES;
}

/**
 * The parsed payload of `path`, or `null` while the browser navigates to
 * `/login`.
 *
 * The `null` is the redirect and nothing else: it is the third answer a board
 * needs to render neither a table, nor an alert, nor an empty state while the
 * page is going away (24da's §5, a project rule's fourth half). Everything
 * else throws — `WireRefusedError` for one of the design's codes, a plain
 * `Error` for every other failure — so a caller's empty state and its alert
 * stay mutually exclusive by construction.
 */
export async function readWire<T>(path: string, schema: z.ZodType<T>): Promise<T | null> {
  const response = await fetch(path, { headers: { accept: "application/json" } });

  if (response.redirected) {
    window.location.assign(response.url);
    return null;
  }
  if (!response.ok) {
    const code = await typedCodeOf(response);
    if (code !== null) {
      throw new WireRefusedError(code);
    }
    throw new Error(`the read of ${path} failed (${response.status})`);
  }

  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error(`the payload of ${path} did not match what this page reads`);
  }
  return parsed.data;
}

/**
 * The typed code a refused response carries, or null when it carries none — a
 * body that is not JSON at all included, which is why `json()` is guarded
 * rather than awaited into an unhandled rejection.
 */
async function typedCodeOf(response: Response): Promise<string | null> {
  const body = await readJson(response);
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const code = (body as { code?: unknown }).code;
  return isTypedCode(code) ? code : null;
}

/** The body as JSON, or `undefined` when it is not JSON at all. */
async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
