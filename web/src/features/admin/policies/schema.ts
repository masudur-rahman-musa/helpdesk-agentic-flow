/**
 * Parsers for the policies board and re-ingest. Every field is required and
 * nothing is coerced: `"false"` read as true would report the wrong version
 * as live.
 */

import { z } from "zod";

/** A `chunk_count` of zero is a value, never an absence. */
export const policyVersionSchema = z.object({
  version: z.string(),
  is_current: z.boolean(),
  ingested_at: z.string(),
  chunk_count: z.number(),
  source_path: z.string(),
  sha256: z.string(),
});

export const policyDocumentSchema = z.object({
  slug: z.string(),
  title: z.string(),
  versions: z.array(policyVersionSchema),
});

/** A tenant with no documents is a valid, empty answer. */
export const policyDocumentsBoardSchema = z.object({
  documents: z.array(policyDocumentSchema),
});

/** An unchanged file answers `created: false` and 0 chunks, as nothing was written. */
export const policyReingestResultSchema = z.object({
  slug: z.string(),
  version: z.string(),
  created: z.boolean(),
  chunk_count: z.number(),
  is_current: z.boolean(),
  sha256: z.string(),
  ingested_at: z.string(),
  summary: z.string(),
});

export type PolicyVersion = z.infer<typeof policyVersionSchema>;
export type PolicyDocument = z.infer<typeof policyDocumentSchema>;
export type PolicyDocumentsBoard = z.infer<typeof policyDocumentsBoardSchema>;
export type PolicyReingestResult = z.infer<typeof policyReingestResultSchema>;
