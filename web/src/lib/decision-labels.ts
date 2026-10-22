/**
 * Sentences for reason codes and labels for policy enums. The server sends
 * codes, never sentences, so the wording lives here. Keys are wire values. An
 * unknown value shows its raw code, which is easy to search for. The SoD rule
 * id is dropped: a raw uuid means nothing to the person asking.
 */

/** `helpdesk.policy.types.ReasonCode` values. */
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
  cancelled: "Cancelled",
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

function labelled(labels: Record<string, string>, value: string): string {
  return labels[value] ?? value;
}

/**
 * The sentence for one reason. `SOD_CONFLICT:{rule_id}` is looked up by the
 * part before the colon, so the id is never shown.
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
