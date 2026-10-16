import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";

/**
 * The shared not-found content: a short "page not found" message and
 * one link back to `/chat`. Both the signed-in group's `not-found.tsx` and the
 * root `not-found.tsx` render it, so the two screens read the same. It holds no
 * nav of its own, which is why the root screen (above the signed-in group) can
 * use it without pulling the shell in (`tests/app-states.test.tsx`).
 */
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
