import { cn } from "@/lib/utils";

export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "border-border bg-muted text-muted-foreground",
  info: "border-transparent bg-info text-info-foreground",
  success: "border-transparent bg-success text-success-foreground",
  warning: "border-transparent bg-warning text-warning-foreground",
  danger: "border-transparent bg-danger text-danger-foreground",
};

/**
 * Status to colour, one table for every page. `approved` is
 * info rather than success because an approved request has not been executed
 * yet. Pinned as an exact mapping by `tests/ui-primitives.test.tsx::maps every
 * known status to its tone`.
 */
const STATUS_TONES: ReadonlyMap<string, BadgeTone> = new Map<string, BadgeTone>([
  ["pending", "warning"],
  ["open", "warning"],
  ["approved", "info"],
  ["executed", "success"],
  ["completed", "success"],
  ["keep", "success"],
  ["rejected", "danger"],
  ["failed", "danger"],
  ["revoke", "danger"],
  ["rolled_back", "neutral"],
  ["expired", "neutral"],
]);

/**
 * A status the table does not know is `neutral`, never missing: a status the
 * backend adds later still renders, just uncoloured (`::maps an unknown status
 * to neutral`). A Map, not an object, so `constructor` is unknown too.
 */
export function statusTone(status: string): BadgeTone {
  return STATUS_TONES.get(status) ?? "neutral";
}

export function Badge({
  tone = "neutral",
  className,
  ...props
}: React.ComponentProps<"span"> & { tone?: BadgeTone }) {
  return (
    <span
      data-tone={tone}
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        TONE_CLASSES[tone],
        className,
      )}
      {...props}
    />
  );
}

/**
 * A badge coloured by `status` and labelled by the caller's `children`, so the
 * page decides the wording. An unknown status still renders its text
 * (`::renders an unknown status badge with its text, never blank`).
 */
export function StatusBadge({
  status,
  ...props
}: React.ComponentProps<"span"> & { status: string }) {
  return <Badge tone={statusTone(status)} {...props} />;
}
