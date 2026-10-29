import { PoliciesReader } from "@/features/policies/components/policies-reader";

/**
 * The `/policies` route: the company's access rules, open to every signed-in
 * role. `?doc=<slug>` picks the document; the reader fetches its own data.
 */
export default async function PoliciesPage({
  searchParams,
}: {
  searchParams: Promise<{ doc?: string | string[] }>;
}) {
  const { doc } = await searchParams;
  return <PoliciesReader doc={typeof doc === "string" && doc !== "" ? doc : null} />;
}
