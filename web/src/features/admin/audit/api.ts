import { useMutation, useQuery } from "@tanstack/react-query";

import {
  type AuditEventRow,
  auditSearchSchema,
  auditVerifySchema,
} from "@/features/admin/audit/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** The applied filters, as the wire parameters they already are. */
export type AppliedFilters = Readonly<Record<string, string>>;

/** One page of the log, as the search reads it. */
export type SearchPage = { events: AuditEventRow[]; nextBeforeSeq: number | null };

/** One page of the audit log, keyed by everything that picks the page. */
export function useAuditSearch(
  filters: AppliedFilters,
  pageSize: number,
  beforeSeq: number | null,
) {
  return useQuery({
    queryKey: queryKeys.auditSearch(filters, pageSize, beforeSeq),
    queryFn: () => fetchSearch(filters, pageSize, beforeSeq),
  });
}

/**
 * An earlier change's answer as an earlier change left it, as this panel holds it: one shape with
 * all four facts, never a second type beside it, so `verdictSentence` cannot be
 * called with half an answer.
 */
export type ChainVerdict = {
  ok: boolean;
  brokenAt: number | null;
  truncatedAt: number | null;
  anchoredSeq: number | null;
};

/**
 * The chain check, fired on a click and never on paint: a chain walk is a full
 * scan of this tenant's log.
 */
export function useAuditVerify() {
  return useMutation<ChainVerdict | null>({ mutationFn: fetchVerify });
}

/**
 * One page of the log for the applied filters, the chosen size and the cursor.
 *
 * `before_seq` belongs to paging alone and is never a form field: it is the
 * `next_before_seq` the tool itself handed back, so the page is never asked for
 * a boundary this code invented.
 */
async function fetchSearch(
  filters: AppliedFilters,
  pageSize: number,
  beforeSeq: number | null,
): Promise<SearchPage> {
  const params = new URLSearchParams({ limit: String(pageSize) });
  for (const [name, value] of Object.entries(filters)) {
    params.set(name, value);
  }
  if (beforeSeq !== null) {
    params.set("before_seq", String(beforeSeq));
  }

  const payload = await readWire(`${API.auditSearch}?${params.toString()}`, auditSearchSchema);
  if (payload === null) {
    // The redirect answers what this read answers when it finds nothing, so no
    // third state appears on screen while the browser navigates to `/login`
    //.
    return { events: [], nextBeforeSeq: null };
  }
  return { events: payload.events, nextBeforeSeq: payload.next_before_seq };
}

/**
 * The chain's verdict, or `null` when the browser was sent to `/login` instead.
 *
 * No query string and no body: the route has nothing to ask for.
 */
async function fetchVerify(): Promise<ChainVerdict | null> {
  const payload = await readWire(API.auditVerify, auditVerifySchema);
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
