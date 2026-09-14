"use client";

import { useMutation } from "@tanstack/react-query";

import { Button } from "@/components/ui/field";
import { failureMessage } from "@/lib/chat-errors";
import { type PolicyReingestResult, policyReingestResultSchema } from "@/lib/policies-schema";
// The vocabulary check alone: `readWire` is a GET and cannot express this POST,
// so the fetch below stays as it is while the design's eight codes are checked in
// the one place that holds them, exactly as
// `kill-switch-toggle.tsx` imports it.
import { isTypedCode } from "@/lib/wire-read";

/**
 * What one version of one policy document can DO: make the file on the ingest
 * host live again.
 *
 * One instance per rendered control, holding its own minted key, its own
 * in-flight mutation and its own alert (§2), `KillSwitchToggle`'s shape (plan
 * 24db): one in-flight flag for the whole board would grey out every other
 * version and every other document for most of a minute while one file is read
 * and embedded. Pinned by `tests/policies-reingest.test.tsx::disables only the
 * acting control while its re-ingest is in flight`.
 *
 * The ARMING is the one piece of state that is not this component's: it lives
 * in the versions table, because §1 requires a click on any other control in
 * the same document to clear it, and a flag held per control cannot see its
 * siblings (31b Deviations). What stays here is when to arm and what the armed
 * control is called. Pinned by `::disarms when another control in the same
 * document is clicked`.
 *
 * a project rule holds without an exception: the mutation is
 * `reingest_policy_document`, a typed MCP tool behind RBAC and the kill switch,
 * reached through 31a's proxy under the caller's own bearer. The LLM is not in
 * the path at all — the tool is deliberately absent from the agent's catalogue
 * (31a decision G) — so the only caller is a person clicking a button. Nothing
 * here decides anything: which tenant's corpus is rewritten, whether the caller
 * may rewrite it, whether the tool is stopped, and what the `policy.reingested`
 * event says are all decided behind the MCP boundary.
 *
 * The body is exactly three keys (§1). No tenant id — the tenant is the
 * token's, and a browser naming one would be a project rule written into the page
 * — and no `sha256`, because the tool reads a version DIRECTORY and re-computes
 * the digest itself, so a digest off a board painted minutes ago would be a
 * second opinion about what is on disk. Pinned as an EXACT object by `::posts
 * its exact three-key body`.
 *
 * The idempotency key is a fresh `crypto.randomUUID()` per click (decision B).
 * A key derived from the row would be stable, which is the defect: the same key
 * with the same arguments REPLAYS, so an admin who edits one file twice would
 * get the first answer handed back and the second edit would never reach the
 * corpus. It is also the only value this page puts on the wire that a person
 * did not pick off the board, and its pre-check is the idempotency guard's own
 * length check against the column it lands in (§1); this page adds no second
 * opinion, and `::mints a fresh idempotency key on every click` asserts the 36
 * characters reach the proxy whole.
 *
 * An expired session at the click is `response.redirected` rather than a
 * status, because the browser has already followed the proxy's redirect by the
 * time this code runs (22b decision E). A board sits open on a screen for
 * minutes, so a session expiring between the paint and the click is the common
 * case, which `::follows the proxy's redirect to /login, with no alert and no
 * re-read` pins.
 *
 * Accessible names: the control carries the SLUG and the version (`Re-ingest
 * {slug} {version}`), because two documents otherwise carry a control with the
 * same name, and the armed form spells out what the second click does
 * (`Confirm re-ingest {slug} {version}, replacing the live version`).
 */

/**
 * The sentence for a re-ingest whose answer said nothing usable (§1's second
 * unhappy path).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`: the question an admin
 * has here is whether the edited file LANDED, and "something went wrong" leaves
 * it open over a corpus the retriever answers from. Pinned verbatim by
 * `tests/policies-reingest.test.tsx::pins the failure and the two override
 * sentences exactly`.
 */
export const REINGEST_FAILED_MESSAGE =
  "The re-ingest could not be completed. Try again in a moment.";

/**
 * The `NOT_FOUND` sentence on this surface, and only on this surface.
 *
 * `FAILURE_MESSAGES.NOT_FOUND` is "That could not be found.", which says
 * nothing about WHICH thing, when the only useful fact is that the corpus file
 * is missing on the ingest host — the admin fixes that by putting the file
 * there, not by retrying. 22b's `tests/chat-schema.test.ts` pins every string
 * in that map verbatim, so widening the shared map is not available.
 */
export const REINGEST_NOT_FOUND_MESSAGE = "No file for that version was found on the ingest host.";

/**
 * The `VALIDATION` sentence on this surface, and only on this surface.
 *
 * The shared one is "That message could not be sent.", which is about a chat
 * message. Exactly TWO codes are overridden — every other sentence still comes
 * from the shared map, so no second opinion about `FORBIDDEN` or
 * `TOOL_DISABLED` can appear on this page.
 */
export const REINGEST_VALIDATION_MESSAGE =
  "That version could not be re-ingested. Check the document and try again.";

/**
 * The line a re-ingest that WROTE something renders (decision D).
 *
 * A function of the answer's own two values rather than a fixed sentence: the
 * count is what tells an admin whether the edited file ingested as spans or
 * registered as an empty document, and the live half is what says whether the
 * version they refreshed is now the one answering — re-ingesting an older
 * version makes it live (31a decision E). Pinned over both arguments by
 * `tests/policies-reingest.test.tsx::renders the created line with the exact
 * chunk count`.
 */
