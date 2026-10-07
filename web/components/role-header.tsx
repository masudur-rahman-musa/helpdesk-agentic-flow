"use client";

import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { useContext } from "react";

import { PRINCIPAL_LABELS, principalLabel } from "@/lib/decision-labels";
import { meSchema, type Me } from "@/lib/me-schema";
import { readWire } from "@/lib/wire-read";

/**
 * The one read of `/api/me` the shell shares.
 *
 * `MainNav` and `RoleHeader` call this with the same query key, so one request
 * answers both. `undefined` while the read is pending, after it failed and
 * when no `QueryClientProvider` is above (the provider-free renders in
 * `tests/shell.test.tsx`, the same guard `NotificationBell` carries); every
 * caller treats `undefined` as "show the four universal tabs and no strip"
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
  const me = useQuery({ queryKey: ["me"], queryFn: fetchMe, retry: false });
  return me.data ?? undefined;
}

async function fetchMe(): Promise<Me | null> {
  return readWire("/api/me", meSchema);
}

/** Principals in the enum's order, not the wire's (decision D). */
function orderedLabels(principals: string[]): string[] {
  const known = Object.keys(PRINCIPAL_LABELS).filter((key) => principals.includes(key));
  const unknown = principals.filter((key) => !(key in PRINCIPAL_LABELS));
  return [...known, ...unknown].map(principalLabel);
}

/**
 * The strip under the brand row: the signed-in person's name and account type
 * in plain words. Renders nothing until the read has answered, and nothing
 * when it failed (`tests/role-nav.test.tsx::shows the four universal links and
 * no strip when $why`).
 */
export function RoleHeader() {
  const me = useMe();
  if (me === undefined) {
    return null;
  }
  return (
    <div
      role="group"
      aria-label="Signed in user"
      className="flex flex-wrap items-center gap-x-2 pb-2 text-xs text-muted-foreground"
    >
      <span className="font-medium text-foreground">{me.display_name}</span>
      <span aria-hidden="true">·</span>
      <span>{orderedLabels(me.principals).join(", ")}</span>
    </div>
  );
}
