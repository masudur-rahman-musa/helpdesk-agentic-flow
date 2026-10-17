import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { isTypedCode, readWire, WireRefusedError } from "@/lib/wire-read";

/**
 * `wire-read.test.ts`: the shared wire read `lib/wire-read.ts`, over the module ALONE with a
 * stubbed `fetch` and a two-field
 * throwaway schema. No component, no jsdom render, no backing service.
 *
 * This is the four-line shape seven components each carry their own copy of
 * (redirected, status, typed code, `safeParse`). It lands here with
 * `RulesBoard` as its only caller; the other pages are migrated later, so
 * nothing in this file knows any page's payload — the schema is a parameter.
 *
 * Shapes the plan left open that this file fixes:
 * - `readWire(path, schema)` is called with a PATH string and a zod schema and
 *   answers `Promise<T | null>`; the `null` is the redirect and nothing else.
 * - a failed read (a body the schema refuses, a body that is not JSON, a code
 *   outside the typed error codes) throws an `Error` that is NOT a `WireRefusedError`; the
 *   message is not pinned, because the CALLER owns the sentence it shows
 *   (`RULES_READ_MESSAGE`) and nothing renders this one.
 * - `isTypedCode` takes `unknown`.
 * - the redirect is a 200 with `redirected` and `url` defined on the Response
 *   INSTANCE, and `location` is stubbed with `vi.stubGlobal` .
 */

const PROBE_PATH = "/api/probe/thing";
const ORIGIN = "http://localhost:3000";
const LOGIN_URL = "http://localhost:3000/login";

/** A throwaway schema: two fields, both required. */
const probeSchema = z.object({ name: z.string(), count: z.number() });

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/**
 * The response a browser holds after FOLLOWING the proxy's redirect to
 * `/login`: a 200 carrying the login page, with `redirected` true and `url`
 * where it ended up. `Response` has no constructor option for either, so both
 * are defined on the instance.
 */
function redirectedResponse(): Response {
  const response = new Response("<html>login</html>", {
    status: 200,
    headers: { "content-type": "text/html" },
  });
  Object.defineProperty(response, "redirected", { value: true, configurable: true });
  Object.defineProperty(response, "url", { value: LOGIN_URL, configurable: true });
  return response;
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

/** What a rejected read threw, as a value this file can assert the type of. */
async function rejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("expected the read to throw, but it resolved");
}

let answer: () => Response = () => jsonResponse({ name: "fake-thing", count: 2 }, 200);

