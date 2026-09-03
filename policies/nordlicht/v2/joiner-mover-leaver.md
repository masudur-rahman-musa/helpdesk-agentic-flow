# Joiner, mover, leaver procedure

Document id: NL-PRC-003
Version: 2.0
Owner: Head of IT Security
Effective date: 2026-03-01
Review cycle: annual
Revision history: 2.0 rewrites the procedure, adds a section on returners after long absence, an escalation ladder for late lifecycle actions, and a worked mover example. The lifecycle rules themselves, including the 30-day joiner rule and the 10 working day mover window, are unchanged from version 1.0.

## 1 Purpose and scope

Access must track employment status, automatically and without gaps. This procedure defines what happens to a person's access at the three lifecycle events: joining Nordlicht Maschinenbau GmbH, transferring between departments, and leaving. It applies to all employees. Contractor accounts follow the Third-party and Contractor Access Policy, which points back to the leaver steps here.

The Access Control Policy governs individual requests. This procedure governs the events around them: what is provisioned without a request, what is revoked without a request, and what must be re-confirmed when circumstances change.

## 2 Joining

### 2.1 What is provisioned automatically

HR enters the new employee into the HR System with three facts: a start date, a department, and a manager. On the start date, the department's role template from the Access Control Policy is provisioned automatically. Every template contains the base set of vpn.user, m365.user, a wiki role, and badge.holder, plus the department's line-of-business roles, for example plm.engineer in Engineering or erp.buyer in Procurement.

Template grants expire like any other grant: a template read role after 365 days, a template write role after 180 days, renewed afterward through the normal request process. Two things are never in a template. No template holds an admin role. And no template holds a role on the payment approval side; payments.approver is always an individual request under the Access Control Policy.

Within the first week, the manager compares the provisioned set against the template and reports gaps through the IT helpdesk portal. Provisioning failures reported this way go to the responsible system owner.

### 2.2 The 30-day rule

During the first 30 days of employment, every access request naming the new employee that would otherwise classify as low is classified elevated instead. The department manager therefore approves it. The rule applies whether the employee submits the request themselves or a colleague submits it for them. It does not touch the template provisioning on the start date; only additional requests are affected.

The rule has two reasons. Impersonation attempts concentrate on new employees, whose faces and voices colleagues do not yet know. And early role assignments carry the highest error rate, because the requester often guesses at what the new person will need. From day 31, the standard classification of the Access Control Policy applies with no adjustment.

## 3 Moving between departments

### 3.1 The template diff

HR records the transfer with an effective date. On that date, the old and the new department templates are compared and three sets fall out. Roles only in the new template are provisioned. Roles in both templates continue unchanged, keeping their existing expiry dates. Roles only in the old template are revoked on the effective date.

### 3.2 Individually requested grants

Access the employee obtained by individual request, outside any template, does not follow them silently. Each such grant is revoked 10 working days after the transfer date unless the new department manager confirms in the IT helpdesk portal that the need continues. Confirmation is not renewal; a confirmed grant keeps its original expiry date. The window lets handover work finish without the employee ending up with the union of two departments' access.

### 3.3 Segregation of duties on transfer

Before the diff is provisioned, it is checked against the Segregation of Duties Standard. When a role in the new template conflicts with a grant the employee still holds, the old conflicting grant is revoked before the new role is provisioned, and the rule number is recorded on the transfer.

### 3.4 A worked example

A Procurement employee moves to Finance. The diff revokes erp.buyer, vendor-master.editor, crm.viewer, and wiki.editor, provisions finance-dash.viewer, payments.initiator, and wiki.viewer, and keeps erp.viewer, vpn.user, m365.user, and badge.holder unchanged. Suppose the employee also holds an individually granted payments.approver role from covering a previous project. The new template contains payments.initiator, and SoD-STD 2.1 prohibits holding both. The payments.approver grant is revoked before payments.initiator is provisioned, and SoD-STD 2.1 is recorded on the transfer.

## 4 Returning after long absence

An employee returning from a recorded long absence, such as parental leave or extended sick leave, is neither a joiner nor a mover, and this section closes that gap. Grants suspended during the absence, including a badge disabled under the Physical Access Policy, are re-enabled on the return date HR records. Before re-enabling, the manager reviews the employee's individually requested grants in the IT helpdesk portal and confirms which are still needed; unconfirmed ones are revoked on the return date. Template roles are re-enabled without individual confirmation, since the template review happens quarterly anyway. The 30-day rule of section 2.2 does not apply to returners; their identity is established and their roles are known.

## 5 Leaving

### 5.1 When offboarding starts

HR records the leaving date in the HR System as soon as it is agreed. Offboarding starts on the date HR sets. Normally that is the last working day; it is earlier when the manager and HR decide access should end before the employment does, for example during release from duty.

### 5.2 Everything is revoked

At the start of offboarding, revocation is total: template roles, individually requested roles, and badge.holder, which disables the physical badge per the Physical Access Policy. Open VPN and m365 sessions are terminated. Mail delivery and file retention follow the Data Classification Standard, but the leaver's own access ends in every case.

There is no partial offboarding and no informal extension. A successor who needs the leaver's data gets access to the data through a request by the manager; the leaver's account is never kept alive as a workaround.

### 5.3 No new requests

From the start of offboarding, every new access request naming the leaver as the target person is blocked, regardless of who submits it, renewals included. The block lifts only when HR reverses the leaving record, for example a withdrawn resignation. In that case the person is re-provisioned as a joiner, and the 30-day rule of section 2.2 applies again.

## 6 Deadlines, responsibilities, and escalation

HR enters start dates, transfer dates, leaving dates, and long-absence records no later than one working day after they are agreed. Every automatic step in this procedure hangs on those dates, so a late entry is a security defect, not a clerical one.

Managers verify joiner provisioning within one week, decide on movers' individually requested grants within the 10 working day window, review returners' grants before the return date, and confirm handover for leavers. System owners fix reported provisioning failures on their systems within two working days.

The IT security office verifies that revocations executed and samples leaver accounts monthly. A lifecycle action still open after five working days goes to the Head of IT Security. One that is still open after ten working days goes to the responsible department head, and the affected grants are suspended until the action completes. Findings from the quarterly review follow the Access Review Procedure.

## 7 Records

Every provisioning, revocation, suspension, re-enablement, confirmation, and block under this procedure is recorded with the triggering date and the role of the responsible person. Records are kept for three years. Employees can list their own current grants at any time in the IT helpdesk portal or by asking the access request assistant.
