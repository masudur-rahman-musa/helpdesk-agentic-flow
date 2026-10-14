# Joiner, Mover, Leaver Procedure

Document id: CU-POL-003
Version: 2.0
Owner: Head of Information Security
Effective date: 2026-02-09
Review cycle: annual
Revision history: 2.0 rewrites the procedure with separate responsibility statements per event, adds a section on secondments and long absences and a clarified escalation path for missed timelines. Templates, provisioning rules, the first-30-days rule, and revocation timelines are unchanged from 1.0.

## 1. What this procedure covers

Access at Cuan Financial Services Ltd changes at three moments in a working life: when a person joins, when they transfer between departments, and when they leave. This procedure governs those three events for employees, and for contractors with the variations set out in the Third-party and Contractor Access Policy (CU-POL-009). How an individual request is classified and approved belongs to the Access Control Policy (CU-POL-001); this document deals with the lifecycle around requests.

## 2. Department role templates

A role template is the standard working set of system roles for one department. The department head and the information security office own each template jointly, and the template is the reference point for the standard versus non-standard distinction in the Access Control Policy. The current templates are:

Engineering: github.developer, datadog.viewer, data-warehouse.engineer, vpn.user, m365.user, wiki.editor.

Operations: core-banking.operator, ledger.viewer, vpn.user, m365.user, wiki.viewer.

Risk & Compliance: kyc.analyst, data-warehouse.analyst, ledger.viewer, vpn.user, m365.user, wiki.viewer.

Finance: ledger.editor, data-warehouse.analyst, vpn.user, m365.user, wiki.viewer.

Customer Support: support-desk.agent, crm.viewer, vpn.user, m365.user, wiki.viewer.

HR: hris.viewer, vpn.user, m365.user, wiki.viewer.

IT: github.developer, datadog.viewer, vpn.user, m365.user, wiki.editor.

### 2.1 Changing a template

The department head proposes the change. The information security office checks the proposed set against the Segregation of Duties Standard (CU-POL-002) before it takes effect. A changed template applies to future joiners and movers only: no one already in the department is granted a newly added role automatically. An existing member who wants the added role requests it in the ordinary way, and because the role is now in the template, the request classifies as standard.

## 3. Joining

HR enters the start date, department, and manager for each new joiner into the HR System. On the start date, IT provisions every account and role in the joiner's department template without any request or approval; the provisioning record names the joiner and the template version applied. The firm account and mailbox take the form firstname.lastname@cuan.example, with credentials issued under the Password and Account Recovery Procedure (CU-POL-007).

Template roles granted at joining are not open-ended. Each takes the default expiry for its privilege from the Access Control Policy: 365 days for read, 180 for write, 90 for admin. When a template role expires, renewing it is a normal request.

### 3.1 The probation window

During the first 30 days of employment, any request for the new joiner that would otherwise classify as low risk is raised to elevated, which means the department manager approves it. The firm holds this line for a plain reason: in the first month, background checks may still be in flight, the firm has little history with the person, and the person is still discovering which systems their work genuinely requires. A short manager check on each extra grant costs little against the risk of quiet accumulation. The lift affects requests that would otherwise be granted automatically, which in practice means template roles; a request beyond the template already carries an approval requirement of its own. The template itself lands on day one as described above.

## 4. Transfers between departments

When an employee moves department, by promotion, reorganisation, or role change, HR records the transfer with an effective date, and IT acts on that date in three steps.

Step one, template reconciliation. IT compares the old and new templates. Missing roles from the new template are provisioned automatically, exactly as at joining. Roles that appear only in the old template are revoked on the effective date. Where live work must be handed over, the old department head may ask for a handover extension of up to 30 days on a named role; the extension is classified and approved as a non-standard request under the Access Control Policy.

Step two, out-of-template review. Access the person requested individually while in the old department does not follow them silently. Each such grant is flagged to the new department manager, who has ten business days to confirm it is still needed. Confirmed grants keep their existing expiry; anything unconfirmed is revoked on day eleven.

Step three, conflict check. The person's full resulting role set is checked against the Segregation of Duties Standard, because a newly provisioned template role can complete a conflicting pair with a grant carried over. Conflicts are remediated under section 4 of that standard.

## 5. Secondments and long absences

A secondment to another department is handled as a transfer under section 4 in both directions: templates are reconciled at the start of the secondment and again at its end, and out-of-template grants are reviewed by the receiving manager both times. A secondment is never run by leaving the old access in place alongside the new.

For a planned absence longer than 60 calendar days, such as extended leave, HR records the absence and IT suspends the person's accounts for its duration. Suspension preserves every grant record; on return, access resumes without fresh requests, minus anything that expired during the absence, which is renewed in the ordinary way. Suspension for absence is an operational safeguard and does not alter any grant's classification or expiry.

## 6. Leaving

A leaver is anyone whose employment or engagement ends: resignation, dismissal, retirement, or contract end. HR records the leaving date in the HR System as soon as it is known. For a dismissal with immediate effect, HR alerts IT directly, before the conversation concludes where practicable.

The moment a person is marked as offboarding, every new access request for them is blocked: requests they submit, requests submitted for them, at every risk class, with no approval route around the block. This is the block stated in the Access Control Policy.

On the leaving date, IT revokes everything: template roles, individually requested grants, VPN, and the m365 account. Facilities handles the access card under the Physical Access Policy (CU-POL-008). For a standard leaver, revocation completes by the end of the leaving day; for an immediate dismissal, within one hour of notification. The mailbox is kept suspended under the retention schedule of the Data Classification Standard (CU-POL-010), and no one signs in to it.

A leaver's account is never parked for reuse, shared with a successor, or kept open for continuity. Where the team needs the leaver's data, IT delegates ownership of it; the account itself dies on schedule.

## 7. Who owns what

HR owns the dates: start, transfer, absence, and leaving dates in the HR System, entered at least two business days ahead wherever the event is known in advance. IT owns provisioning and revocation to the timelines in sections 3, 4, and 6. Department managers own the ten-day confirmation of out-of-template grants for movers and their part in the Access Review Procedure (CU-POL-005). The information security office owns this procedure and monitors that everyone else meets their part of it.

### 7.1 Escalation for missed timelines

A missed timeline, such as a late leaver revocation or an unconfirmed mover grant past day eleven, is raised by the information security office to the responsible owner on discovery, and to that owner's manager if not corrected within two business days. Every missed timeline, corrected or not, appears in the quarterly report to the executive, because a lifecycle control that runs late is the same exposure as one that does not run.

## 8. Records

Provisioning events, revocations, handover extensions, suspension and resumption for absences, and mover confirmations are all recorded with dates and acting persons. The records feed the quarterly access reviews and are produced to our regulator on request.
