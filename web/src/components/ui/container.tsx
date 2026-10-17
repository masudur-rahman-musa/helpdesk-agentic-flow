import { cn } from "@/lib/utils";

/** The one content width, so header and page edges line up. */
export function Container({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("mx-auto w-full max-w-6xl px-gutter", className)} {...props} />;
}
