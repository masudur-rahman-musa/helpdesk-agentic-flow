import { GrantList } from "@/features/access/components/grant-list";

/**
 * The `/access` route. A thin server wrapper; the list fetches its own data.
 * The middleware already checked the session cookie; the list read finds an
 * expired token.
 */
export default function AccessPage() {
  return <GrantList />;
}
