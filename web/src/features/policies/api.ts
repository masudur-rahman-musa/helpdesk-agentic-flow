import { useQuery } from "@tanstack/react-query";

import { type PolicyRead, policyReadSchema } from "@/features/policies/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire, WireRefusedError } from "@/lib/wire-read";

/**
 * One read of the access rules. `null` asks for the list alone. `enabled`
 * waits for a slug that is not known yet. The answer is `null` only while the
 * browser follows the redirect to `/login`. A typed refusal is not tried
 * again: a missing document stays missing.
 */
export function usePolicyRead(slug: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.policies(slug),
    queryFn: () => fetchPolicies(slug),
    enabled,
    retry: (failures, error) => !(error instanceof WireRefusedError) && failures < 1,
  });
}

async function fetchPolicies(slug: string | null): Promise<PolicyRead | null> {
  const path =
    slug === null ? API.readPolicies : `${API.readPolicies}?slug=${encodeURIComponent(slug)}`;
  return readWire(path, policyReadSchema);
}
