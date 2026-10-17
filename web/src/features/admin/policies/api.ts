import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type PolicyDocument,
  policyDocumentsBoardSchema,
  type PolicyReingestResult,
  policyReingestResultSchema,
} from "@/features/admin/policies/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
// `readWire` only does GETs, so the re-ingest POST reuses just its code check.
import { isTypedCode, readWire } from "@/lib/wire-read";

/** `null` only while the browser follows the redirect to `/login`. */
export type BoardAnswer = PolicyDocument[] | null;

export function usePoliciesBoard() {
  return useQuery({ queryKey: queryKeys.policiesBoard, queryFn: fetchBoard });
}

/** Re-read, never an optimistic update: the server decides which version is live. */
export function useRefreshPoliciesBoard(): () => void {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.policiesBoard });
  };
}

/** `onChanged` runs only when the server answered, never on a redirect. */
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

async function fetchBoard(): Promise<BoardAnswer> {
  const parsed = await readWire(API.policies, policyDocumentsBoardSchema);
  return parsed === null ? null : parsed.documents;
}

/** The parsed answer, or `null` while redirecting to `/login`. */
export type ReingestOutcome = PolicyReingestResult | null;

/** Carries only the typed code; the page picks the sentence. */
export class ReingestRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the re-ingest was refused");
    this.code = code;
  }
}

/** A fresh idempotency key per click: a reused key would replay the old answer. */
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
    // Check the status first: the proxy's own 502 body is valid JSON too.
    const body = await readJson(response);
    const code =
      typeof body === "object" && body !== null ? (body as { code?: unknown }).code : undefined;
    if (isTypedCode(code)) {
      throw new ReingestRefusedError(code);
    }
    throw new Error(`the re-ingest could not be completed (${response.status})`);
  }

  // Parsed, not cast: a malformed answer is a failure, not "undefined chunks".
  const parsed = policyReingestResultSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error("the re-ingest answer did not match what this page reads");
  }
  return parsed.data;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
