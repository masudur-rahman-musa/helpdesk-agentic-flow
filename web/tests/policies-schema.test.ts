import { describe, expect, it } from "vitest";

import {
  policyDocumentSchema,
  policyDocumentsBoardSchema,
  policyReingestResultSchema,
  policyVersionSchema,
} from "@/lib/policies-schema";

/**
 * An earlier change §6, `policies-schema.test.ts`: the parser 24h's `/admin/policies`
 * page reads, over the one payload the new proxy returns.
 *
 * Surface under test (written before it exists):
 * - `web/lib/policies-schema.ts` exports `policyVersionSchema`,
 *   `policyDocumentSchema` and `policyDocumentsBoardSchema`, plus the inferred
 *   types `PolicyVersion`, `PolicyDocument` and `PolicyDocumentsBoard` — one
 *   file over the ENVELOPE, `rules-schema.ts`' and `killswitch-schema.ts`'
 *   own shape.
 * - the envelope key is `documents`, the document carries the THREE fields of
 *   `PolicyDocumentRow` (`slug`, `title`, `versions`) and a version the SIX of
 *   `PolicyVersionRow` (`version`, `is_current`, `ingested_at`,
 *   `chunk_count`, `source_path`, `sha256`) — all read off
 *   `mcp_server/tools/admin.py`, never invented here.
 * - every field is REQUIRED and nothing is nullable: unlike the rules board,
 *   this tool's model has no optional half at all.
 * - the instants and the digests cross as STRINGS and are never coerced: the
 *   wire is JSON and the tool's `datetime` is already serialised, the way
 *   `audit-schema.ts` carries its own.
 * - nothing here is `.strict()` (`killswitch-schema.ts`' reason: the wire may
 *   grow, and a page that refused a payload carrying one new key would break
 *   on a deploy that changed nothing it reads).
 *
 * No backing service, no route, no page: this is the parser alone.
 */

/** One version of one document, every field present, obviously fake. */
const CURRENT_VERSION: Record<string, unknown> = {
  version: "v9",
  is_current: true,
  ingested_at: "2026-04-01T09:00:00Z",
  chunk_count: 3,
  source_path: "policies/fake-tenant/v9/fake-access-policy.md",
  sha256: "a9".repeat(32),
};

/**
 * A version that registered a document and wrote no span: `chunk_count` 0 and
 * `is_current` false. The row §1 says may never be dropped.
 */
const UNCHUNKED_VERSION: Record<string, unknown> = {
  ...CURRENT_VERSION,
  version: "v10",
  is_current: false,
  ingested_at: "2026-03-01T09:00:00Z",
  chunk_count: 0,
  sha256: "b0".repeat(32),
};

/** The six field names of the version, written out rather than derived. */
const VERSION_FIELDS = [
  "version",
  "is_current",
  "ingested_at",
  "chunk_count",
  "source_path",
  "sha256",
] as const;

/** The three field names of the document, written out for the same reason. */
const DOCUMENT_FIELDS = ["slug", "title", "versions"] as const;

/** A document with two versions, one current and one half-landed. */
const ACCESS_DOCUMENT: Record<string, unknown> = {
  slug: "fake-access-policy",
  title: "Fake Access Standard",
  versions: [CURRENT_VERSION, UNCHUNKED_VERSION],
};

/** A document with NO current version — every flag false (§1's criterion 6). */
const ORPHANED_DOCUMENT: Record<string, unknown> = {
  slug: "fake-leave-policy",
  title: "Fake Leave Standard",
  versions: [{ ...UNCHUNKED_VERSION, version: "v1", chunk_count: 2 }],
};

function without(row: Record<string, unknown>, field: string): Record<string, unknown> {
  const copy = { ...row };
  delete copy[field];
  return copy;
}

function documentWithVersion(version: Record<string, unknown>): Record<string, unknown> {
  return { ...ACCESS_DOCUMENT, versions: [version] };
}

function board(...documents: Record<string, unknown>[]): unknown {
  return { documents };
}

