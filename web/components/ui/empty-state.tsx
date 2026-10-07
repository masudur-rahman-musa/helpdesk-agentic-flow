import { cn } from "@/lib/utils";

/**
 * What a list says when it has nothing in it. Every other prop reaches the
 * root, so a page keeps the `role="status"` or the label its tests find it by
 * (`tests/ui-primitives.test.tsx::renders the empty state text and passes
 * props through`).
 */
export function EmptyState({
  title,
  description,
  action,
  className,
  ...props
}: Omit<React.ComponentProps<"div">, "title"> & {
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center",
        className,
      )}
      {...props}
    >
      <p className="text-sm font-medium text-foreground">{title}</p>
      {description !== undefined ? (
        <p className="text-sm text-muted-foreground">{description}</p>
      ) : null}
      {action !== undefined ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
