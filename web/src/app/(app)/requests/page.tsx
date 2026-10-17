import { RequestList } from "@/features/requests/components/request-list";

/** The `/requests` route. A thin server wrapper; the list fetches its own data. */
export default function RequestsPage() {
  return <RequestList />;
}
