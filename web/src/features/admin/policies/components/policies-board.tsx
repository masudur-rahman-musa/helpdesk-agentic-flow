"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { PoliciesReingest } from "@/features/admin/policies/components/policies-reingest";
import { failureMessage } from "@/features/chat/errors";
import { formatSwitchInstant } from "@/features/admin/killswitch/format";
import {
  formatDigest,
  LIVE_VERSION_TEXT,
  NO_CHUNKS_TEXT,
  NO_LIVE_VERSION_TEXT,
  NOT_LIVE_VERSION_TEXT,
  POLICIES_EMPTY_TEXT,
  POLICIES_READ_MESSAGE,
} from "@/features/admin/policies/format";
import {
  type PolicyDocument,
  policyDocumentsBoardSchema,
  type PolicyVersion,
} from "@/features/admin/policies/schema";
import { readWire, WireRefusedError } from "@/lib/wire-read";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

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

/**
 * The seven columns, in the order every cell assertion in
 * `tests/policies-board.test.tsx` reads them by.
 *
 * Six read cells rather than a live marker folded into the version cell (24h
 * decision B): a merged cell could not say whether a version is live without
 * re-reading the version string, and reading by position is how every other
 * admin board in the tree is asserted. `Actions` is APPENDED (31b §1), so those
 * six keep positions 0 to 5 and every positional assertion still reads the
 * column it was written for. Pinned as an exact sequence by `::PoliciesBoard —
 * the shape of the board::renders the seven column headers in order`.
 */
const COLUMNS = ["Version", "Status", "Ingested", "Chunks", "Source", "Digest", "Actions"] as const;

/** The query this board reads. */
const BOARD_KEY = ["admin", "policies", "board"] as const;

/**
 * What the board answered, or `null` while the browser navigates away.
 *
 * The null is the redirect and nothing else: rendering the empty state for an
 * expired session would tell somebody who is not even signed in that the tenant
 * has ingested no policy at all, and rendering the alert would blame a read
 * that was fine (§5's second risk, a project rule's fourth half).
 */
type BoardAnswer = PolicyDocument[] | null;

export function PoliciesBoard() {
  const queryClient = useQueryClient();
  const board = useQuery({ queryKey: BOARD_KEY, queryFn: fetchBoard });

  /**
   * What a successful re-ingest does, and the only thing the control is told
   * about the cache (24db decision G): the key is the board's own, so
   * `policies-reingest.tsx` imports nothing from here and the two modules
   * cannot form a cycle.
   *
   * Invalidation and never an optimistic rewrite — the tool decides what the
   * rows become, including which version is live, and a row that changed on
   * click would show an ingest that did not land. Pinned by
   * `::PoliciesBoard — the re-ingest control it carries::passes
   * the board's invalidation down rather than exposing its query key`.
   */
  const onChanged = (): void => {
    void queryClient.invalidateQueries({ queryKey: BOARD_KEY });
  };

  const refusal = board.isError ? errorMessage(board.error) : null;
  const documents = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Policies" className="flex max-w-5xl flex-col gap-6">
      <PageHeader title="Policy documents" />

      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {refusal}
        </p>
      ) : null}

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

      {hasVersions ? <VersionsTable policy={policy} onChanged={onChanged} /> : null}
    </section>
  );
}

/**
 * One line per version of one document, in the payload's own order.
 *
 * Keyed by `${version}:${sha256}` (31b decision F). The bare `version` is NOT
 * unique within a document: `register_document_version` matches on the DIGEST,
 * so a changed re-ingest writes a second row under the same version string and
 * 24g's read answers both. Two children under one key is a React error and,
 * worse, carries one row's component state — the armed flag this table hands
 * down — onto the other. An index would re-key every row whenever a later
 * ingest adds one. Pinned by `::renders both rows of a repeated version
 * string`.
 *
 * WHICH rows carry a re-ingest is decided here (31b decision A): the FIRST
 * occurrence of each distinct version string in the payload's own order, which
 * is the newest row of that version because 24g decision C orders versions
 * `ingested_at` descending. The tool re-ingests a version DIRECTORY, so two
 * rows sharing a version string are one re-ingestable thing and a second
 * control would be a duplicate accessible name doing an identical act. Nothing
 * is sorted, filtered or regrouped to find them. Pinned by `::renders one
 * re-ingest control per distinct version of a document`.
 *
 * The ARMING lives here rather than in the control (§1, 31b Deviations): a
 * click on any other control in the same document must clear it, and a flag
 * held per control cannot see its siblings. One armed version per document, so
 * clearing it clears whichever control set it.
 */
