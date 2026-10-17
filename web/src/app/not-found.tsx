import { NotFoundView } from "@/components/layout/not-found-view";

/**
 * Not-found for a URL no route matches. No shell or nav here: a visitor who is
 * not signed in must not see links to pages they cannot reach.
 */
export default function RootNotFound() {
  return <NotFoundView />;
}
