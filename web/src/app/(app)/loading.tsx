/**
 * The loading screen for every signed-in page, drawn inside the shell.
 * `role="status"` lets a screen reader announce the wait.
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
