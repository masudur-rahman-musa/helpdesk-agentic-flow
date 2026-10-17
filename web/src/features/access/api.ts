import { useQuery } from "@tanstack/react-query";

import { type MyGrant, myAccessSchema } from "@/features/access/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

export function useMyAccess() {
  return useQuery({ queryKey: queryKeys.myAccess, queryFn: fetchGrants });
}

/**
 * Empty while the browser follows the redirect to `/login`. A refusal or a
 * failed read throws, and the page shows one sentence for both.
 */
async function fetchGrants(): Promise<MyGrant[]> {
  const parsed = await readWire(API.myAccess, myAccessSchema);
  return parsed === null ? [] : parsed.grants;
}
