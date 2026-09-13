/**
 * The parser for what `/api/admin/policies` returns.
 *
 * Written over the ENVELOPE rather than over a bare row (24eb decision D), the
 * way `rules-schema.ts`, `killswitch-schema.ts` and `audit-schema.ts` carry
 * theirs: an earlier change runs ONE `safeParse` on the proxy's body and gets either the
 * whole corpus or one refusal, never a half-parsed list it has to re-check per
 * row.
 *
 * Every field is REQUIRED and nothing is nullable: the tool answers a value for
 * all nine, and an `.optional()` anywhere would reach the page as `undefined` —
 * a version with no `chunk_count` rendering as a blank rather than as the
 * half-landed ingest this page exists to catch.
 * `tests/policies-schema.test.ts` rejects a document missing any one of its
 * three and a version missing any one of its six, one case per field.
 *
 * The instants, the digests and the version strings cross as STRINGS and are
 * never coerced: the wire is JSON and the tool's values are already serialised,
 * so a `z.coerce.*` would accept a number and, worse on `is_current`, read the
 * string `"false"` as `true` — a half-landed version reported as the live one.
 *
 * Nothing here is `.strict()`. The wire may grow, and a page that refused a
 * payload carrying one new key would break on a deploy that changed nothing it
 * reads.
 */

import { z } from "zod";

/**
 * One ingested version: `PolicyVersionRow`'s six fields.
 *
 * `chunk_count` is the only field that separates a version which ingested from
 * one that registered a document and wrote no span (decision B), and `sha256`
 * the only evidence that two versions differ at all (decision G). Zero is a
 * value here, never an absence.
 */
export const policyVersionSchema = z.object({
  version: z.string(),
  is_current: z.boolean(),
  ingested_at: z.string(),
  chunk_count: z.number(),
  source_path: z.string(),
  sha256: z.string(),
});

/**
 * One document on the board: `PolicyDocumentRow`'s three fields.
 *
 * Grouped by slug rather than flat (decision A), so 24h renders the versions of
 * a document without regrouping a list in the browser on every paint. `title`
 * is the current version's where one exists and the newest-ingested version's
 * otherwise, which the tool decides; this side never re-derives it.
 */
export const policyDocumentSchema = z.object({
  slug: z.string(),
  title: z.string(),
  versions: z.array(policyVersionSchema),
});

/**
 * The whole corpus: every document the tenant has, in the tool's own order
 * (`slug` ascending, each document's versions `ingested_at` descending then
 * `version` descending — 24g decision C).
 *
 * An empty `documents` is a valid payload — a tenant with no corpus is a 200,
 * never a 404 — and a bare array is not: the envelope is the unit the page
 * parses.
 */
export const policyDocumentsBoardSchema = z.object({
  documents: z.array(policyDocumentSchema),
});

export type PolicyVersion = z.infer<typeof policyVersionSchema>;
export type PolicyDocument = z.infer<typeof policyDocumentSchema>;
export type PolicyDocumentsBoard = z.infer<typeof policyDocumentsBoardSchema>;
