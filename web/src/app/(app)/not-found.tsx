import { NotFoundView } from "@/components/layout/not-found-view";

/**
 * The not-found screen for an unknown URL under the signed-in group.
 * It renders inside the group layout, so the shell is still there and only the
 * content area says the page is missing.
 */
export default function NotFound() {
  return <NotFoundView />;
}
