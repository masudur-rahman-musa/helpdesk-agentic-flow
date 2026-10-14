# Joiner, mover, leaver procedure

Document id: NL-PRC-003
Version: 1.0
Owner: Head of IT Security
Effective date: 2025-02-01
Review cycle: annual

## 1 Purpose and scope

This procedure defines how access to company systems is created when an employee joins Nordlicht Maschinenbau GmbH, changed when they move between departments, and removed when they leave. It applies to all employees. Contractor accounts follow the Third-party and Contractor Access Policy, which references the leaver steps in this document.

The procedure exists so that access always matches employment status. The Access Control Policy sets the rules for individual requests; this procedure covers the lifecycle events that happen around them.

## 2 Joiners

### 2.1 Auto-provisioning from the department template

HR records the new employee in the HR System with a start date, a department, and a manager. On the start date, the accounts and roles listed in the department's role template are provisioned automatically. The templates are defined in the Access Control Policy and cover, for every department, the base set: vpn.user, m365.user, a wiki role, badge.holder, and the department's line-of-business roles, such as plm.engineer for Engineering or erp.buyer for Procurement.

Template grants carry the same expiry rules as any other grant. A template role with read privilege expires after 365 days and a template role with write privilege after 180 days, after which it is renewed through the normal request process. No template contains an admin role, and no template contains a role on the payments approval side; payments.approver is always an individual request under the Access Control Policy.

The new employee's manager checks within the first week that the provisioned set is complete and correct, and reports gaps through the IT helpdesk portal.

### 2.2 The first 30 days

For the first 30 days of employment, any access request for the new employee that would otherwise be classified low is classified elevated instead, which means the department manager must approve it. This applies both to requests the employee submits and to requests a colleague submits on their behalf. The rule reflects two facts: new employees are the most frequent target of impersonation attempts, and early role assignments are the most likely to be wrong. Template provisioning on the start date is not affected; the rule applies to additional requests only.

After day 30 the standard classification in the Access Control Policy applies without adjustment.

## 3 Movers

### 3.1 Template diff

When an employee transfers to another department, HR records the transfer with an effective date. On that date the old and new department templates are compared. Roles present only in the new template are provisioned. Roles present in both templates are kept unchanged, with their existing expiry dates. Roles present only in the old template are revoked on the effective date.

### 3.2 Out-of-template grants

Grants that were never part of any template, meaning access the employee requested individually, are not carried over silently. Each such grant is revoked 10 working days after the transfer date unless the new department manager confirms through the IT helpdesk portal that it is still needed. A confirmed grant keeps its original expiry date; confirmation is not a renewal. This window exists so that handover work can finish without the employee accumulating the union of two departments' access.

### 3.3 Segregation of duties on transfer

The template diff is checked against the Segregation of Duties Standard before provisioning. If a new template role conflicts with a grant the employee still holds, for example a Procurement employee holding an individually granted payments.approver role moving into a template that contains vendor-master.editor, the conflicting old grant is revoked before the new role is provisioned. The rule number, such as SoD-STD 2.2, is recorded on the transfer.

## 4 Leavers

### 4.1 Notice and offboarding start

HR records the leaving date in the HR System as soon as it is agreed. Offboarding begins on the date HR sets, normally the last working day, and earlier when the manager and HR decide that access should end before the employment does, for example during release from duty.

### 4.2 Full revocation

At the start of offboarding, all grants are revoked: template roles, individually requested roles, and the badge.holder role, which disables the physical badge as described in the Physical Access Policy. VPN and m365 sessions are terminated. Mail delivery and file access are handled per the retention rules in the Data Classification Standard; access for the leaver themselves ends in every case.

Revocation is automatic and complete. There is no partial offboarding and no informal extension. Where a successor needs the leaver's data, the manager requests access to the data for the successor; the leaver's account is never kept alive for this purpose.

### 4.3 Block on new requests

From the start of offboarding, every new access request naming the leaver as the target person is blocked, whoever submits it. This includes renewals of expiring grants. The block is lifted only if HR reverses the leaving record, for example when a resignation is withdrawn, in which case access is re-provisioned as for a joiner, including the 30-day rule in section 2.2.

## 5 Timelines and responsibilities

HR maintains start dates, transfer dates, and leaving dates in the HR System and enters them no later than one working day after they are agreed. These dates drive every automatic step in this procedure, so a late entry is a security defect, not an administrative one.

Managers verify joiner provisioning within one week, decide on out-of-template grants for movers within 10 working days, and confirm to HR that a leaver's duties are handed over. System owners resolve provisioning failures on their systems within two working days of a report.

The IT security office monitors that revocations executed, samples leaver accounts monthly, and reports unresolved lifecycle actions older than five working days to the Head of IT Security. Discrepancies found during the quarterly review are handled under the Access Review Procedure.

## 6 Records

Every provisioning, revocation, confirmation, and block under this procedure is recorded with its trigger date and the responsible person's role. Records are retained for three years. An employee can see their own current grants at any time in the IT helpdesk portal and can ask the access request assistant to list them.
