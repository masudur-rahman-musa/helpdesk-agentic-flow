import { cn } from "@/lib/utils";

type CardElement = "div" | "section" | "li" | "article";

/** `as` lets a card be a named region or a list item without a wrapper. */
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
