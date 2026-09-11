import { describe, expect, it } from "vitest";

import {
  REVIEW_DECISION_LABELS,
  REVIEW_FLAG_LABELS,
  REVIEW_STATUS_LABELS,
  reviewFlagLabel,
  reviewItemDecisionLabel,
  reviewStatusLabel,
} from "@/lib/review-labels";

/**
 * An earlier change §6, `review-labels.test.ts`: the hand-kept mirror of the review
 * family's three Python vocabularies, as literals. Pure — no DOM, no fetch,
 * no React. (No basename collision in the tree, a project rule.)
 *
 * The wire values are `helpdesk.models.access`'s as BUILT, each read off the
 * enum rather than remembered: `ReviewFlag` has four members
 * (`sod_conflict`, `non_standard`, `stale`, `ok`, in that precedence order),
 * `ReviewStatus` two (`open`, `completed` — a review is never `cancelled`) and
 * `ReviewDecision` two (`keep`, `revoke`). Every key asserted here is the
 * enum's VALUE as `model_dump(mode="json")` puts it on the wire, never the
 * Python member name.
 *
 * Decision C keeps these three out of `lib/decision-labels.ts`: that module's
 * `statusLabel` already means REQUEST status, so a second `open`/`completed`
 * status in the same namespace would be a collision the next reader
 * disambiguates by call site. This file therefore asserts its OWN module and
 * touches nothing of 22d's.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23f's Deviations): §6 fixes the exported names and the
 * assertion shape but NOT the label texts, so no case here pins a sentence —
 * the strongest true statement available is "a non-empty label that is not the
 * code itself", plus the map/lookup agreement below. A plan that named the six
 * texts would get a project rule treatment instead; this one did not, and
 * inventing them here would let the implementer invent a third answer.
 */

/** Every `helpdesk.models.access.ReviewFlag` member, as its wire value. */
const FLAGS = ["sod_conflict", "non_standard", "stale", "ok"] as const;

/** Every `ReviewStatus` member. */
const STATUSES = ["open", "completed"] as const;

/** Every `ReviewDecision` member. */
const DECISIONS = ["keep", "revoke"] as const;

type Lookup = (value: string) => string;

const VOCABULARIES = [
  {
    name: "flag",
    lookup: reviewFlagLabel as Lookup,
    map: REVIEW_FLAG_LABELS,
    values: FLAGS as readonly string[],
  },
  {
    name: "status",
    lookup: reviewStatusLabel as Lookup,
    map: REVIEW_STATUS_LABELS,
    values: STATUSES as readonly string[],
  },
  {
    name: "decision",
    lookup: reviewItemDecisionLabel as Lookup,
    map: REVIEW_DECISION_LABELS,
    values: DECISIONS as readonly string[],
  },
] as const;

describe("every member of the three review vocabularies", () => {
  const rows = VOCABULARIES.flatMap((vocabulary) =>
    vocabulary.values.map((value) => ({
      vocabulary: vocabulary.name,
      lookup: vocabulary.lookup,
      value,
    })),
  );

  it.each(rows)("$vocabulary $value reads as a label, not as its code", ({ lookup, value }) => {
    // Eight members over three enums, one assertion each. Catches a map short
    // of a member — a `ReviewFlag` map written from the three a reviewer
    // remembers renders `non_standard` at a reviewer through the fallback,
    // which is silently green everywhere else — and catches a map keyed by the
    // Python MEMBER NAME (`SOD_CONFLICT`, `KEEP`), where every lookup misses
    // and the page shows the wire value. The `not.toBe(value)` half is
    // load-bearing: a lookup implemented as the identity function (the
    // fallback with no map behind it) satisfies "non-empty" for all eight.
    const label = lookup(value);

    expect(label.length).toBeGreaterThan(0);
    expect(label).not.toBe(value);
  });
});

describe("the three maps and the three lookups", () => {
  it.each(VOCABULARIES)("$name's map holds exactly its enum's members", ({ map, values }) => {
    // The mirror rule in the one direction the case above cannot see: a map
    // carrying a member Python does NOT have (`cancelled`, a fifth flag
    // invented while writing the file) is a vocabulary that has silently
    // drifted from `helpdesk.models.access`, and a page rendering it would
    // claim a state the tool can never send. Adding a member on the Python
    // side is then a deliberate edit to the literal list at the top of this
    // file rather than a change nothing notices.
    expect(Object.keys(map).sort()).toEqual([...values].sort());
  });

  it.each(VOCABULARIES)("$name's lookup answers its own map's value", ({ lookup, map, values }) => {
    // Catches the lookup written as an inline `switch` beside an exported map
    // nobody reads: the map is then decoration, the two drift the first time
    // one is edited, and the case above passes for both because each answers
    // SOME non-empty label. This is the only case that fails when they
    // disagree, and it pins no text of its own, so it stays true whatever the
    // six labels end up saying.
    for (const value of values) {
      expect(lookup(value)).toBe(map[value]);
    }
  });
});

describe("an unknown value in any of the three maps", () => {
  it.each(VOCABULARIES)("$name renders the raw wire string", ({ lookup }) => {
    // One behaviour, three lookups, §6's second bullet. Catches the lookup
    // written as a bare index — `REVIEW_FLAG_LABELS[value]` on a member Python
    // added since renders the literal "undefined" in front of a reviewer — and
    // the one written with a vague default, which hides the drift instead of
    // showing a value someone can grep for against the enum
    // (`decision-labels.ts` decision B, the same choice one module over).
    // Parametrised because a fallback added to two of the three is the shape
    // this actually ships as.
    expect(lookup("not_a_member")).toBe("not_a_member");
  });
});
