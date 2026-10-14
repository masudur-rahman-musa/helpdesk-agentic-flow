import { cn } from "@/lib/utils";

/**
 * The one content width of the app: centred, `max-w-6xl`, with the gutter
 * token as side padding. The shell's header rows and its main content both
 * sit inside one, so their edges line up (`tests/app-shell-layout.test.tsx::
 * renders page content inside the max-w-6xl container`).
 */
export function Container({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("mx-auto w-full max-w-6xl px-gutter", className)} {...props} />;
}
