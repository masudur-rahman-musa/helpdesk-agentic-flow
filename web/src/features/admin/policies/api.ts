import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type PolicyDocument,
  policyDocumentsBoardSchema,
  type PolicyReingestResult,
  policyReingestResultSchema,
} from "@/features/admin/policies/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
// The vocabulary check alone: `readWire` is a GET and cannot express the
// re-ingest POST, so its fetch stays as it is while the design's eight codes are
// checked in the one place that holds them.
import { isTypedCode, readWire } from "@/lib/wire-read";

/**
 * What the board answered, or `null` while the browser navigates away.
 *
 * The null is the redirect and nothing else: rendering the empty state for an
 * expired session would tell somebody who is not even signed in that the tenant
 * has ingested no policy at all, and rendering the alert would blame a read
 * that was fine (§5's second risk, a project rule's fourth half).
 */
export type BoardAnswer = PolicyDocument[] | null;

/** The policy documents board, read through TanStack Query. */
export function usePoliciesBoard() {
  return useQuery({ queryKey: queryKeys.policiesBoard, queryFn: fetchBoard });
}

/**
 * Re-read the board. Invalidation and never an optimistic rewrite: the tool
 * decides what the rows become, including which version is live.
 */
export function useRefreshPoliciesBoard(): () => void {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.policiesBoard });
  };
}

/**
 * One version's re-ingest. `onChanged` runs only when the tool answered, never
 * on the redirect to `/login`.
 */
export function usePolicyReingest(slug: string, version: string, onChanged: () => void) {
  return useMutation({
    mutationFn: () => postReingest(slug, version),
    onSuccess: (outcome: ReingestOutcome) => {
      if (outcome !== null) {
        onChanged();
      }
    },
  });
}

/**
 * Every document the tenant has ingested, in the tool's own order, or `null`
 * while the browser follows the proxy's redirect to `/login`.
 */
async function fetchBoard(): Promise<BoardAnswer> {
  const parsed = await readWire(API.policies, policyDocumentsBoardSchema);
  return parsed === null ? null : parsed.documents;
}

/**
 * What one re-ingest resolved to: the tool's parsed answer, or `null` while the
 * browser navigates to `/login`.
 *
 * The null is the redirect and nothing else — invalidating a query the page is
 * about to leave behind would fire a read under an expired session, and an
 * alert would blame a re-ingest that was never attempted.
 */
export type ReingestOutcome = PolicyReingestResult | null;

/**
 * A re-ingest the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
export class ReingestRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the re-ingest was refused");
    this.code = code;
  }
}

/**
 * One re-ingest, posted to the proxy for this document and this version.
 *
 * Answers `null` when the browser is navigating to `/login`, so the caller
 * files nothing and re-reads nothing.
 */
async function postReingest(slug: string, version: string): Promise<ReingestOutcome> {
  const response = await fetch(API.policyReingest, {
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