function VersionsTable({ policy, onChanged }: { policy: PolicyDocument; onChanged: () => void }) {
  const [armedVersion, setArmedVersion] = useState<string | null>(null);
  const acting = new Set(firstOccurrences(policy.versions));

  return (
    <Table aria-label={`Versions of ${policy.slug}`}>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((column) => (
            <TableHead key={column} scope="col">
              {column}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {policy.versions.map((entry, index) => (
          <VersionRow
            key={`${entry.version}:${entry.sha256}`}
            version={entry}
            control={
              acting.has(index) ? (
                <PoliciesReingest
                  slug={policy.slug}
                  version={entry.version}
                  isCurrent={entry.is_current}
                  armed={armedVersion === entry.version}
                  onArmedChange={(armed) => setArmedVersion(armed ? entry.version : null)}
                  onChanged={onChanged}
                />
              ) : null
            }
          />
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * The index of each version string's first row, in the payload's own order.
 *
 * A first-occurrence scan and not a group-by: nothing is reordered, so the rows
 * an admin reads stay in the order the tool fixed and the control lands on the
 * newest row of its version.
 */
function firstOccurrences(versions: PolicyVersion[]): number[] {
  const seen = new Set<string>();
  const indexes: number[] = [];
  versions.forEach((entry, index) => {
    if (seen.has(entry.version)) {
      return;
    }
    seen.add(entry.version);
    indexes.push(index);
  });
  return indexes;
}

/**
 * One version, seven cells.
 *
 * `Status` is a word either way and never a blank cell (decision C).
 * `Ingested` renders through `formatSwitchInstant` (decision F), so the date
 * and the minute are read in UTC rather than in whatever zone the browser sits
 * in. `Chunks` carries the exact count AND, at zero, the marker that says the
 * ingest registered a document and wrote no span (decision D) — the number
 * stays, because the marker replaces nothing. `Source` is `source_path`
 * verbatim, which 24g decision G made corpus-relative, so nothing about the
 * ingest host reaches the browser through this cell. `Digest` shows
 * `DIGEST_PREFIX` characters with the whole 64 in the cell's `title` (decision
 * E), which is where `::PoliciesBoard — what a version row shows::renders the
 * digest as twelve characters carrying the whole sha256 as its title` looks for
 * it. `Actions` carries the re-ingest on the rows `firstOccurrences` picked and
 * an empty cell on the rest, so every row still has seven cells and a
 * positional read of the first six is unaffected.
 */
function VersionRow({ version, control }: { version: PolicyVersion; control: ReactNode }) {
  return (
    <TableRow className="align-top">
      <TableCell className="font-mono text-xs">{version.version}</TableCell>
      {/* The live word is a hand-toned badge: `Live` reads success
          and `Not live` reads neutral. */}
      <TableCell>
        <Badge tone={version.is_current ? "success" : "neutral"}>
          {version.is_current ? LIVE_VERSION_TEXT : NOT_LIVE_VERSION_TEXT}
        </Badge>
      </TableCell>
      <TableCell>{formatSwitchInstant(version.ingested_at)}</TableCell>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span>{version.chunk_count}</span>
          {version.chunk_count === 0 ? (
            <span className="text-xs text-muted-foreground">{NO_CHUNKS_TEXT}</span>
          ) : null}
        </div>
      </TableCell>
      <TableCell className="font-mono text-xs">{version.source_path}</TableCell>
      <TableCell className="font-mono text-xs" title={version.sha256}>
        {formatDigest(version.sha256)}
      </TableCell>
      <TableCell>{control}</TableCell>
    </TableRow>
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

/**
 * Every document the tenant has ingested, in the tool's own order, or `null`
 * while the browser follows the proxy's redirect to `/login`.
 */
async function fetchBoard(): Promise<BoardAnswer> {
  const parsed = await readWire("/api/admin/policies", policyDocumentsBoardSchema);
  return parsed === null ? null : parsed.documents;
}
