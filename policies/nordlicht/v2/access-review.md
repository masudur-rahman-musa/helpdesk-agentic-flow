# Access review procedure

Document id: NL-PRC-005
Version: 2.0
Owner: Head of IT Security
Effective date: 2026-03-01
Review cycle: annual
Revision history: 2.0 reorganizes the procedure around the review lifecycle, adds a section on review evidence and its retention, a preparation step for managers, and a clarified escalation ladder. Decision options, deadlines, and default handling are unchanged from version 1.0.

## 1 Why the review exists

The approval process controls how access comes into being. Expiry controls how long a grant can live at most: read roles end after 365 days at the latest, write roles after 180, admin roles after 30. Neither control answers the question this procedure exists for: is the access still the right access today? A grant can be validly approved, far from expiry, and wrong anyway, because the person's work changed.

This procedure defines the quarterly access review at Nordlicht Maschinenbau GmbH: what is reviewed, by whom, on what timeline, and what happens with the outcome. It covers every grant on every system in the Access Control Policy inventory, contractor accounts under the Third-party and Contractor Access Policy included.

## 2 The review cycle

The review runs four times a year, in the first month of each calendar quarter.

Opening: the IT security office opens the review in the IT helpdesk portal on the first working day of the month and sends each department manager the list of grants held by their employees.

Certification: managers have 15 working days to record a decision on every grant on their list.

Consolidation: the IT security office consolidates the results, verifies revocations, and closes the review by the end of the month.

Who reviews whom follows the reporting line upward. Managers review their employees' grants. A manager's own grants are reviewed by the department head, and the department heads' grants by the managing directors. Contractor grants are reviewed by their sponsors, as the Third-party and Contractor Access Policy requires.

## 3 Preparing to certify

Before recording decisions, a manager reads the list with three questions in mind. Which grants belong to duties the person no longer has? Which grants were requested for a task that has ended? Which grants would I refuse if they were requested today? The portal supports this by showing, for each grant, the original justification, the grant date, the expiry date, and last recorded use where the system produces usage data.

A manager certifies grants, not people. "This employee is trustworthy" is not a reason to confirm anything; the only question is whether the specific role is needed for the specific current duties. A manager who confirms a grant answers for that confirmation at the next review and in any later audit.

## 4 The three decisions

Confirm: the access is needed for current duties. Confirmation never extends expiry; a confirmed grant still ends on schedule and is renewed through the normal request process under the Access Control Policy.

Revoke: the access is no longer needed. The revocation executes within two working days of the decision.

Refer: the manager cannot judge the grant, typically a technical role. The grant goes to the system owner, who decides within five working days. A referral is a decision to hand over, not a way to avoid deciding; referred grants appear in the department's completion statistics until the owner has ruled.

## 5 Grants that get mandatory attention

The review list flags two categories that cannot be waved through.

Stale grants show no recorded use in the past 90 days, on systems that produce usage data. Confirming a stale grant requires a written reason, for example a role used only during year-end closing. A stale grant confirmed without a reason counts as unreviewed under section 7.

Out-of-template grants are roles outside the department template defined in the Access Control Policy. They were requested for a specific purpose rather than attached to the job, which makes them the grants most likely to outlive that purpose. Each one is confirmed individually; the portal disables bulk confirmation for this category.

## 6 Segregation-of-duties findings

The list marks every person holding a role pair the Segregation of Duties Standard prohibits, with the rule cited, for example SoD-STD 2.3 for someone holding both erp.buyer and erp.goods-receipt. Such pairs should not exist, since requests that would create them are blocked, but migrations, system-side changes, and manual provisioning errors produce them anyway.

A flagged conflict cannot be confirmed. The manager resolves it within 10 working days by revoking one of the two roles, following section 5 of the Segregation of Duties Standard. The department's review stays open until every flagged conflict is resolved or formally escalated to the Head of IT Security.

## 7 Missed deadlines and default handling

The 15 working day window is firm. Five working days before it closes, the portal reminds managers with open items and copies their department head.

When the window closes, unreviewed grants are handled by default. Out-of-template grants and all privileged or critical grants are suspended: the access stops working while the grant record remains. Template grants are flagged for priority handling in the next review. A manager can reinstate a suspended grant within 20 working days by completing the review decision; after that, the suspended grant is revoked.

The default is meant to sting. If an unreviewed privileged grant simply kept working, the review would be optional in practice, and the discomfort of a suspension lands on the manager who did not decide, not on the process.

The escalation ladder for the manager is separate from the grant handling. One missed quarter: escalation to the department head. Two consecutive missed quarters: report to the managing directors, and new non-standard access requests for that department require department head approval until the backlog is cleared. Repeated failure to review is a management performance issue, not an IT issue.

## 8 Review evidence and retention

Every decision is recorded with the reviewer, the date, the decision, and the reason where one is required. The evidence set for a review consists of the opening list as distributed, every decision record, every suspension and reinstatement, and the closing report. This set is frozen when the review closes and is retained for three years, so that any later audit can reconstruct who saw what and decided what. Review evidence is readable by the IT security office and internal audit and is protected from modification after closing.

Revocations triggered by the review are verified by the IT security office within five working days of closing. A revocation that did not execute is an incident, not a follow-up item.

## 9 Reporting and follow-up

After each quarter, the IT security office reports to the managing directors: completion rate per department, revocations, stale grant ratio, conflicts found and resolved, and suspensions applied. A finding that points at a structural cause, for example a template that no longer matches a department's actual work, goes to the owner of the Access Control Policy as a proposed template change rather than being rediscovered every quarter. Questions about a specific review item go to it-security@nordlicht.example with the review and grant reference.
