import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * The one button of the app, in four variants and two sizes (an earlier change
 * decision B: `class-variance-authority`, the shadcn idiom, already a
 * dependency). `primary` is the default and keeps the classes the login
 * button had before it moved here from `field.tsx`. Each variant's colour
 * class is pinned by `tests/ui-primitives.test.tsx::gives each button variant
 * its colour class`.
 *
 * No default `type`: a `<button>` inside a form submits it, which is what the
 * login form relies on, so a caller wanting a plain button says
 * `type="button"` itself (`::adds no default type to a button`).
 */
export const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium",
    "transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
    "disabled:pointer-events-none disabled:opacity-50",
  ],
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-foreground hover:bg-primary/90",
        secondary: "bg-secondary text-secondary-foreground hover:bg-secondary/80",
        ghost: "text-foreground hover:bg-accent hover:text-accent-foreground",
        destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90",
      },
      size: {
        sm: "h-8 px-2.5 text-xs",
        md: "h-9 px-3 py-2 text-sm",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  },
);

export type ButtonProps = React.ComponentProps<"button"> & VariantProps<typeof buttonVariants>;

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
