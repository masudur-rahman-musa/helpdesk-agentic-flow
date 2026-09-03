# Joiner, Mover, Leaver Procedure

Document id: CU-POL-003
Version: 1.0
Owner: Head of Information Security
Effective date: 2025-01-13
Review cycle: annual

## 1. Purpose and scope

This procedure describes how access changes when a person joins Cuan Financial Services Ltd, transfers between departments, or leaves. It applies to employees and, with the variations in the Third-party and Contractor Access Policy (CU-POL-009), to contractors. The classification and approval of individual requests is defined in the Access Control Policy (CU-POL-001); this procedure covers the lifecycle events around them.

## 2. Department role templates

Each department has a role template: the standard set of system roles its members need for ordinary work. Templates are owned jointly by the department head and the information security office and are the reference for what counts as standard access.

Engineering: github.developer, datadog.viewer, data-warehouse.engineer, vpn.user, m365.user, wiki.editor.

Operations: core-banking.operator, ledger.viewer, vpn.user, m365.user, wiki.viewer.

Risk & Compliance: kyc.analyst, data-warehouse.analyst, ledger.viewer, vpn.user, m365.user, wiki.viewer.

Finance: ledger.editor, data-warehouse.analyst, vpn.user, m365.user, wiki.viewer.

Customer Support: support-desk.agent, crm.viewer, vpn.user, m365.user, wiki.viewer.

HR: hris.viewer, vpn.user, m365.user, wiki.viewer.

IT: github.developer, datadog.viewer, vpn.user, m365.user, wiki.editor.

Template changes are requested by the department head, reviewed by the information security office for segregation conflicts under the Segregation of Duties Standard (CU-POL-002), and take effect for future joiners and movers only. Existing staff are not retro-granted a role added to a template; they request it, and it classifies as standard access.

## 3. Joiners

HR records the start date, department, and manager of every new joiner in the HR System. On the start date, IT provisions the accounts and roles in the new joiner's department template automatically. No request and no approval is needed for template roles at joining; the provisioning is recorded against the joiner with the template version used.

The joiner's firm account and mailbox use the address firstname.lastname@cuan.example. Credentials are issued as described in the Password and Account Recovery Procedure (CU-POL-007).

Template provisioning still carries expiry. Each granted role takes the default expiry for its privilege as set in the Access Control Policy: 365 days for read roles, 180 for write, 90 for admin. Renewals at expiry run through the normal request path.

### 3.1 The first 30 days

For the first 30 days of employment, any access request for the new joiner that would classify as low risk is lifted to elevated and therefore needs the department manager's approval. The reason is practical. In the first month the firm has limited history with the person, background checks may still be completing, and the person is still learning which systems their duties actually need. A brief manager check on every extra grant in that window is a small cost against the exposure of unattended accumulation. The lift applies to requests that would otherwise be granted automatically, which in practice means template roles; a request beyond the template already needs approval on its own. The template itself is provisioned on day one as above.

## 4. Movers

A mover is an employee transferring between departments, whether by promotion, reorganisation, or role change. HR records the transfer in the HR System with an effective date, and the following runs on that date.

IT computes the difference between the old and new department templates. Roles in the new template that the person lacks are provisioned automatically, the same as for a joiner. Roles from the old template that are absent from the new one are revoked on the effective date. The old department head may request a handover extension of up to 30 days for a specific role where live work must transfer; the extension classifies and routes as a non-standard request under the Access Control Policy.

Grants outside any template, that is, access the person requested individually while in the old department, do not carry over silently. Each out-of-template grant is flagged to the new department manager, who has ten business days to confirm the grant is still needed. A confirmed grant keeps its existing expiry. An unconfirmed grant is revoked on day eleven.

Every transfer triggers a segregation of duties check across the person's resulting role set, because a new template role can complete a conflicting pair with a retained grant. Conflicts found are handled under section 4 of the Segregation of Duties Standard.

## 5. Leavers

A leaver is any person whose employment or engagement ends, whether by resignation, dismissal, retirement, or contract end. HR records the leaving date in the HR System as soon as it is known; for dismissals with immediate effect, HR notifies IT directly before the conversation concludes where practicable.

From the moment a person is marked as offboarding, every new access request for them is blocked. This applies to requests they make themselves and to requests made on their behalf, at every risk class, with no approval path around it. The block is stated in section 7 of the Access Control Policy.

On the leaving date IT revokes all access: template roles, individually requested grants, VPN, and the m365 account. Physical access cards are handled under the Physical Access Policy (CU-POL-008). Revocation is completed by end of the leaving day for standard leavers and within one hour of notification for immediate dismissals. The mailbox is retained in a suspended state under the retention schedule in the Data Classification Standard (CU-POL-010); no one signs in to it.

Managers must not park a leaver's credentials for reuse, share their sign-in with a successor, or request that an account stay open for continuity. Continuity needs are met by delegating data ownership through IT, not by keeping the account alive.

## 6. Timelines and responsibilities

HR owns the accuracy of start dates, transfer dates, and leaving dates in the HR System and enters them at least two business days before the event where the event is known in advance. IT owns provisioning and revocation and completes template provisioning on the start or transfer date and revocation as in section 5. Department managers own confirmation of out-of-template grants for movers within ten business days and participation in the reviews under the Access Review Procedure (CU-POL-005). The information security office owns this procedure, monitors completion, and reports missed timelines quarterly.

## 7. Records

Every provisioning, revocation, handover extension, and mover confirmation is recorded with dates and actors. These records support the quarterly access reviews and are produced to our regulator on request.
