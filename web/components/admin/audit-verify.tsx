"use client";

import { useMutation } from "@tanstack/react-query";

import { auditVerifySchema } from "@/lib/audit-schema";
import { FAILURE_MESSAGES, failureMessage } from "@/lib/chat-errors";
import { Button } from "@/components/ui/field";

/**
 * The chain check on `/admin/audit`, from `GET /api/admin/audit/verify` (plan
 * 24bb §1).
 *
 * Its own client component beside `AuditSearch` (decision D): it shares no
 * state with the search — no filters, no cursor, no page size — and 24ba's
 * whole test file renders `<AuditSearch />`, so folding it in would put a
 * closed plan's cases inside this diff for a component they never asked about.
 *
 * The check fires on a CLICK through `useMutation` (decision E) and never on
 * paint: a chain walk is a full scan of this tenant's log, and firing it on
 * every visit would put that scan behind a page an auditor opens to read one
 * row. A mutation rather than a disabled query because an answer asked for
 * twice is two answers and not a cache hit — the second ask exists precisely
 * because something may have changed. Pinned by
 * `tests/audit-verify-panel.test.tsx::renders the named region and its control
 * without calling the proxy`.
 *
 * The read takes NO argument at all: 24a's proxy forwards no query and no body,
 * and agent-api walks the chain against the tenant in the verified token, never
 * a parameter. A page that could ask about another tenant's chain
 * is the thing that must not exist, which `::asks the verify proxy once, as a
 * GET, with no query string at all` pins from this side.
 *
 * Nothing here decides anything. Whether this caller may ask, whether the chain
 * is intact and where it broke all come from an earlier change's route; this renders one
 * sentence about the answer.
 *
 * Two unhappy paths, one discriminator: a body carrying one
 * of the design's eight codes is a REFUSAL and renders that code's shared
 * sentence, and everything else — a payload the parser refuses, a body that is
 * not JSON, and the proxy's own `UPSTREAM_UNAVAILABLE`, which is not one of the
 * eight — is a failed read. No `VALIDATION` override on this surface, unlike
 * `AuditSearch`: the route takes no argument, so there is nothing an auditor
 * could correct. Pinned by the two `$name renders its own sentence and not the
 * read failure` rows and the three `$name renders the read failure and no
 * verdict` rows.
 *
 * An expired session is `response.redirected` rather than a status, because the
 * browser has already followed the proxy's redirect by the time this code runs
 * (22b decision E). On this surface that matters more than on any other: a
 * fetcher checking `response.ok` would render "the chain could not be checked"
 * for a session that merely expired, which is the sentence an auditor is most
 * likely to escalate as evidence of tampering. Pinned by `::follows the proxy's
 * redirect to /login instead of answering about the chain`.
 *
 * Accessible names: the region is `Chain integrity` — a second region beside
 * 24ba's `Audit log` on the same screen — and its one control is `Verify
 * integrity`.
 */

/**
 * The sentence for a check that did not come back as a verdict.
 *
 * Not `FAILURE_MESSAGES.default` ("Something went wrong. Nothing was changed."),
 * which is a claim about a mutation this surface never makes, and not a sentence
 * saying the chain is BROKEN — unchecked and broken are opposite facts, and only
 * the first is known here. Pinned by `::pins the read failure sentence exactly`.
 */
export const AUDIT_VERIFY_READ_MESSAGE = "The chain could not be checked. Try again in a moment.";

/** An earlier change's answer, as this panel holds it. */
type ChainVerdict = { ok: boolean; brokenAt: number | null };

export function AuditVerify() {
  const verify = useMutation<ChainVerdict | null>({ mutationFn: fetchVerify });

  const verdict = verify.isSuccess ? verify.data : null;
  const refusal = verify.isError ? errorMessage(verify.error) : null;

  return (
    <section aria-label="Chain integrity" className="flex max-w-5xl flex-col gap-3">
      <h2 className="text-base font-semibold">Chain integrity</h2>

      <div>
        <Button type="button" disabled={verify.isPending} onClick={() => verify.mutate()}>
          Verify integrity
        </Button>
      </div>

      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {refusal}
        </p>
      ) : null}

      {verdict !== null ? (
        <p className="text-sm">
          {/* A broken chain is a SUCCESSFUL read whose content is bad news, so
              it is a verdict and never an alert: rendering it as a failure
              would report tampering as an outage. The seq is named because it
              is the single fact plan 07 computed — "the chain is broken" alone
              leaves an auditor to search the log by hand for the event that
              does not follow from the one before it. */}
          {verdict.ok
            ? "The chain is intact."
            : `The chain is broken at event ${verdict.brokenAt}.`}
        </p>
      ) : null}
    </section>
  );
}

/**
 * A check the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so no
 * free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
class VerifyRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the chain check was refused");
    this.code = code;
  }
}

/**
 * Whether a body's `code` is one of the design's eight, which is what separates
 * this panel's two unhappy paths. `default` is the shared
 * map's fallback key and never a wire value; the proxy's own
 * `UPSTREAM_UNAVAILABLE` is outside the vocabulary and so is a failed read.
 */
function isTypedCode(code: unknown): code is string {
  return typeof code === "string" && code !== "default" && code in FAILURE_MESSAGES;
}

/** The sentence for whatever went wrong, one alert either way. */
function errorMessage(error: unknown): string {
  if (error instanceof VerifyRefusedError) {
    return failureMessage(error.code);
  }
  return AUDIT_VERIFY_READ_MESSAGE;
}

/**
 * The chain's verdict, or `null` when the browser was sent to `/login` instead.
 *
 * No query string and no body: the route has nothing to ask for. The status is
 * read BEFORE the body, because the proxy's own `502 UPSTREAM_UNAVAILABLE` is
 * valid JSON and a reader that skipped it would turn an outage into a verdict.
 */
async function fetchVerify(): Promise<ChainVerdict | null> {
  const response = await fetch("/api/admin/audit/verify", {
    headers: { accept: "application/json" },
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return null;
  }
  if (!response.ok) {
    const code = await typedCodeOf(response);
    if (code !== null) {
      throw new VerifyRefusedError(code);
    }
    throw new Error(`the audit chain could not be checked (${response.status})`);
  }

  const parsed = auditVerifySchema.safeParse(await bodyOf(response));
  if (!parsed.success) {
    throw new Error("the verify payload did not match what this panel reads");
  }
  return { ok: parsed.data.ok, brokenAt: parsed.data.broken_at };
}

/** The parsed body, or `undefined` when it was not JSON at all. */
async function bodyOf(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/**
 * The typed code a refused response carries, or null when it carries none — a
 * body that is not JSON at all included.
 */
async function typedCodeOf(response: Response): Promise<string | null> {
  const body = await bodyOf(response);
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const code = (body as { code?: unknown }).code;
  return isTypedCode(code) ? code : null;
}
