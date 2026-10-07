/**
 * One sentence per reason code, one label per enum member, in one place
 *.
 *
 * The wire carries MACHINE CODES and never a human sentence:
 * `agent_api/decision_card.py` says so in as many words — turning
 * `SOD_CONFLICT:{rule_id}` into a sentence is this app's job, in the language
 * its labels are written in. Every key here is the enum's VALUE as
 * `model_dump(mode="json")` puts it on the wire (`auto`, `it_admin`,
 * `pending`), never the Python member name, which is the failure
 * `tests/decision-labels.test.ts`'s sixteen-row case exists to catch: a map
 * keyed by member name misses on every lookup and the fallback makes it
 * silently green.
 *
 * Six vocabularies, six lookups, all total by construction. A value the map
 * does not know answers THE VALUE ITSELF (decision B): a reason exists to
 * explain, a vague default explains nothing and hides the drift, and the raw
 * code is greppable against `ReasonCode` the moment someone reports it. That
 * is the opposite of `chat-errors.ts`'s `default` on purpose — there a code is
 * all the wire sends and the sentence is the whole message.
 *
 * The maps are a HAND-KEPT mirror of six Python enums: a member
 * added on the Python side renders as its raw code with nothing red, so
 * `tests/decision-labels.test.ts` lists all ten reason codes and every member
 * of the other four as LITERALS. Adding one is a deliberate edit to that list.
 *
 * No sentence names a tenant, a user, a tool or an id. The SoD rule id in
 * particular is dropped rather than rendered (decision C): a raw uuid reads as
 * nothing to the person asking, and it belongs to `/approvals`' full decision
 * trace. Pinned by `::a SOD_CONFLICT carrying a rule id reads as the bare code
 * does`, whose second half fails if the id is interpolated back in.
 */

/** the design's ten codes, as `helpdesk.policy.types.ReasonCode` values. */
export const REASON_SENTENCES: Record<string, string> = {
  IN_TEMPLATE: "This access is part of the standard set for your role.",
  NOT_IN_TEMPLATE: "This access is not part of the standard set for your role.",
  SENSITIVITY_HIGH: "The data behind this system is highly sensitive.",
  PRIVILEGE_ADMIN: "You asked for admin rights rather than read or write access.",
  CRITICAL_SYSTEM: "This system is business critical.",
  NEW_JOINER: "You joined recently, so new access is looked at more closely.",
  SOD_CONFLICT: "This would conflict with access you already hold.",
  EXPIRY_CAPPED: "The access period was shortened to the longest this policy allows.",
  SELF_ADMIN_ESCALATED: "You asked for admin rights over your own account.",
  TARGET_LEAVER: "The person this is for is leaving, so access cannot be extended.",
};

/** `helpdesk.policy.types.Decision`. */
export const DECISION_LABELS: Record<string, string> = {
  auto: "Granted automatically",
  approval: "Needs approval",
  blocked: "Blocked",
};

/** `helpdesk.policy.types.RiskClass`. */
export const RISK_LABELS: Record<string, string> = {
  low: "Low risk",
  elevated: "Elevated risk",
  privileged: "Privileged",
  critical: "Critical",
};

/** `helpdesk.policy.types.ApproverSlot`. */
export const SLOT_LABELS: Record<string, string> = {
  department_manager: "Department manager",
  system_owner: "System owner",
  it_admin: "IT admin",
  hr: "HR",
};

/** `helpdesk.models.access.RequestStatus`. */
export const STATUS_LABELS: Record<string, string> = {
  pending: "Pending approval",
  approved: "Approved",
  rejected: "Rejected",
  executed: "Access granted",
  failed: "Failed",
};

/** `helpdesk.auth.rbac.Principal`, in the enum's own order. */
export const PRINCIPAL_LABELS: Record<string, string> = {
  employee: "Employee",
  manager: "Manager",
  system_owner: "System owner",
  hr: "HR",
  it_admin: "IT admin",
  auditor: "Auditor",
  platform_admin: "Platform admin",
};

/** The label for a known member, or the wire value itself for anything else. */
function labelled(labels: Record<string, string>, value: string): string {
  return labels[value] ?? value;
}

/**
 * The sentence for one reason, bare code or `SOD_CONFLICT:{rule_id}` alike.
 *
 * `sod_conflict_reason` is the only reason that carries a value, so the lookup
 * is keyed on the part before the colon and the id is never read again. A code
 * the map does not know answers itself, colon and all.
 */
export function reasonSentence(reason: string): string {
  const code = reason.split(":", 1)[0] ?? reason;
  return REASON_SENTENCES[code] ?? reason;
}

export function decisionLabel(decision: string): string {
  return labelled(DECISION_LABELS, decision);
}

export function riskLabel(riskClass: string): string {
  return labelled(RISK_LABELS, riskClass);
}

export function slotLabel(slot: string): string {
  return labelled(SLOT_LABELS, slot);
}

export function statusLabel(status: string): string {
  return labelled(STATUS_LABELS, status);
}

export function principalLabel(principal: string): string {
  return labelled(PRINCIPAL_LABELS, principal);
}
