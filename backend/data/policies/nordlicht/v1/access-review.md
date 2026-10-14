# Access review procedure

Document id: NL-PRC-005
Version: 1.0
Owner: Head of IT Security
Effective date: 2025-02-01
Review cycle: annual

## 1 Purpose and scope

Approvals control how access is granted; reviews control whether it should still exist. This procedure defines the quarterly access review at Nordlicht Maschinenbau GmbH: who reviews what, by when, and what happens with the findings. It covers every grant on every system listed in the Access Control Policy, including contractor accounts under the Third-party and Contractor Access Policy.

Expiry already limits how long a grant can live: read roles expire after 365 days at most, write roles after 180, admin roles after 30. The review exists for what expiry cannot catch, which is access that is still valid on paper but no longer matches the person's actual work.

## 2 Schedule

Reviews run four times a year, in the first month of each calendar quarter. The IT security office opens the review in the IT helpdesk portal on the first working day of that month and distributes to each department manager the list of grants held by their employees. Managers have 15 working days to complete their certification. The IT security office consolidates results and closes the review by the end of the month.

## 3 What department managers certify

For each grant on their list, the manager records one of three decisions.

Confirm: the person still needs this access for their current duties. Confirmation does not extend the expiry date; a confirmed grant still expires on schedule and is renewed through the normal request process.

Revoke: the access is no longer needed. Revocation executes within two working days of the decision.

Refer: the manager cannot judge the grant, typically for technical roles. Referred grants go to the system owner, who decides within five working days.

A manager certifies grants, not people. "This employee is trustworthy" is not a valid basis for confirmation; the question is whether the specific role is needed for the specific duties. Managers who confirm a grant are accountable for that confirmation at the next review and in any later audit.

Reviews of a manager's own grants are performed by the department head, and the department heads' grants are reviewed by the managing directors.

## 4 Stale and out-of-template grants

The review list flags two categories for mandatory attention.

Stale grants are grants with no recorded use in the past 90 days, where the system produces usage data. A stale grant can only be confirmed with a written reason, for example a role needed for year-end closing that is idle mid-year. A stale grant confirmed without a reason is treated as unreviewed under section 6.

Out-of-template grants are roles outside the department's role template as defined in the Access Control Policy. These are the grants most likely to have outlived their purpose, because they were requested for a specific task rather than for the role the person holds. Each out-of-template grant must be confirmed individually; bulk confirmation is disabled for this category in the IT helpdesk portal.

## 5 Segregation-of-duties conflicts

The review list marks every person who holds a role pair prohibited by the Segregation of Duties Standard, with the rule cited, for example SoD-STD 2.3 for an employee holding both erp.buyer and erp.goods-receipt. Conflicts should not exist at this point, because requests that would create them are blocked, but system-side changes, migrations, and manual provisioning errors can still produce them.

A flagged conflict cannot be confirmed. The manager must resolve it within 10 working days by revoking one of the two roles, following section 5 of the Segregation of Duties Standard. The review does not close for that department until every flagged conflict is resolved or formally escalated.

## 6 Deadlines and escalation

The 15 working day certification window is firm. Five working days before it ends, the IT helpdesk portal reminds managers with open items and copies their department head.

Grants that remain unreviewed when the window closes are handled by default: out-of-template grants and all privileged or critical grants are suspended, and template grants are flagged for priority handling in the next review. Suspension means the access stops working but the grant record remains; the manager can reinstate it within 20 working days by completing the review decision, after which a suspended grant is revoked.

This default is intentionally uncomfortable. An unreviewed privileged grant that keeps working indefinitely would make the review optional in practice.

## 7 Consequences of missed reviews

A manager who misses the deadline in one quarter receives an escalation to their department head. A manager who misses it in two consecutive quarters is reported to the managing directors, and new non-standard access requests for their department require department head approval until the backlog is cleared. Repeated failure to review is treated as a management performance issue, not an IT issue.

## 8 Records and follow-up

Every review decision is recorded with the reviewer, the date, and the reason where one is required. Revocations triggered by the review are verified by the IT security office within five working days of the review closing; a revocation that did not execute is an incident.

The IT security office reports each quarter to the managing directors: completion rate per department, number of revocations, stale grant ratio, conflicts found and resolved, and suspensions applied. Findings that indicate a structural problem, such as a template that no longer matches a department's work, are passed to the owner of the Access Control Policy for a template change.
