/**
 * The parts of `/admin/policies` a test can pin without a DOM (an earlier change §2,
 * decision H): how a version's digest renders, and the six strings the board
 * shows.
 *
 * Nothing here talks to the wire. `components/admin/policies-board.tsx` renders
 * through all of them, the way `kill-switch-board.tsx` renders through
 * `lib/killswitch-format.ts` — a pure module with its own test file, so the
 * digest is pinned without rendering a board.
 *
 * The INSTANT is deliberately absent: a version's `ingested_at` renders through
 * `formatSwitchInstant` in `lib/killswitch-format.ts`, imported unchanged
 * (decision F), which leaves one implementation of that slice in `web/` rather
 * than a second copy of a rule about midnight and local time. The cost is a
 * module whose name is now wrong for two callers; the rename is named as debt
 * in row 31's §3, because mixing it into this diff is `/plans` §7 trigger 3.
 *
 * Pinned by `tests/policies-format.test.ts`, one exact assertion per value.
 */

/**
 * How many characters of a `sha256` the digest cell shows (decision E).
 *
 * Twelve hex characters separate two versions of one document past any
 * accident, while all 64 would cost the other five columns their width on every
 * row. The whole value stays one hover away — the component puts it in the
 * cell's `title` — so nothing an admin compares against an ingest log is lost.
 * Pinned at 12 by `tests/policies-format.test.ts::policies-format — the values
 * the board reads::fixes DIGEST_PREFIX at twelve`.
 */
export const DIGEST_PREFIX = 12;

/**
 * A version's `sha256`, as the first `DIGEST_PREFIX` characters (decision E).
 *
 * A digest no longer than the prefix is returned UNCHANGED and never marked:
 * appending a truncation marker to a value that was not truncated would tell an
 * admin the cell is hiding characters that do not exist. Both directions of the
 * inclusive boundary are pinned — 13 is the shortest input that is cut
 * (`tests/policies-format.test.ts::formatDigest — the digests it shortens::
 * renders $name as its first twelve characters`) and 12 the longest returned
 * whole (`::formatDigest — the digests it returns whole::returns $name
 * unchanged`).
 *
 * A prefix and never a suffix: an ingest log prints a digest from its start, so
 * a cell shortened from the other end could not be compared against one at all.
 */
export function formatDigest(sha256: string): string {
  return sha256.slice(0, DIGEST_PREFIX);
}

/**
 * The sentence for a read that did not come back as a corpus (§1's first
 * unhappy path, a project rule's discriminator).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`, which says only that
 * something went wrong and nothing about WHICH thing could not be read — on a
 * page whose alternative reading is "this tenant has ingested no policy at
 * all". Pinned verbatim by `tests/policies-format.test.ts::policies-format —
 * the values the board reads::$name is exactly the sentence the board shows`,
 * which is parametrised over all six of this module's strings.
 */
export const POLICIES_READ_MESSAGE =
  "The policy documents could not be read. Try again in a moment.";

/**
 * What a tenant with no ingested document sees.
 *
 * Rendered only for a read that SUCCEEDED and answered an empty corpus: a
 * failed read throws inside `readWire`, so this sentence and the alert are
 * mutually exclusive by construction (§5's first risk), and both sides are
 * pinned by `tests/policies-board.test.tsx::PoliciesBoard — nothing to show::
 * renders the named empty state with no table and no alert` and the three
 * `PoliciesBoard — reads that fail::$name renders the read message, no table
 * and no empty state` rows.
 */
export const POLICIES_EMPTY_TEXT = "No policy documents have been ingested.";

/** The `Status` cell of the version the retriever answers from (decision C). */
export const LIVE_VERSION_TEXT = "Live";

/**
 * The `Status` cell of every other version (decision C).
 *
 * A word and never a blank: an empty cell reads as a value that failed to load,
 * where this one is a superseded version the corpus still carries. Pinned
 * against both the blank and the swap with `LIVE_VERSION_TEXT` by
 * `tests/policies-board.test.tsx::PoliciesBoard — what a version row shows::
 * renders Live on the current version and Not live on the others`.
 */
export const NOT_LIVE_VERSION_TEXT = "Not live";

/**
 * The line a document carrying no current version shows (decision C).
 *
 * The half-landed ingest is exactly the state this page exists to make visible,
 * and a badge on the live row cannot say anything when there is no live row.
 * Pinned from both sides by `tests/policies-board.test.tsx::PoliciesBoard — a
 * document with no live version::shows the line on a document whose versions
 * are all superseded, and not on one that is live`.
 */
export const NO_LIVE_VERSION_TEXT = "No live version";

/**
 * The marker beside a `chunk_count` of zero (decision D).
 *
 * A zero in a column of numbers reads as "small"; it is the one value on this
 * surface meaning the ingest registered a document and wrote no span, so the
 * version is invisible to the retriever. The number is rendered too — the
 * marker replaces nothing. Pinned with its non-zero neighbour by
 * `tests/policies-board.test.tsx::PoliciesBoard — what a version row shows::
 * renders a chunk_count of zero with its marker and a non-zero count without
 * one`.
 */
export const NO_CHUNKS_TEXT = "No chunks ingested";
