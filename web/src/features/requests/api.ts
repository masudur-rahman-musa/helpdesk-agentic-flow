import { useQuery } from "@tanstack/react-query";

import { type MyRequest, myRequestsSchema } from "@/features/access/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** This user's requests, read through TanStack Query. */
export function useMyRequests() {
  return useQuery({ queryKey: queryKeys.myRequests, queryFn: fetchRequests });
}

/**
 * Every request this user has filed, or the empty list while the browser
 * follows the proxy's redirect to `/login`.
 *
 * A refusal and a failed read both THROW out of `readWire`, and this page
 * dresses neither (24ia decision B): the section's own alert sentence is what a
 * requester reads for a typed 403 and for a bodyless 500 alike, pinned by
 * `tests/requests-page.test.tsx::a 403 FORBIDDEN renders this page's own read
 * sentence, not the code's`.
 */
async function fetchRequests(): Promise<MyRequest[]> {
  const parsed = await readWire(API.myRequests, myRequestsSchema);
  return parsed === null ? [] : parsed.requests;
}
