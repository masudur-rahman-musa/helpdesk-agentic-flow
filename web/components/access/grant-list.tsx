"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { type MyGrant, myAccessSchema } from "@/lib/access-schema";
import { renewalHref } from "@/lib/chat-prefill";
import { daysUntil } from "@/lib/expiry";

/**
 * What this user holds right now, from `GET /api/me/access`.
 *
 * The same shape as `RequestList`: a client read through TanStack Query
 * (decision A), the expired session detected as `response.redirected` rather
 * than a status (22b decision E), and a failed read thrown rather than
 * answered as an empty list — "you hold no access" over a broken proxy is the
 * sentence on this page a person might act on by asking again for access they
 * already have (`tests/access-page.test.tsx`'s two `a read that fails` rows
 * against `::renders the named empty state and no list`).
 *
 * Accessible names: the region is `Active access`, the rows are the list
 * `Grants`, and each row's renewal link is named `Request renewal for
 * {role_name}` — one row per role means a link named "Renew" on every row is a
 * list of identical links to a screen reader.
 *
 * The countdown is `daysUntil` and never a rendered timestamp, and a NULL
 * `expires_at` reads `No expiry`: a standing grant run through the arithmetic
 * would tell its holder access ends today. An unreadable timestamp falls back
 * to the raw value rather than `NaN days left`. The granted date
 * is the ISO prefix, never a locale format.
 */
export function GrantList() {
  const access = useQuery({ queryKey: ["me", "access"], queryFn: fetchGrants });

  return (
    <section aria-label="Active access" className="flex max-w-3xl flex-col gap-4">
      <h1 className="text-lg font-semibold">Active access</h1>

      {access.isError ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          Your access could not be read. Try again in a moment.
        </p>
      ) : null}

      {access.isSuccess && access.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">You hold no active access.</p>
      ) : null}

      {access.isSuccess && access.data.length > 0 ? (
        <ul aria-label="Grants" className="flex flex-col gap-3">
          {access.data.map((grant) => (
            <GrantRow key={grant.grant_id} grant={grant} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function GrantRow({ grant }: { grant: MyGrant }) {
  return (
    <li className="flex flex-col gap-2 rounded-md border border-border p-4 text-sm">
      <div className="flex flex-wrap gap-2">
        <span className="font-medium">{grant.role_name}</span>
        <span className="text-muted-foreground">{grant.system_name}</span>
        <span className="text-muted-foreground">{grant.privilege}</span>
      </div>

      <div className="flex flex-wrap gap-2 text-muted-foreground">
        <span>Granted {grant.granted_at.slice(0, 10)}</span>
        <span>{expiryLabel(grant.expires_at)}</span>
      </div>

      <Link
        href={renewalHref(grant)}
        aria-label={`Request renewal for ${grant.role_name}`}
        className="self-start rounded-sm underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Request renewal
      </Link>
    </li>
  );
}

/** The countdown, `No expiry` for a standing grant, the raw value if unreadable. */
function expiryLabel(expiresAt: string | null): string {
  if (expiresAt === null) {
    return "No expiry";
  }
  const days = daysUntil(expiresAt, new Date());
  return days === null ? expiresAt : `${days} days left`;
}

async function fetchGrants(): Promise<MyGrant[]> {
  const response = await fetch("/api/me/access", { headers: { accept: "application/json" } });

  if (response.redirected) {
    window.location.assign(response.url);
    return [];
  }
  if (!response.ok) {
    // Thrown rather than swallowed: the proxy's own `502
    // UPSTREAM_UNAVAILABLE` is valid JSON, so a reader that skipped the status
    // would turn an outage into "you hold nothing".
    throw new Error(`your access could not be read (${response.status})`);
  }

  const parsed = myAccessSchema.safeParse(await response.json());
  if (!parsed.success) {
    throw new Error("the access payload did not match what this page reads");
  }
  return parsed.data.grants;
}
