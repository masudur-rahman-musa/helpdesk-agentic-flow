"use client";

import Link from "next/link";

import { useMyAccess } from "@/features/access/api";
import type { MyGrant } from "@/features/access/schema";
import { renewalHref } from "@/features/chat/prefill";
import { daysUntil } from "@/features/access/expiry";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";
import { formatIsoDate } from "@/lib/date-format";

/**
 * What this user holds now. A failed read shows an error, never the empty
 * list: "you hold no access" would make people ask again for access they have.
 * Each renewal link names its role, so a screen reader does not hear a list of
 * identical links.
 */
export function GrantList() {
  const access = useMyAccess();

  return (
    <section aria-label="Active access" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="Active access" />

      {access.isError ? <Alert>Your access could not be read. Try again in a moment.</Alert> : null}

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
        <span>Granted {formatIsoDate(grant.granted_at)}</span>
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

/** A grant with no end date reads `No expiry`, never "ends today". */
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
