import { cn } from "@/lib/utils";

export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "border-border bg-muted text-muted-foreground",
  info: "border-transparent bg-info text-info-foreground",
  success: "border-transparent bg-success text-success-foreground",
  warning: "border-transparent bg-warning text-warning-foreground",
  danger: "border-transparent bg-danger text-danger-foreground",
};

/** Status to colour. `approved` is info, not success: it is not executed yet. */
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
  ["cancelled", "neutral"],
]);

/** Unknown status is `neutral`. A Map, so `constructor` is unknown too. */
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

export function StatusBadge({
  status,
  ...props
}: React.ComponentProps<"span"> & { status: string }) {
  return <Badge tone={statusTone(status)} {...props} />;
}
