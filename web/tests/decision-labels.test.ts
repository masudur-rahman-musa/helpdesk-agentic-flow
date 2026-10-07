import { describe, expect, it } from "vitest";

import {
  decisionLabel,
  principalLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/lib/decision-labels";

/**
 * An earlier change §6, `decision-labels.test.ts`: the hand-kept mirror of five Python
 * vocabularies, as literals. Pure — no DOM, no fetch, no React.
 *
 * The wire values are `services/agent-api/agent_api/decision_card.py`'s as
 * BUILT, not as remembered: `DecisionCard` dumps `decision`
 * (`helpdesk.policy.types.Decision`), `risk_class` (`RiskClass`), `reasons`
 * (`ReasonCode` values, plus `SOD_CONFLICT:{rule_id}` from
 * `sod_conflict_reason`), `approvers[].slot` (`ApproverSlot`) and `status`
 * (`helpdesk.models.access.RequestStatus`), each `mode="json"`, so every key
 * asserted here is the enum's VALUE and never its Python member name.
 *
 * Shapes this file fixes that the plan left open (a project rule/39; these go
 * in the plan's Deviations). §3 names the five maps but not the lookups §6 says
 * are "parametrised over", so:
 * - `web/lib/decision-labels.ts` exports one lookup per vocabulary —
 *   `reasonSentence`, `decisionLabel`, `riskLabel`, `slotLabel`, `statusLabel`
 *   — each `(value: string) => string`, total by construction: a value the map
 *   does not know answers the value itself (decision B).
 * - the lookups, not the maps, are what is asserted. A map exported beside them
 *   is pinned through the lookup that reads it; asserting the map as well would
 *   pin the same fact twice and let a lookup that ignores its map stay green.
 *
 * §5 names the standing risk this file answers: a member added on the Python
 * side renders as its raw code with nothing red, so the ten reason codes and
 * every member of the other four enums are LISTED here as literals. Adding one
 * is then a deliberate edit to this list.
 */

/** Every `helpdesk.policy.types.ReasonCode` member, as its wire value. */
const REASON_CODES = [
  "IN_TEMPLATE",
  "NOT_IN_TEMPLATE",
  "SENSITIVITY_HIGH",
  "PRIVILEGE_ADMIN",
  "CRITICAL_SYSTEM",
  "NEW_JOINER",
  "SOD_CONFLICT",
  "EXPIRY_CAPPED",
  "SELF_ADMIN_ESCALATED",
  "TARGET_LEAVER",
] as const;

const RULE_ID = "3f2b1c4d-0000-4000-8000-00000000abcd";

type Lookup = (value: string) => string;

describe("reasonSentence", () => {
  it.each(REASON_CODES)("%s reads as a sentence, not as its code", (code) => {
    // Catches a map that is short of a member — the design has ten and a map
    // written from the four or five a reviewer remembers renders the missing
    // ones as `EXPIRY_CAPPED`, which explains nothing to the person who asked
    // and is the exact failure decision B accepts only for a code the Python
    // side has not shipped yet. The `not.toBe(code)` half is load-bearing: a
    // lookup implemented as the identity function (the fallback with no map
    // behind it) satisfies "non-empty" for all ten.
    const sentence = reasonSentence(code);
    expect(sentence.length).toBeGreaterThan(0);
    expect(sentence).not.toBe(code);
  });

  it("a SOD_CONFLICT carrying a rule id reads as the bare code does", () => {
    // `sod_conflict_reason` is the only reason that carries a value, so this is
    // the one code whose wire string is not a bare enum value. Catches a lookup
    // that keys on the WHOLE string — `SOD_CONFLICT:3f2b…` misses the map and
    // falls back to itself, putting a raw uuid in front of a user (decision C)
    // and losing the sentence at the same time. The absence assertion is the
    // half that fails if the sentence is built by interpolating the id back in.
    const reason = `SOD_CONFLICT:${RULE_ID}`;

    expect(reasonSentence(reason)).toBe(reasonSentence("SOD_CONFLICT"));
    expect(reasonSentence(reason)).not.toContain(RULE_ID);
  });

  it("an unknown reason code renders itself", () => {
    // Catches a lookup whose miss answers a vague default ("This needed a
    // closer look") or an empty string: a reason code added on the Python side
    // would then be invisible on the card, where the raw code is greppable
    // against `ReasonCode` the moment someone reports it (decision B). An
    // implementation returning `undefined` for a miss fails here too, which is
    // what stops the literal "undefined" reaching the DOM.
    expect(reasonSentence("NEW_THING")).toBe("NEW_THING");
  });
});

describe("the four label maps", () => {
  const rows = [
    {
      map: "decision",
      lookup: decisionLabel as Lookup,
      value: "auto",
      label: "Granted automatically",
    },
    {
      map: "decision",
      lookup: decisionLabel as Lookup,
      value: "approval",
      label: "Needs approval",
    },
    { map: "decision", lookup: decisionLabel as Lookup, value: "blocked", label: "Blocked" },
    { map: "risk", lookup: riskLabel as Lookup, value: "low", label: "Low risk" },
    { map: "risk", lookup: riskLabel as Lookup, value: "elevated", label: "Elevated risk" },
    { map: "risk", lookup: riskLabel as Lookup, value: "privileged", label: "Privileged" },
    { map: "risk", lookup: riskLabel as Lookup, value: "critical", label: "Critical" },
    {
      map: "slot",
      lookup: slotLabel as Lookup,
      value: "department_manager",
      label: "Department manager",
    },
    { map: "slot", lookup: slotLabel as Lookup, value: "system_owner", label: "System owner" },
    { map: "slot", lookup: slotLabel as Lookup, value: "it_admin", label: "IT admin" },
    { map: "slot", lookup: slotLabel as Lookup, value: "hr", label: "HR" },
    { map: "status", lookup: statusLabel as Lookup, value: "pending", label: "Pending approval" },
    { map: "status", lookup: statusLabel as Lookup, value: "approved", label: "Approved" },
    { map: "status", lookup: statusLabel as Lookup, value: "rejected", label: "Rejected" },
    { map: "status", lookup: statusLabel as Lookup, value: "executed", label: "Access granted" },
    { map: "status", lookup: statusLabel as Lookup, value: "failed", label: "Failed" },
  ] as const;

  it.each(rows)("$map $value reads $label", ({ lookup, value, label }) => {
    // Sixteen members over four enums, one assertion each. Catches a map keyed
    // by the Python MEMBER NAME (`AUTO`, `DEPARTMENT_MANAGER`) rather than the
    // value `model_dump(mode="json")` puts on the wire — every lookup would
    // miss and the card would read `auto` / `it_admin` at the user, which the
    // fallback makes a silent, green failure. It also catches the two pairs a
    // hand-written map gets wrong by eye: `executed` is "Access granted" and
    // not "Executed" (the user's word, not the table's), and `auto` is
    // "Granted automatically" and not "Approved", which would make an
    // auto-grant read like a filed request somebody signed off.
    expect(lookup(value)).toBe(label);
  });
});

describe("an unknown value in any of the four maps", () => {
  const lookups = [
    { map: "decision", lookup: decisionLabel as Lookup },
    { map: "risk", lookup: riskLabel as Lookup },
    { map: "slot", lookup: slotLabel as Lookup },
    { map: "status", lookup: statusLabel as Lookup },
  ] as const;

  it.each(lookups)("$map renders the raw wire string", ({ lookup }) => {
    // One behaviour, four lookups (§1's second unhappy path). Catches the
    // lookup written as a bare index — `DECISION_LABELS[value]` on a member
    // Python added since renders the literal "undefined" on the card — and the
    // one written with a vague default, which hides the drift instead of
    // showing the value someone can grep for. Parametrised because a fallback
    // added to three of the four is the shape this actually ships as.
    expect(lookup("chief_of_staff")).toBe("chief_of_staff");
  });
});

describe("principalLabel (plan 42 decision D)", () => {
  const principals = [
    { value: "employee", label: "Employee" },
    { value: "manager", label: "Manager" },
    { value: "system_owner", label: "System owner" },
    { value: "hr", label: "HR" },
    { value: "it_admin", label: "IT admin" },
    { value: "auditor", label: "Auditor" },
    { value: "platform_admin", label: "Platform admin" },
  ] as const;

  it.each(principals)("$value reads $label", ({ value, label }) => {
    // Seven members of `helpdesk.auth.rbac.Principal`, one assertion each.
    // Catches a map keyed by the Python member name (`PLATFORM_ADMIN`), which
    // misses every lookup and falls back to the raw value, and the two labels a
    // hand-written map gets wrong by eye: `hr` is "HR" and not "Hr", and
    // `it_admin` is "IT admin" and not "It admin".
    expect(principalLabel(value)).toBe(label);
  });

  it("answers an unknown principal with the value itself", () => {
    // Catches a bare index (`undefined` at the user) and a vague default such
    // as "Unknown", which hides the drift the raw value makes greppable.
    expect(principalLabel("chief_of_staff")).toBe("chief_of_staff");
  });
});