describe("policyDocumentsBoardSchema", () => {
  it("accepts a two-document board: one current version, one half-landed, one orphan", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all nine of them pass while the page
    // rendered nothing at all. The three version states are the ones §1 says
    // must all appear — a live version, a version with zero chunks, and a
    // document whose every flag is false — so a schema that required
    // `is_current` to be true somewhere, or `chunk_count` to be positive,
    // refuses exactly the rows this page exists to show. The counts ride along
    // because §1 fixes the document at three fields and the version at six
    //.
    const parsed = policyDocumentsBoardSchema.safeParse(board(ACCESS_DOCUMENT, ORPHANED_DOCUMENT));

    expect(parsed.success).toBe(true);
    expect(DOCUMENT_FIELDS).toHaveLength(3);
    expect(VERSION_FIELDS).toHaveLength(6);
    if (!parsed.success) {
      throw new Error("the complete board must parse");
    }
    expect(parsed.data.documents.map((document) => document.slug)).toEqual([
      "fake-access-policy",
      "fake-leave-policy",
    ]);
    expect(parsed.data.documents[0]?.versions.map((version) => version.version)).toEqual([
      "v9",
      "v10",
    ]);
    expect(parsed.data.documents[0]?.versions[1]?.chunk_count).toBe(0);
    expect(parsed.data.documents[1]?.versions[0]?.is_current).toBe(false);
  });

  it.each(DOCUMENT_FIELDS)("rejects a board document with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether). Each of the three is a different silent lie: an undefined
    // `slug` leaves a card nothing can be done about and no key to render by,
    // an undefined `title` leaves the page showing a slug where a document
    // name belongs, and an undefined `versions` turns the one question this
    // page asks — which versions exist and which is live — into a blank.
    expect(
      policyDocumentsBoardSchema.safeParse(board(without(ACCESS_DOCUMENT, field))).success,
    ).toBe(false);
  });

  it.each(VERSION_FIELDS)("rejects a version row with no %s", (field) => {
    // Catches an `.optional()` field inside the version, asserted through the
    // ENVELOPE as well as through the version schema, so a board written over
    // a looser version than `policyVersionSchema` is caught too. A version
    // missing `chunk_count` hides the half-landed ingest §1 carries it for, a
    // missing `is_current` loses which copy is live, a missing `sha256` leaves
    // two versions indistinguishable (decision G), and a missing `ingested_at`
    // removes the column the whole order is built on (decision C).
    expect(policyVersionSchema.safeParse(without(CURRENT_VERSION, field)).success).toBe(false);
    expect(
      policyDocumentsBoardSchema.safeParse(
        board(documentWithVersion(without(CURRENT_VERSION, field))),
      ).success,
    ).toBe(false);
  });

  it("accepts an empty board and rejects a bare array or another envelope key", () => {
    // §1's "a tenant with no corpus is a 200 carrying an empty list". Catches
    // a schema written over the DOCUMENT rather than over the envelope, which
    // would leave the page parsing per row and never seeing the envelope the
    // tool answers, and one that guessed a different key — `policies`, or the
    // `rules` of the file this one is modelled on.
    expect(policyDocumentsBoardSchema.safeParse({ documents: [] }).success).toBe(true);
    expect(policyDocumentsBoardSchema.safeParse([ACCESS_DOCUMENT]).success).toBe(false);
    expect(policyDocumentsBoardSchema.safeParse({ policies: [ACCESS_DOCUMENT] }).success).toBe(
      false,
    );
  });

  it("accepts a document, a version and an envelope carrying an unknown extra key", () => {
    // Catches a `.strict()` schema at any of the three levels. The wire may
    // grow — row 31's re-ingest control is expected to add to this very
    // payload — and a page that refused a payload carrying one new key would
    // break on a deploy that changed nothing it reads.
    expect(
      policyDocumentsBoardSchema.safeParse(board({ ...ACCESS_DOCUMENT, archived: false })).success,
    ).toBe(true);
    expect(
      policyDocumentsBoardSchema.safeParse(
        board(documentWithVersion({ ...CURRENT_VERSION, ingested_by: "fake" })),
      ).success,
    ).toBe(true);
    expect(
      policyDocumentsBoardSchema.safeParse({
        ...(board(ACCESS_DOCUMENT) as object),
        read_at: "later",
      }).success,
    ).toBe(true);
  });
});

