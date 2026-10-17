"use client";

import { PolicyVersionsTable } from "@/features/admin/policies/components/policy-versions-table";
import { failureMessage } from "@/lib/failure-messages";
import {
  NO_LIVE_VERSION_TEXT,
  POLICIES_EMPTY_TEXT,
  POLICIES_READ_MESSAGE,
} from "@/features/admin/policies/format";
import { usePoliciesBoard, useRefreshPoliciesBoard } from "@/features/admin/policies/api";
import type { PolicyDocument } from "@/features/admin/policies/schema";
import { WireRefusedError } from "@/lib/wire-read";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";

/**
 * The policy corpus board, from `GET /api/admin/policies`.
 *
 * A client component doing its own read through TanStack Query (§2, decision
 * A), `RulesBoard`'s shape exactly — the read itself `readWire` from
 * `lib/wire-read.ts`, whose twelfth caller this is. The bearer stays an
 * httpOnly cookie the proxy attaches, and an expired session arrives as
 * `response.redirected` rather than as a status, because the browser has
 * already followed the proxy's redirect by the time this code runs (22b
 * decision E). Pinned by `tests/policies-board.test.tsx::PoliciesBoard — an
 * expired session::follows the proxy's redirect to /login instead of rendering
 * a board`.
 *
 * The read is fired on paint and takes NO argument of any kind (decision A):
 * which corpus this caller sees is the token's tenant, decided behind the MCP
 * boundary, so a page naming one would be a project rule written into the browser.
 * Pinned by `::PoliciesBoard — the read it makes::reads the proxy once on
 * paint, as a GET with no query and no body`.
 *
 * Everything here is a READ except ONE control: an earlier change appended an `Actions`
 * column carrying a re-ingest per distinct version, the last cell of the design's
 * `/admin/policies` row. a project rule holds without an exception — the mutation
 * is a typed MCP tool behind RBAC and the kill switch, and nothing about it is
 * decided here; `components/admin/policies-reingest.tsx` holds the click and
 * `tests/policies-reingest.test.tsx` pins what it does.
 *
 * Nothing is decided here either. The payload arrives GROUPED by slug (24g
 * decision A) in the tool's own total order — `slug` ascending, each document's
 * versions `ingested_at` descending then `version` descending (24g decision C)
 * — and this component maps it and nothing else: no regroup, no sort, no
 * filter, so a document ingested by a later plan cannot land somewhere an admin
 * does not look. Pinned over two payload orders by `::PoliciesBoard — the shape
 * of the board::renders a payload in $name order exactly as it arrived` and,
 * per document, by `::renders a document's versions in the payload's own order`
 * over a fixture whose version strings sort differently as strings.
 *
 * A failed read THROWS inside `readWire`, so the alert and the empty state are
 * mutually exclusive by construction: `No policy documents have been ingested.`
 * over a broken proxy is the one sentence on this page an admin acts on by
 * concluding the ingest never ran (§5's first risk). Pinned from both sides by
 * `::PoliciesBoard — nothing to show::renders the named empty state with no
 * table and no alert` and the three `::PoliciesBoard — reads that fail::$name
 * renders the read message, no table and no empty state` rows.
 *
 * Accessible names: the board is the region `Policies`, each document a region
 * named by its `slug`, each versions table named `Versions of <slug>`. All are
 * `aria-label`s, because none of those roles takes its name from its contents.
 */

export function PoliciesBoard() {
  const board = usePoliciesBoard();

  /**
   * What a successful re-ingest does, and the only thing the control is told
   * about the cache (24db decision G): `policies-reingest.tsx` takes a callback
   * and imports nothing from here, so the two modules cannot form a cycle.
   * Pinned by `::PoliciesBoard — the re-ingest control it carries (an earlier change
   * §6)::passes the board's invalidation down rather than exposing its query
   * key`.
   */
  const onChanged = useRefreshPoliciesBoard();

  const refusal = board.isError ? errorMessage(board.error) : null;
  const documents = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Policies" className="flex max-w-5xl flex-col gap-6">
      <PageHeader title="Policy documents" />

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {documents !== null && documents.length === 0 ? (
        <EmptyState title={POLICIES_EMPTY_TEXT} />
      ) : null}

      {documents !== null
        ? documents.map((entry) => (
            <DocumentSection key={entry.slug} policy={entry} onChanged={onChanged} />
          ))
        : null}
    </section>
  );
}

/**
 * One ingested document: its title, whether anything of it is live, and the
 * versions the corpus holds.
 *
 * A region per document rather than one flat table repeating the document on
 * every version row (decision A): the payload is already grouped by slug so the
 * browser does not regroup a list on every paint, and "which documents, and
 * which version of each is live" is the grouped shape written out.
 *
 * A document carrying an EMPTY `versions` renders its heading and no table, and
 * no extra sentence of any kind (decision G) — `No live version` included,
 * which §1's fifth criterion would otherwise ask of it. Every `policy_documents`
 * row is a version, so 24g's tool cannot answer this and a sentence written for
 * it is a string nobody can read to check; the shape is still decided here
 * rather than left to a runtime crash. Pinned by `::PoliciesBoard — nothing to
 * show::renders a document with no versions as its region and heading alone`.
 */
function DocumentSection({ policy, onChanged }: { policy: PolicyDocument; onChanged: () => void }) {
  const hasVersions = policy.versions.length > 0;
  const hasLive = policy.versions.some((entry) => entry.is_current);

  return (
    <section aria-label={policy.slug} className="flex flex-col gap-2">
      <h2 className="text-base font-medium">{policy.title}</h2>

      {hasVersions && !hasLive ? (
        <p className="text-sm text-muted-foreground">{NO_LIVE_VERSION_TEXT}</p>
      ) : null}

      {hasVersions ? <PolicyVersionsTable policy={policy} onChanged={onChanged} /> : null}
    </section>
  );
}

/**
 * The sentence for whatever went wrong, one alert either way.
 *
 * A `WireRefusedError` carries one of the design's eight codes and gets that
 * code's shared sentence; everything else — a payload the parser refused, a
 * body that is not JSON, or a code outside that vocabulary such as the proxy's
 * own `UPSTREAM_UNAVAILABLE` — is a failed READ and gets
 * `POLICIES_READ_MESSAGE` (a project rule, §1's discriminator). No override
 * on this surface: the two codes reachable here are `FORBIDDEN` and
 * `TOOL_DISABLED`, and both shared sentences are true of this read. Pinned per
 * code by `::PoliciesBoard — a refused read::$name renders its own sentence in
 * an alert, with no table`.
 */
function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return POLICIES_READ_MESSAGE;
}
