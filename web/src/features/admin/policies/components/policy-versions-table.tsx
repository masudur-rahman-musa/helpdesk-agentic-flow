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

/** `Actions` is last, so the data columns keep their places. */
const COLUMNS = ["Version", "Status", "Ingested", "Chunks", "Source", "Digest", "Actions"] as const;

/**
 * Keyed by version and digest: a changed re-ingest adds a second row with the
 * same version string. Only the newest row of each version gets a control,
 * since the server re-ingests the whole version. The armed flag lives here so a
 * click on another control in the same document can clear it.
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
 * Zero chunks gets a marker, since that version is invisible to search.
 * The digest shows a prefix with the full value on hover.
 */
function VersionRow({ version, control }: { version: PolicyVersion; control: ReactNode }) {
  return (
    <TableRow className="align-top">
      <TableCell className="font-mono text-xs">{version.version}</TableCell>
      {/* A word either way, never a blank cell. */}
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
