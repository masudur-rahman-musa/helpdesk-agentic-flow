import { useQuery } from "@tanstack/react-query";

import { type MyGrant, myAccessSchema } from "@/features/access/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** This user's grants, read through TanStack Query. */
export function useMyAccess() {
  return useQuery({ queryKey: queryKeys.myAccess, queryFn: fetchGrants });
}

/**
 * Every grant this user holds, or the empty list while the browser follows the
 * proxy's redirect to `/login`.
 *
 * A refusal and a failed read both THROW out of `readWire`, and this page
 * dresses neither (24ia decision B): the section's own alert sentence is what a
 * holder reads for a typed 403 and for a bodyless 500 alike, pinned by
 * `tests/access-page.test.tsx::a 403 FORBIDDEN renders this page's own read
 * sentence, not the code's`.
 */
async function fetchGrants(): Promise<MyGrant[]> {
  const parsed = await readWire(API.myAccess, myAccessSchema);
  return parsed === null ? [] : parsed.grants;
}
