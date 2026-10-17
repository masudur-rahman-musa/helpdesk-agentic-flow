"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { type MyGrant, myAccessSchema } from "@/features/access/schema";
import { renewalHref } from "@/features/chat/prefill";
import { daysUntil } from "@/features/access/expiry";
import { API } from "@/lib/api-paths";
import { readWire } from "@/lib/wire-read";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

/**
 * What this user holds right now, from `GET /api/me/access`.
 *
 * The same shape as `RequestList`: a client read through TanStack Query
 * (decision A) made by `lib/wire-read.ts`, whose `null` — the
 * browser already following the proxy's redirect to `/login` (22b decision E) —
 * is mapped here to the empty list, and a failed read thrown rather than
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
      <PageHeader title="Active access" />

      {access.isError ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          Your access could not be read. Try again in a moment.
        </p>
      ) : null}

      {access.isSuccess && access.data.length === 0 ? (
        <EmptyState title="You hold no active access." />
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
  const expiry = expiryBadge(grant.expires_at);
  return (
    <Card as="li" className="flex flex-col gap-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{grant.role_name}</span>
        <span className="text-muted-foreground">{grant.system_name}</span>
        <span className="text-muted-foreground">{grant.privilege}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
        <span>Granted {grant.granted_at.slice(0, 10)}</span>
        <Badge tone={expiry.tone}>{expiry.text}</Badge>
      </div>

      <Link
        href={renewalHref(grant)}
        aria-label={`Request renewal for ${grant.role_name}`}
        className="self-start rounded-sm underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Request renewal
      </Link>
    </Card>
  );
}

/**
 * The expiry as a word and a hand-picked tone: `No expiry` for a
 * standing grant and the raw value for a timestamp that will not parse both read
 * neutral, an expired grant (a negative countdown) reads neutral as the status
 * map's `expired` does, a grant with a week or less left reads warning, and
 * anything further out reads success.
 */
function expiryBadge(expiresAt: string | null): { text: string; tone: BadgeTone } {
  if (expiresAt === null) {
    return { text: "No expiry", tone: "neutral" };
  }
  const days = daysUntil(expiresAt, new Date());
  if (days === null) {
    return { text: expiresAt, tone: "neutral" };
  }
  const text = `${days} days left`;
  if (days < 0) {
    return { text, tone: "neutral" };
  }
  if (days <= 7) {
    return { text, tone: "warning" };
  }
  return { text, tone: "success" };
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
