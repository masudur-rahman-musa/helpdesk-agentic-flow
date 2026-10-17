"use client";

import { type ReactNode, useState } from "react";

import { PoliciesReingest } from "@/features/admin/policies/components/policies-reingest";
import { formatSwitchInstant } from "@/lib/date-format";
import {
  formatDigest,
  LIVE_VERSION_TEXT,
  NO_CHUNKS_TEXT,
  NOT_LIVE_VERSION_TEXT,
} from "@/features/admin/policies/format";
import type { PolicyDocument, PolicyVersion } from "@/features/admin/policies/schema";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

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
export function PolicyVersionsTable({
  policy,
  onChanged,
}: {
  policy: PolicyDocument;
  onChanged: () => void;
}) {
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
