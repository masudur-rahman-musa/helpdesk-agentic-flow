/** The policies page's digest format and sentences. */

/** Twelve characters tell versions apart; the full value is in the title. */
export const DIGEST_PREFIX = 12;

/** A prefix, since an ingest log prints a digest from its start. */
export function formatDigest(sha256: string): string {
  return sha256.slice(0, DIGEST_PREFIX);
}

/** Names what failed: the page's other reading is "nothing ingested". */
export const POLICIES_READ_MESSAGE =
  "The policy documents could not be read. Try again in a moment.";

export const POLICIES_EMPTY_TEXT = "No policy documents have been ingested.";

/** The version search answers from. */
export const LIVE_VERSION_TEXT = "Live";

/** A word, never blank: blank reads as a failed load. */
export const NOT_LIVE_VERSION_TEXT = "Not live";

/** A half-finished ingest is what this page must make visible. */
export const NO_LIVE_VERSION_TEXT = "No live version";

/** Zero chunks means search cannot see this version. */
export const NO_CHUNKS_TEXT = "No chunks ingested";
