import { cn } from "@/lib/utils";

type CardElement = "div" | "section" | "li" | "article";

/**
 * A bordered surface. `as` picks the element so a card can be a landmark
 * (`section` with an `aria-label` is a named region) or a list item without
 * a wrapper; every other prop reaches that element
 * (`tests/ui-primitives.test.tsx::renders a card as a named region`).
 */
export function Card({
  as: Element = "div",
  className,
  ...props
}: React.HTMLAttributes<HTMLElement> & { as?: CardElement }) {
  return (
    <Element
      className={cn(
        "rounded-lg border border-border bg-card text-card-foreground shadow-sm",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-1 p-4 pb-2", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return <h2 className={cn("text-base font-semibold leading-tight", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("p-4 pt-2", className)} {...props} />;
}
