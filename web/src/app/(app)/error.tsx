"use client";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";

/**
 * The error screen for every signed-in page. Next.js renders it in
 * place of the page that threw, inside the group layout, and hands it the
 * error and a `reset` that re-renders the segment. A client component, because
 * `reset` is a callback and the "Try again" button is a click (`tests/
 * app-states.test.tsx`).
 */
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <EmptyState
      title="Something went wrong"
      description={error.message}
      action={
        <Button type="button" onClick={reset}>
          Try again
        </Button>
      }
    />
  );
}
