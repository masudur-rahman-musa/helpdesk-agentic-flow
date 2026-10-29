/**
 * Parsers for the access rules page. Every field is required and nothing is
 * coerced, so a bad answer fails instead of showing a blank page.
 */

import { z } from "zod";

export const policyListRowSchema = z.object({
  slug: z.string(),
  title: z.string(),
  version: z.string(),
});

/** `heading_path` is outermost first; the heading line is not in `text`. */
export const policySectionSchema = z.object({
  heading_path: z.array(z.string()),
  text: z.string(),
});

export const policyTextSchema = z.object({
  slug: z.string(),
  title: z.string(),
  version: z.string(),
  sections: z.array(policySectionSchema),
});

/** `document` is null when no slug was asked for. */
export const policyReadSchema = z.object({
  documents: z.array(policyListRowSchema),
  document: policyTextSchema.nullable(),
});

export type PolicyListRow = z.infer<typeof policyListRowSchema>;
export type PolicySection = z.infer<typeof policySectionSchema>;
export type PolicyText = z.infer<typeof policyTextSchema>;
export type PolicyRead = z.infer<typeof policyReadSchema>;
