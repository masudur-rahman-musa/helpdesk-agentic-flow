"use client";

import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { useContext } from "react";

import { API } from "@/lib/api-paths";
import { meSchema, type Me } from "@/lib/me-schema";
import { queryKeys } from "@/lib/query-keys";
import type { LoginInput } from "@/lib/schemas";
import { readWire } from "@/lib/wire-read";

/**
 * The shell's one shared read of `/api/me`. `undefined` while pending, after a
 * failure, or with no query provider; callers then show the safe minimum. No
 * retry: a failed read already has a safe answer.
 */
export function useMe(): Me | undefined {
  const client = useContext(QueryClientContext);
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return client === undefined ? undefined : useMeQuery();
}

function useMeQuery(): Me | undefined {
  const me = useQuery({ queryKey: queryKeys.me, queryFn: fetchMe, retry: false });
  return me.data ?? undefined;
}

async function fetchMe(): Promise<Me | null> {
  return readWire(API.me, meSchema);
}

/** `false` for a refusal or a network failure alike: the form shows one message. */
export async function logIn(fields: LoginInput): Promise<boolean> {
  try {
    const response = await fetch(API.login, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(fields),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** The cookie is `httpOnly`, so only the server can expire it. */
export async function logOut(): Promise<void> {
  await fetch(API.logout, { method: "POST" });
}
