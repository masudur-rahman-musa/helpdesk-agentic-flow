"use client";

import { useMutation } from "@tanstack/react-query";

import { auditVerifySchema } from "@/lib/audit-schema";
import { failureMessage } from "@/lib/chat-errors";
import { WireRefusedError, readWire } from "@/lib/wire-read";
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
 * is intact, where it broke, where it was cut and which head it was measured
 * against all come from an earlier change's route as an earlier change left it; this renders one
 * sentence about the answer and one line naming the head.
 *
 * The read goes through `lib/wire-read.ts`, which owns the redirect, the
 * status, the the design vocabulary check and the `safeParse` for every board
 * that talks to `app/api/`. What stays here is the sentence: a
 * `WireRefusedError` renders its code's shared sentence and every other failure
 * renders `AUDIT_VERIFY_READ_MESSAGE`, with no `VALIDATION` override on this
 * surface unlike `AuditSearch` — the route takes no argument, so there is
 * nothing an auditor could correct. Pinned by the two `$name renders its own
 * sentence and not the read failure` rows, the three `$name renders the read
 * failure and no verdict` rows, and `::follows the proxy's redirect to /login
 * instead of answering about the chain`.
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

/**
 * An earlier change's answer as an earlier change left it, as this panel holds it: one shape with
 * all four facts, never a second type beside it, so `verdictSentence` cannot be
 * called with half an answer.
 */
type ChainVerdict = {
  ok: boolean;
  brokenAt: number | null;
  truncatedAt: number | null;
  anchoredSeq: number | null;
};

/**
 * The one sentence for one answer.
 *
 * `broken_at` wins where both seqs arrive non-null — a shape `verify_chain`
 * never emits, because it answers the earlier and more specific fact when a
 * chain is both edited and shortened. Two sentences on screen would be this
 * page inventing a verdict shape the engine does not have. Pinned by
 * `tests/audit-verify-panel.test.tsx::renders only the broken sentence when
 * both seqs arrive non-null`.
 *
 * The truncated sentence says MISSING rather than "truncated at", because
 * `truncated_at` is a seq that is gone: an auditor sent to event 902 would find
 * no row there. Pinned by `::renders the missing-tail sentence for a truncated
 * chain and no alert`.
 *
 * A false `ok` with neither seq is the fallback, and it never claims intact and
 * never prints a null into a sentence — the defect this function replaces.
 * Pinned by `::renders the did-not-verify sentence and never the words event
 * null`.
 */
function verdictSentence(verdict: ChainVerdict): string {
  if (verdict.ok) {
    return "The chain is intact.";
  }
  if (verdict.brokenAt !== null) {
    return `The chain is broken at event ${verdict.brokenAt}.`;
  }
  if (verdict.truncatedAt !== null) {
    return `The chain is missing event ${verdict.truncatedAt} and everything after it.`;
  }
  return "The chain did not verify.";
}

/**
 * Which head the check was measured against, on EVERY verdict including the
 * failed ones: an intact chain checked against an anchor and an intact chain
 * with no anchor at all are different amounts of evidence, and a reader of a
 * break needs to know what it was compared to. Pinned by `::renders the intact
 * sentence and the no-anchor line for a chain never anchored`.
 */
function anchorLine(anchoredSeq: number | null): string {
  if (anchoredSeq === null) {
    return "No head has been anchored yet, so a removed tail would not show here.";
  }
  return `Checked against the anchored head, event ${anchoredSeq}.`;
}

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
        <div className="flex flex-col gap-1">
          {/* A broken or truncated chain is a SUCCESSFUL read whose content is
              bad news, so it is a verdict and never an alert: rendering it as a
              failure would report tampering as an outage. The seq is named
              because it is the single fact plan 07 computed — "the chain is
              broken" alone leaves an auditor to search the log by hand for the
              event that does not follow from the one before it. */}
          <p className="text-sm">{verdictSentence(verdict)}</p>
          <p className="text-sm text-muted-foreground">{anchorLine(verdict.anchoredSeq)}</p>
        </div>
      ) : null}
    </section>
  );
}

/** The sentence for whatever went wrong, one alert either way. */
function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return AUDIT_VERIFY_READ_MESSAGE;
}

/**
 * The chain's verdict, or `null` when the browser was sent to `/login` instead.
 *
 * No query string and no body: the route has nothing to ask for.
 */
async function fetchVerify(): Promise<ChainVerdict | null> {
  const payload = await readWire("/api/admin/audit/verify", auditVerifySchema);
  if (payload === null) {
    return null;
  }
  return {
    ok: payload.ok,
    brokenAt: payload.broken_at,
    truncatedAt: payload.truncated_at,
    anchoredSeq: payload.anchored_seq,
  };
}
