import { useQuery } from "@tanstack/react-query";

import { type MyRequest, myRequestsSchema } from "@/features/access/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** This user's requests. */
export function useMyRequests() {
  return useQuery({ queryKey: queryKeys.myRequests, queryFn: fetchRequests });
}

/**
 * Every request this user has filed, or an empty list while the browser follows
 * the redirect to `/login`. Errors throw to the section's own alert.
 */
async function fetchRequests(): Promise<MyRequest[]> {
  const parsed = await readWire(API.myRequests, myRequestsSchema);
  return parsed === null ? [] : parsed.requests;
}
