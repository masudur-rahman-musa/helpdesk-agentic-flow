import { describe, expect, it } from "vitest";

import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { MAX_MESSAGE_CHARS, chatMessageSchema } from "@/lib/chat-schema";

/**
 * An earlier change §6, `chat-schema.test.ts`: the ONE schema the composer and the route
 * handler share, checked against `MAX_MESSAGE_CHARS`, on both sides of the
 * bound and on the empty string — the same three answers
 * `helpdesk.models.conversation.check_message_content` gives (§1, "input
 * validation").
 *
 * Shapes this file fixes that the plan left open (a project rule/39; these go
 * in the plan's Deviations):
 * - `web/lib/chat-schema.ts` exports `MAX_MESSAGE_CHARS` (the mirror of the
 *   Python constant) and `chatMessageSchema`, a Zod object over the WIRE body
 *   `{ message: string }` rather than a bare string schema. The route handler
 *   parses a JSON body and the composer builds the same object, so one
 *   `safeParse` serves both and the two cannot drift into checking different
 *   shapes.
 * - `FAILURE_MESSAGES` is a plain object keyed by the `ErrorCode` VALUE plus a
 *   `default` key. §6 fixes three of its entries as literals and the page test
 *   reads `FAILURE_MESSAGES.INTERNAL`, so the map is TOTAL over the eight codes
 *   in `helpdesk/errors.py::ErrorCode`; only the three sentences the plan wrote
 *   down are pinned as literals here, because the other five are the
 *   implementer's wording and pinning invented prose would be a test nobody can
 *   satisfy without reading this file.
 *
 * The map lives in `web/lib/chat-errors.ts`; it is asserted here rather than in
 * a fifth file because §6 fixes exactly four files and these are constants of
 * the same "one message, one constant" family as the schema.
 */

const CAP = 4000;

/** A message of exactly `length` characters, with no whitespace to trim away. */
function message(length: number): string {
  return "x".repeat(length);
}

describe("MAX_MESSAGE_CHARS", () => {
  it("pins the cap at exactly 4000", () => {
    // Catches an implementation that mirrors the Python cap by eye and writes
    // 4096, 2000 or "some large number": a web cap ABOVE the column width lets
    // a message through to be refused by agent-api after the round trip, and one
    // below silently truncates what a user may ask. a project rule wants the
    // value, not a band.
    expect(MAX_MESSAGE_CHARS).toBe(CAP);
  });
});

describe("chatMessageSchema", () => {
  const rows = [
    { name: "the empty string", message: "", accepted: false },
    { name: "one character", message: message(1), accepted: true },
    { name: "exactly the cap", message: message(CAP), accepted: true },
    { name: "one character over the cap", message: message(CAP + 1), accepted: false },
  ] as const;

  it.each(rows)("$name is accepted: $accepted", ({ message: content, accepted }) => {
    // The two boundary rows catch an EXCLUSIVE bound written where the Python
    // side is inclusive (`.max(4000)` swapped for `<` on 4000, or a `>=`
    // comparison), which refuses a message agent-api would have stored — and
    // its mirror, a cap of 4001 that accepts a message the column cannot hold.
    // The empty row catches a schema built from `z.string()` alone: Zod accepts
    // "" for that, and an empty turn is billed by the gateway and stored as a
    // message with no content, which is exactly what `check_message_content`
    // refuses.
    expect(chatMessageSchema.safeParse({ message: content }).success).toBe(accepted);
  });

  it("refuses a body whose message is not a string", () => {
    // Catches a handler that pre-checks only the LENGTH of whatever arrived —
    // `String(body.message).length` on a number, an array or null passes every
    // row above and forwards a body agent-api answers 422 to, which the client
    // has no typed code to render.
    expect(chatMessageSchema.safeParse({ message: 42 }).success).toBe(false);
    expect(chatMessageSchema.safeParse({}).success).toBe(false);
  });
});

describe("FAILURE_MESSAGES", () => {
  it("carries the three sentences the plan fixed, verbatim", () => {
    // Catches an implementation that renders agent-api's own typed CODE to the
    // user ("POLICY_BLOCKED") or invents a sentence per call site, which is how
    // two places end up telling a user two different things about one refusal.
    expect(FAILURE_MESSAGES.VALIDATION).toBe("That message could not be sent.");
    expect(FAILURE_MESSAGES.POLICY_BLOCKED).toBe(
      "Too many messages just now. Wait a moment and send it again.",
    );
    expect(FAILURE_MESSAGES.default).toBe("Something went wrong. Nothing was changed.");
  });

  it("carries an entry for INTERNAL, the code a mid-stream failure arrives as", () => {
    // Catches a map holding only the two named codes and the default: an
    // `error` event from `_failure_frames` carries `INTERNAL` more often than
    // anything else, and `FAILURE_MESSAGES.INTERNAL` would be `undefined` —
    // rendering the literal "undefined" into the alert region, which the page
    // test's reference comparison would otherwise accept.
    // The local annotation is deliberate: it typechecks whether the map is a
    // total record over the codes or an index signature (a project rule's
    // shape, on the TypeScript side).
    const internal: string | undefined = FAILURE_MESSAGES.INTERNAL;
    expect(typeof internal).toBe("string");
    expect((internal ?? "").length).toBeGreaterThan(0);
  });
});