export function REINGEST_CREATED_TEXT(chunkCount: number, isCurrent: boolean): string {
  const chunks = `${chunkCount} ${chunkCount === 1 ? "chunk" : "chunks"}`;
  const live = isCurrent ? "This version is now live." : "This version is not the live one.";
  return `Re-ingested: ${chunks} written. ${live}`;
}

/**
 * The line a re-ingest that wrote NOTHING renders (decision D).
 *
 * An unchanged source moves no row on the board, so an invalidate-only control
 * would be indistinguishable from a click that did nothing and the admin would
 * edit the file again. A plain string and not a function: `chunk_count` is 0
 * because nothing was written, so there is no number worth printing. Pinned by
 * `::renders the unchanged line for a source that did not differ`.
 */
export const REINGEST_UNCHANGED_TEXT =
  "The file on the ingest host has not changed. Nothing was re-ingested.";

/** The proxy path 31a built, its own file mirroring agent-api's (31a decision I). */
const REINGEST_PATH = "/api/admin/policies/reingest";

/**
 * What one re-ingest resolved to: the tool's parsed answer, or `null` while the
 * browser navigates to `/login`.
 *
 * The null is the redirect and nothing else — invalidating a query the page is
 * about to leave behind would fire a read under an expired session, and an
 * alert would blame a re-ingest that was never attempted.
 */
type ReingestOutcome = PolicyReingestResult | null;

export function PoliciesReingest({
  slug,
  version,
  isCurrent,
  armed,
  onArmedChange,
  onChanged,
}: {
  slug: string;
  version: string;
  isCurrent: boolean;
  armed: boolean;
  onArmedChange: (armed: boolean) => void;
  onChanged: () => void;
}) {
  const reingest = useMutation({
    mutationFn: () => postReingest(slug, version),
    onSuccess: (outcome: ReingestOutcome) => {
      if (outcome === null) {
        return;
      }
      onChanged();
    },
  });

  const refusal = reingest.isError ? reingestRefusal(reingest.error) : null;
  const result = reingest.isSuccess ? reingest.data : null;
  const name = armed
    ? `Confirm re-ingest ${slug} ${version}, replacing the live version`
    : `Re-ingest ${slug} ${version}`;

  /**
   * The misclick guard, on the dangerous direction alone (decision C).
   *
   * Re-ingesting a version that is not live MAKES it live, which is the one act
   * on this page an admin can be surprised by; refreshing the version that is
   * already live changes nothing about which version answers, so arming it
   * would tax the ordinary case for the rare one (24db's precedent).
   *
   * Every click that is not an arming clears the arming FIRST, which is what
   * disarms a sibling control in the same document (§1): the table holds one
   * armed version, so clearing it here clears whichever control set it.
   */
  function onClick(): void {
    if (!isCurrent && !armed) {
      onArmedChange(true);
      return;
    }
    onArmedChange(false);
    reingest.mutate();
  }

  return (
    <div className="flex flex-col gap-1">
      <Button type="button" aria-label={name} disabled={reingest.isPending} onClick={onClick}>
        {armed ? "Confirm re-ingest" : "Re-ingest"}
      </Button>

      {refusal !== null ? (
        <p role="alert" className="text-xs">
          {refusal}
        </p>
      ) : null}

      {result !== null ? (
        <p role="status" className="text-xs text-muted-foreground">
          {result.created
            ? REINGEST_CREATED_TEXT(result.chunk_count, result.is_current)
            : REINGEST_UNCHANGED_TEXT}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A re-ingest the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
class ReingestRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the re-ingest was refused");
    this.code = code;
  }
}

/** The sentence for a refused re-ingest, two codes overridden and no more. */
function reingestRefusal(error: unknown): string {
  if (!(error instanceof ReingestRefusedError)) {
    return REINGEST_FAILED_MESSAGE;
  }
  if (error.code === "NOT_FOUND") {
    return REINGEST_NOT_FOUND_MESSAGE;
  }
  if (error.code === "VALIDATION") {
    return REINGEST_VALIDATION_MESSAGE;
  }
  return failureMessage(error.code);
}

/**
 * One re-ingest, posted to the proxy for this document and this version.
 *
 * Answers `null` when the browser is navigating to `/login`, so the caller
 * files nothing and re-reads nothing.
 */
async function postReingest(slug: string, version: string): Promise<ReingestOutcome> {
  const response = await fetch(REINGEST_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      slug,
      version,
      idempotency_key: crypto.randomUUID(),
    }),
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return null;
  }
  if (!response.ok) {
    // The status is read BEFORE the body: the proxy's own `502
    // UPSTREAM_UNAVAILABLE` is valid JSON carrying a code outside the design,
    // and a reader that skipped the status would answer a refusal sentence for
    // an outage.
    const body = await readJson(response);
    const code =
      typeof body === "object" && body !== null ? (body as { code?: unknown }).code : undefined;
    if (isTypedCode(code)) {
      throw new ReingestRefusedError(code);
    }
    throw new Error(`the re-ingest could not be completed (${response.status})`);
  }

  // Parsed rather than cast: an answer this parser refuses is a FAILURE, never
  // a status line claiming an ingest of `undefined` chunks and a confident
  // refetch on the strength of a broken reply (§1).
  const parsed = policyReingestResultSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error("the re-ingest answer did not match what this page reads");
  }
  return parsed.data;
}

/** The body as JSON, or `undefined` when it is not JSON at all. */
async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
