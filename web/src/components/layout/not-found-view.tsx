import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";

/** Shared not-found content. No nav, so the signed-out root screen can use it. */
export function NotFoundView() {
  return (
    <EmptyState
      title="Page not found"
      description="That page does not exist. It may have moved, or the link was wrong."
      action={
        <Link href="/chat" className={buttonVariants()}>
          Back to chat
        </Link>
      }
    />
  );
}
