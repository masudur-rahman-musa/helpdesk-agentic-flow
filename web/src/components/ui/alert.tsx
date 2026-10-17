import { cn } from "@/lib/utils";

/**
 * The boxed `role="alert"` banner every page shows when a read or an action
 * fails. `neutral` is the plain bordered box; `destructive` is the
 * red one chat shows for a failed turn.
 *
 * Pinned by `tests/ui-primitives.test.tsx` and, for who uses it,
 * `tests/shared-ui-adoption.test.tsx`.
 */

const TONES = {
  neutral: "rounded-md border border-border px-3 py-2 text-sm",
  destructive:
    "rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive",
} as const;

export type AlertTone = keyof typeof TONES;

export function Alert({
  tone = "neutral",
  className,
  ...props
}: React.ComponentProps<"p"> & { tone?: AlertTone }) {
  return <p role="alert" className={cn(TONES[tone], className)} {...props} />;
}
