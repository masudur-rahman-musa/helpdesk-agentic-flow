/**
 * The loading screen for every signed-in page. Next.js shows it
 * inside the group layout while a page fetches, so the shell stays put and only
 * the content area says it is working. `role="status"` makes a screen reader
 * announce the wait (`tests/app-states.test.tsx`).
 */
export default function Loading() {
  return (
    <div
      role="status"
      className="flex items-center justify-center py-16 text-sm text-muted-foreground"
    >
      Loading…
    </div>
  );
}