describe("policyVersionSchema", () => {
  it("rejects a numeric instant, a numeric digest and a numeric version", () => {
    // §1's "ids/instants as `z.string()`, never coerced". Catches a schema
    // written with `z.number()` or with `z.coerce.string()` on any of the
    // three: the tool serialises its `datetime` and its digest as strings, so
    // a number is a payload nothing on the server produces, and a coercing
    // parser would silently accept it and hand the page a value no sort and no
    // lookup can use. Only the field's type can decide these rows — every
    // field is present.
    expect(policyVersionSchema.safeParse({ ...CURRENT_VERSION, ingested_at: 1 }).success).toBe(
      false,
    );
    expect(policyVersionSchema.safeParse({ ...CURRENT_VERSION, sha256: 1 }).success).toBe(false);
    expect(policyVersionSchema.safeParse({ ...CURRENT_VERSION, version: 9 }).success).toBe(false);
  });

  it("rejects a stringified chunk_count and a stringified is_current", () => {
    // The other half of "never coerced", on the two fields that are NOT
    // strings. Catches `z.coerce.number()` and `z.coerce.boolean()`, which
    // accept `"0"` and `"false"` — and `z.coerce.boolean()` turns the string
    // `"false"` into `true`, so a half-landed version would render as the live
    // one. A count arriving as text would also sort as text on the page.
    expect(policyVersionSchema.safeParse({ ...CURRENT_VERSION, chunk_count: "3" }).success).toBe(
      false,
    );
    expect(policyVersionSchema.safeParse({ ...CURRENT_VERSION, is_current: "false" }).success).toBe(
      false,
    );
  });

  it("accepts a zero chunk count and rejects an undefined one", () => {
    // Catches a schema written as `z.number().positive()` or, worse, one whose
    // `chunk_count` is optional: zero is the value that says an ingest
    // registered a document and wrote no span (decision B), which is the whole
    // reason the field is carried, and both of those turn it into nothing at
    // all. `undefined` is asserted separately because an `.optional()` field
    // passes every "missing key" rejection above by not being there.
    expect(policyVersionSchema.safeParse(UNCHUNKED_VERSION).success).toBe(true);
    expect(
      policyVersionSchema.safeParse({ ...CURRENT_VERSION, chunk_count: undefined }).success,
    ).toBe(false);
  });
});

describe("policyDocumentSchema", () => {
  it("accepts one document on its own and rejects a versions field that is not an array", () => {
    // The document schema is exported so 24h can narrow a single card without
    // re-parsing the board. Catches a file that exported only the envelope —
    // the page would then have no type for a document at all — and one whose
    // `versions` is a single object or a string rather than a list, which
    // decision A rejects: the page's question is "which versions of each", so
    // a document that carried one version would render a corpus as if every
    // document had exactly one.
    expect(policyDocumentSchema.safeParse(ACCESS_DOCUMENT).success).toBe(true);
    expect(policyDocumentSchema.safeParse(ORPHANED_DOCUMENT).success).toBe(true);
    expect(
      policyDocumentSchema.safeParse({ ...ACCESS_DOCUMENT, versions: CURRENT_VERSION }).success,
    ).toBe(false);
    expect(policyDocumentSchema.safeParse({ ...ACCESS_DOCUMENT, versions: null }).success).toBe(
      false,
    );
  });
});