beforeEach(() => {
  answer = () => jsonResponse({ name: "fake-thing", count: 2 }, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes(PROBE_PATH)) {
      return answer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", { ...window.location, href: `${ORIGIN}/admin/rules`, assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("readWire — a read that worked", () => {
  it("returns the parsed value for a 200 carrying a valid payload", async () => {
    // Kills a `readWire` that answers the RAW body rather than `parsed.data`:
    // the caller would then hold an unvalidated `unknown` typed as the schema's
    // output, which is the whole reason the schema is a parameter. Kills one
    // that answers `undefined` or the envelope it never unwrapped. The value is
    // asserted as an exact object, so a reader dropping a field is red.
    const parsed = await readWire(PROBE_PATH, probeSchema);

    expect(parsed).toEqual({ name: "fake-thing", count: 2 });
    expect(fetchStub).toHaveBeenCalledTimes(1);
    const url = new URL(urlOf(fetchStub.mock.calls[0]?.[0] ?? ""), ORIGIN);
    expect(url.pathname).toBe(PROBE_PATH);
    expect(url.search).toBe("");
  });
});

describe("readWire — a session that expired", () => {
  it("assigns response.url and returns null for a redirected response", async () => {
    // The case the seven copies exist to keep identical.
    // Kills a `readWire` that reads `response.ok` or `response.status === 401`
    // first: a followed redirect is a 200 carrying the login page, so both see
    // success, the schema then refuses the HTML, and the caller renders "could
    // not be read" for a session that merely expired. The RETURN is asserted to
    // be `null` and not the parsed value, because that null is the third answer
    // a board needs to render neither a table, nor an alert, nor an empty state
    // while the browser navigates away .
    answer = redirectedResponse;

    const parsed = await readWire(PROBE_PATH, probeSchema);

    expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    expect(parsed).toBeNull();
  });
});

describe("readWire — a refusal the wire typed", () => {
  const REFUSALS: ReadonlyArray<{ name: string; status: number; code: string }> = [
    { name: "FORBIDDEN", status: 403, code: "FORBIDDEN" },
    { name: "TOOL_DISABLED", status: 503, code: "TOOL_DISABLED" },
  ];

  it.each(REFUSALS)("throws WireRefusedError carrying $name", async ({ status, code }) => {
    // The half that lets a caller pick a sentence per code. Kills a `readWire`
    // that throws a plain `Error` for every non-ok response, which collapses a
    // typed refusal into the caller's generic read message — a caller who is
    // not an `it_admin` would be told to "try again in a moment" for something
    // no retry fixes. Kills one that drops the code (an error carrying only a
    // status), and one that keeps the wire's own free text: the code is the
    // only thing the error contract puts on the wire and the sentence is the client's.
    // Two codes rather than one, so a module hardcoding `FORBIDDEN` is red.
    answer = () => jsonResponse({ code }, status);

    const error = await rejection(readWire(PROBE_PATH, probeSchema));

    expect(error).toBeInstanceOf(WireRefusedError);
    expect((error as WireRefusedError).code).toBe(code);
  });
});

describe("readWire — reads that failed", () => {
  it("throws a plain error for a 502 carrying UPSTREAM_UNAVAILABLE", async () => {
    // The refusal-versus-failure discriminator, and the bug that turns an outage into
    // an empty board. The proxy's own 502 envelope is VALID JSON carrying a
    // `code`, so a module that read the body without asking whether the code is
    // one of the eight typed error codes would answer `WireRefusedError` and the caller
    // would render `FAILURE_MESSAGES.default` — "Something went wrong. Nothing
    // was changed." — for a dependency that is down. Arranged so only that
    // check flips it: the status is non-ok either way, and the body parses
    // either way, so the type of the thrown value is the single observable
    // difference between a module that classifies the code and one that does
    // not.
    answer = () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502);

    const error = await rejection(readWire(PROBE_PATH, probeSchema));

    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(WireRefusedError);
  });

  it("throws a plain error for a non-ok response whose body is not JSON", async () => {
    // Kills a module calling `response.json()` unguarded: the parse error
    // escapes as an unhandled rejection rather than as a failed read, so the
    // caller's query never settles into an error state and the page sits on a
    // spinner. The thrown value is asserted NOT to be a `WireRefusedError`,
    // which kills the other shape — a module treating "no code found" as a
    // refusal with an undefined code, whose caller then renders the fallback
    // sentence for an HTML error page.
    answer = () =>
      new Response("<html>bad gateway</html>", {
        status: 500,
        headers: { "content-type": "text/html" },
      });

    const error = await rejection(readWire(PROBE_PATH, probeSchema));

    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(WireRefusedError);
  });

  it("throws a plain error for a 200 whose body the schema refuses", async () => {
    // Kills a module that CASTS the body instead of running `safeParse` on it:
    // the caller would be handed an object missing a field its own type says is
    // required, and the fault would surface as a blank cell rather than as a
    // failed read. The response is a 200, so neither the status branch nor the
    // redirect branch can be what makes this case throw — only the parse can.
    // Not a `WireRefusedError`: a payload this page cannot read is a failed
    // read, not something the wire refused.
    answer = () => jsonResponse({ name: "fake-thing" }, 200);

    const error = await rejection(readWire(PROBE_PATH, probeSchema));

    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(WireRefusedError);
  });
});

describe("isTypedCode — the SPEC §8.1 vocabulary and nothing else", () => {
  const CODES: ReadonlyArray<{ name: string; value: unknown; typed: boolean }> = [
    { name: "the fallback key default", value: "default", typed: false },
    { name: "a code outside the map", value: "UPSTREAM_UNAVAILABLE", typed: false },
    { name: "a non-string", value: 403, typed: false },
    { name: "FORBIDDEN", value: "FORBIDDEN", typed: true },
    { name: "INTERNAL", value: "INTERNAL", typed: true },
  ];

  it.each(CODES)("answers $typed for $name", ({ value, typed }) => {
    // Three false rows, with two true rows beside them so the predicate is
    // pinned rather than satisfied by `() => false`. The first row kills a predicate written as a
    // bare `code in
    // FAILURE_MESSAGES`: `default` is the shared map's fallback KEY and never a
    // wire value, so a body carrying it would be dressed as a typed refusal.
    // The second row is the refusal-versus-failure discriminator at the predicate — the proxy's
    // own code must fall outside. The third row kills one calling `in` on a number,
    // which throws rather than answering false. The fourth and fifth rows kill a predicate
    // that shrank the vocabulary to the two codes this page can reach: other pages
    // hand this module the other five surfaces, and `INTERNAL` is what a
    // mid-stream failure arrives as.
    expect(isTypedCode(value)).toBe(typed);
  });
});
