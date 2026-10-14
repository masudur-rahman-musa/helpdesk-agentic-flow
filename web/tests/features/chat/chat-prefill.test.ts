import { describe, expect, it } from "vitest";

import { type MyGrant, myAccessSchema } from "@/features/access/schema";
import { CHAT_PREFILL_PARAM, readAsk, renewalHref } from "@/features/chat/prefill";
import { MAX_MESSAGE_CHARS } from "@/features/chat/schema";

/**
 * An earlier change §6, `chat-prefill.test.ts`: the `/access` renewal link and the
 * parameter `/chat` prefills its composer from, as pure functions (decision C).
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23b's Deviations):
 * - `readAsk(search: string): string | null` takes `window.location.search`
 *   verbatim — leading `?` and all — so the panel needs no router context and
 *   no Suspense boundary (§2), and answers `null` for anything
 *   `chatMessageSchema` refuses.
 * - `renewalHref(grant: MyGrant): string` takes the PARSED grant row, which is
 *   what `/access` already holds, and answers a root-relative href.
 * - the sentence is not pinned word for word here: what is pinned is that it
 *   NAMES the role and the system, that it survives `readAsk` unchanged (so
 *   the composer will accept what the link carries), and that the href is
 *   encoded. A literal sentence would be a second place to edit the wording.
 *
 * The grant fixture is built by PARSING a wire row through `myAccessSchema`,
 * so a field renamed on the wire fails here rather than in a hand-written
 * object that agrees with nothing.
 */

const GRANT_ROW: Record<string, unknown> = {
  grant_id: "55555555-5555-5555-8555-555555555555",
  role_slug: "payroll-read-write",
  role_name: "Fake Payroll Read & Write",
  privilege: "write",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  granted_at: "2026-01-05T09:00:00Z",
  expires_at: "2026-06-10T12:00:00Z",
};

/**
 * The one grant every case below uses. Its role name carries a space and an
 * ampersand on purpose: both are the characters a hand-built query string
 * loses, and neither is exotic in a role called "read & write".
 */
function grant(overrides: Record<string, unknown> = {}): MyGrant {
  const [parsed] = myAccessSchema.parse({ grants: [{ ...GRANT_ROW, ...overrides }] }).grants;
  if (parsed === undefined) {
    throw new Error("the grant fixture must parse to exactly one row");
  }
  return parsed;
}

/** The `search` of an href, as `window.location.search` would hand it over. */
function searchOf(href: string): string {
  return new URL(href, "http://localhost:3000").search;
}

describe("CHAT_PREFILL_PARAM", () => {
  it("is exactly ask", () => {
    // a project rule, on the one name shared by three places: the link
    // `/access` writes, the reader `/chat` runs and §1's own `/chat?ask=…`.
    // Catches a rename to `q` or `message` that updates the reader and the
    // writer together and so passes every other case in this file, while every
    // link already in a user's history silently stops prefilling.
    expect(CHAT_PREFILL_PARAM).toBe("ask");
  });
});

describe("readAsk", () => {
  const ASK = "Can I renew Fake Payroll Read & Write on Fake Payroll?";

  it("answers the message of a well-formed parameter, ampersand and all", () => {
    // Catches a reader that splits the search on `&` and `=` by hand: the
    // encoded ampersand inside the sentence then ends the value, and the
    // composer is prefilled with half a question. `URLSearchParams` is the
    // implementation that passes.
    expect(readAsk(`?${CHAT_PREFILL_PARAM}=${encodeURIComponent(ASK)}`)).toBe(ASK);
  });

  const REFUSED = [
    { name: "an empty search", search: "" },
    { name: "a search carrying another parameter only", search: "?other=hello" },
    { name: "an empty ask", search: "?ask=" },
    { name: "a parameter whose name merely starts with ask", search: "?asking=hello" },
  ] as const;

  it.each(REFUSED)("answers null for $name", ({ search }) => {
    // The first two rows catch a reader that answers `""` (or a canned sample
    // question) when the parameter is absent, which would prefill the composer
    // on every ordinary visit to `/chat`. The empty-ask row is §1's own
    // refusal: `chatMessageSchema`'s `min(1)` is what must decide it, not a
    // truthiness check that happens to agree today. The last row catches a
    // reader matching the parameter by `search.includes("ask=")`, which reads
    // a value out of a parameter that is not this one.
    expect(readAsk(search)).toBeNull();
  });

  it("is inclusive at MAX_MESSAGE_CHARS and refuses one character more", () => {
    // a project rule's boundary, on a client-supplied string that reaches a
    // control. Catches a reader with no check at all (the composer then starts
    // holding a message it would itself refuse, so Send answers VALIDATION and
    // the user cannot see why), and catches an off-by-one written as `<` where
    // the schema says `<=` — which would refuse the longest message the
    // conversation can actually store.
    const atCap = "a".repeat(MAX_MESSAGE_CHARS);
    const overCap = "a".repeat(MAX_MESSAGE_CHARS + 1);

    expect(readAsk(`?ask=${atCap}`)).toBe(atCap);
    expect(readAsk(`?ask=${overCap}`)).toBeNull();
  });
});

describe("renewalHref", () => {
  it("points at /chat with the ask parameter", () => {
    // Catches an href built for another route (or a bare `/chat` with no
    // parameter, which is the alternative decision C turned down): §1 fixes
    // `/chat?ask=<sentence>` as the shape, and the prefix is the half of it
    // the round-trip below cannot see.
    expect(renewalHref(grant()).startsWith(`/chat?${CHAT_PREFILL_PARAM}=`)).toBe(true);
  });

  it("carries a sentence naming the role and the system, encoded so readAsk gets it whole", () => {
    // The pair criterion, asserted as a pair: the link is only worth anything
    // if the READER accepts what the WRITER produced. Catches an href built by
    // string concatenation — the raw space and the `&` in "Fake Payroll Read &
    // Write" split the query, `readAsk` answers "Can I renew Fake Payroll Read"
    // and the user gets a truncated question — and catches a sentence that
    // names the role but not the system, which on a tenant holding the same
    // role name on two systems asks for a renewal nobody can act on.
    const href = renewalHref(grant());
    const ask = readAsk(searchOf(href));

    expect(href).not.toContain(" ");
    expect(ask).not.toBeNull();
    expect(ask).toContain("Fake Payroll Read & Write");
    expect(ask).toContain("Fake Payroll");
  });

  it("builds a different sentence for a different role", () => {
    // Catches a hardcoded sentence: one constant string would satisfy every
    // assertion above for the fixture role, and every row of `/access` would
    // then send the same question about the same access.
    const other = renewalHref(grant({ role_name: "Fake Warehouse Read", role_slug: "wh-read" }));

    expect(readAsk(searchOf(other))).toContain("Fake Warehouse Read");
    expect(other).not.toBe(renewalHref(grant()));
  });
});
