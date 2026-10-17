import { useMutation, useQuery } from "@tanstack/react-query";

import {
  type AuditEventRow,
  auditSearchSchema,
  auditVerifySchema,
} from "@/features/admin/audit/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

export type AppliedFilters = Readonly<Record<string, string>>;

export type SearchPage = { events: AuditEventRow[]; nextBeforeSeq: number | null };

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

export type ChainVerdict = {
  ok: boolean;
  brokenAt: number | null;
  truncatedAt: number | null;
  anchoredSeq: number | null;
};

/** Runs on a click, never on load: a chain check scans the whole log. */
export function useAuditVerify() {
  return useMutation<ChainVerdict | null>({ mutationFn: fetchVerify });
}

/** `before_seq` is only ever the cursor the server handed back. */
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
    // Redirecting to `/login`: show an empty page meanwhile.
    return { events: [], nextBeforeSeq: null };
  }
  return { events: payload.events, nextBeforeSeq: payload.next_before_seq };
}

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
