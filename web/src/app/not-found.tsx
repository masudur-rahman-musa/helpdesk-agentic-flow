import { NotFoundView } from "@/components/layout/not-found-view";

/**
 * The not-found screen for any URL that matches no route at all. It
 * sits above the signed-in group, so there is no shell and no nav here: a
 * visitor who is not signed in must not see links to pages they cannot reach.
 */
export default function RootNotFound() {
  return <NotFoundView />;
}
