"use client";

import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { useContext } from "react";

import { API } from "@/lib/api-paths";
import { meSchema, type Me } from "@/lib/me-schema";
import { queryKeys } from "@/lib/query-keys";
import type { LoginInput } from "@/lib/schemas";
import { readWire } from "@/lib/wire-read";

/**
 * The one read of `/api/me` the shell shares.
 *
 * `MainNav` and `UserMenu` call this with the same query key, so one request
 * answers both. `undefined` while the read is pending, after it failed and
 * when no `QueryClientProvider` is above (the provider-free renders in
 * `tests/shell.test.tsx`, the same guard `useUnreadCount` carries); every
 * caller treats `undefined` as "show the four universal tabs and no menu"
 * (decision E), never more. `retry: false` because a failed read already has
 * a safe answer and a retry loop would hold the pending state open.
 */
export function useMe(): Me | undefined {
  const client = useContext(QueryClientContext);
  // The branch is unreachable in the app, where `QueryProvider` wraps the shell.
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

/**
 * Sign in with fields `loginSchema` already accepted. `true` when the server
 * set the cookie; `false` for a refusal or a dropped network alike, because the
 * form shows one message for every failure (`tests/features/auth/auth-api.test.ts`).
 */
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

/**
 * Ask the server to expire the session cookie. The browser cannot clear it
 * itself, because it is `httpOnly`.
 */
export async function logOut(): Promise<void> {
  await fetch(API.logout, { method: "POST" });
}
