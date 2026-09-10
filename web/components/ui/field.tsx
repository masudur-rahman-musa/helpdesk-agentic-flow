import { cn } from "@/lib/utils";

/**
 * The two primitives this plan actually uses. shadcn/ui is a copy-in library,
 * so what belongs in the repo is the components in use and nothing else —
 * `components.json` is here for the next plan that needs a dialog or a table.
 */

export function Label({ className, ...props }: React.ComponentProps<"label">) {
  return <label className={cn("text-sm font-medium text-foreground", className)} {...props} />;
}

export function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "w-full rounded-md border border-border bg-background px-3 py-2 text-sm",
        "placeholder:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
      {...props}
    />
  );
}

export function Button({ className, ...props }: React.ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}