/**
 * An earlier change §6, extending this file: the RESULT envelope the re-ingest POST
 * returns, one rejection case per required field.
 *
 * Surface under test (written before it exists):
 * - `web/lib/policies-schema.ts` also exports `policyReingestResultSchema` and
 *   the inferred type `PolicyReingestResult`.
 * - the envelope is the tool's `ReingestPolicyDocumentOutput` verbatim: eight
 *   REQUIRED fields, `slug`, `version`, `created`, `chunk_count`,
 *   `is_current`, `sha256`, `ingested_at` and `summary` — read off
 *   `mcp_server/tools/admin.py`, never invented here.
 * - nothing is `.strict()`, for the same reason nothing above it is, and
 *   nothing is coerced: the instant and the digest cross as strings, the count
 *   as a number and the two flags as booleans.
 */

/** One re-ingest that landed: every field present, obviously fake. */
const REINGEST_RESULT: Record<string, unknown> = {
  slug: "fake-access-policy",
  version: "v9",
  created: true,
  chunk_count: 4,
  is_current: true,
  sha256: "e5".repeat(32),
  ingested_at: "2026-05-04T09:30:00Z",
  summary: "Re-ingested fake-access-policy v9: 4 chunks, now current.",
};

/** The eight field names, written out rather than derived from the object. */
const REINGEST_FIELDS = [
  "slug",
  "version",
  "created",
  "chunk_count",
  "is_current",
  "sha256",
  "ingested_at",
  "summary",
] as const;

describe("policyReingestResultSchema", () => {
  it("accepts a landed re-ingest and an unchanged one", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all eight of them pass while the control
    // reported failure on a re-ingest that worked. The UNCHANGED shape is the
    // second half — `created: false` with `chunk_count: 0` is what the tool
    // answers for a source whose sha256 already matched, and a schema written
    // `z.number().positive()` or `z.literal(true)` refuses exactly the answer
    // the admin gets when nothing needed doing.
    expect(policyReingestResultSchema.safeParse(REINGEST_RESULT).success).toBe(true);
    expect(REINGEST_FIELDS).toHaveLength(8);
    expect(
      policyReingestResultSchema.safeParse({
        ...REINGEST_RESULT,
        created: false,
        chunk_count: 0,
        is_current: false,
      }).success,
    ).toBe(true);
  });

  it.each(REINGEST_FIELDS)("rejects a re-ingest result with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether). Each is a different silent lie on the one surface that says
    // what the mutation did: an undefined `created` or `chunk_count` leaves the
    // page unable to tell a re-ingest that wrote four chunks from one that
    // wrote nothing, an undefined `is_current` loses whether the version just
    // made live is the live one (decision E's whole visible effect), an
    // undefined `sha256` removes the only evidence the file on disk differed,
    // an undefined `ingested_at` leaves no time to show, and an undefined
    // `slug` or `version` leaves the answer unattached to the row it belongs
    // to. A missing `summary` leaves the control with nothing to say, which is
    // the field the design requires beside the values rather than instead of
    // them.
    expect(policyReingestResultSchema.safeParse(without(REINGEST_RESULT, field)).success).toBe(
      false,
    );
  });

  it("rejects coerced values and accepts an unknown extra key", () => {
    // The other half of "never coerced", and the `.strict()` check this file
    // already makes for the board. Catches `z.coerce.boolean()` on `created`,
    // which turns the string "false" into `true` — a re-ingest that did
    // nothing reported as one that landed — and `z.coerce.number()` on
    // `chunk_count`, which accepts "0" and hands the page a value no
    // comparison can use. The extra key is the deploy-safety row: the wire may
    // grow, and a page that refused a payload carrying one new key would break
    // on a deploy that changed nothing it reads.
    expect(
      policyReingestResultSchema.safeParse({ ...REINGEST_RESULT, created: "false" }).success,
    ).toBe(false);
    expect(
      policyReingestResultSchema.safeParse({ ...REINGEST_RESULT, chunk_count: "4" }).success,
    ).toBe(false);
    expect(
      policyReingestResultSchema.safeParse({ ...REINGEST_RESULT, ingested_at: 1 }).success,
    ).toBe(false);
    expect(
      policyReingestResultSchema.safeParse({ ...REINGEST_RESULT, requested_by: "fake" }).success,
    ).toBe(true);
  });
});
