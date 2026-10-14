# Access Control Policy

Document id: CU-POL-001
Version: 1.0
Owner: Head of Information Security
Effective date: 2025-01-06
Review cycle: annual

## 1. Purpose and scope

This policy governs how employees and contractors of Cuan Financial Services Ltd obtain, hold, and lose access to the firm's information systems. It applies to every system the firm operates or subscribes to, to every person with a Cuan account, and to every request for access, whether made through the access request assistant, the IT helpdesk portal, or directly to the information security office.

Cuan is a regulated payments and lending firm. Our regulator expects us to show, for any account on any system, who approved it, why, and when it expires. This policy is the anchor for that evidence. Related documents are the Segregation of Duties Standard, the Joiner, Mover, Leaver Procedure, the Privileged Access Standard, and the Access Review Procedure.

## 2. Sensitivity tiers

Every system in the firm's inventory carries one of four sensitivity tiers. The tier drives how access requests are classified and approved.

Critical systems hold customer money, customer identity data, or the means to alter either. They are: Core Banking (core-banking), Payments Gateway (payments-gateway), KYC (kyc), the HR System (hris), and Production AWS (prod-aws).

High systems hold financial records or aggregated customer data. They are: Ledger (ledger) and Data Warehouse (data-warehouse).

Medium systems support daily work and hold limited customer or internal data. They are: crm, support-desk, vpn, github, and m365.

Low systems hold operational or public material. They are: datadog and wiki.

The information security office maintains the inventory and assigns the tier when a system is onboarded. A system without an assigned tier is treated as critical until classified.

## 3. Standard and non-standard access

Each department has a role template: the set of system roles a new joiner in that department receives automatically. The templates are defined in the Joiner, Mover, Leaver Procedure. Access is standard when the requested role appears in the template of the requester's department. Access is non-standard when it does not. Standard access is the default working set for the department; non-standard access always needs a stated business reason.

## 4. Risk classification of a request

Every access request is classified into one of four risk classes: low, elevated, privileged, or critical. Classification follows these rules, applied in order, with the highest matching class winning.

Any role on a critical system is classified critical, regardless of the privilege level or template membership. Any request for admin privilege on any system is classified at least privileged. A request for a role that is not in the requester's department template is classified privileged when the system is high sensitivity, and elevated when the system is low or medium sensitivity. A request for a role that is in the template is classified low when the system is low or medium sensitivity. A template role on a high sensitivity system is classified low when the privilege is read and elevated when the privilege is write.

One adjustment applies. When the person the access is for has been employed for fewer than 30 days, a request that would otherwise be low is lifted to elevated. The reasoning is in the Joiner, Mover, Leaver Procedure.

## 5. Approval

The risk class determines who must approve before access is granted.

Low requests are granted automatically. No human approver is involved, and the grant is recorded with its classification.

Elevated requests need approval from the department manager of the person the access is for.

Privileged requests need two approvals: the department manager and the owner of the target system.

Critical requests need three approvals: the department manager, the system owner, and a third approver. For production and infrastructure systems the third approver is IT administration. For the HR System the third approver is HR.

Two rules override the chain. A manager cannot approve a request made for themselves; in that case the approval moves to the next level, so the system owner approves, and if the system owner is also the requester, IT administration approves. Second, a request a person makes for admin access for themselves never auto-approves and always escalates one approval level above what the class alone would require. The Privileged Access Standard describes both rules in detail.

## 6. Expiry

No grant is permanent. Every grant carries an expiry date, and the Access Review Procedure catches anything that outlives its purpose.

The default expiry depends on the privilege of the role: read roles default to 365 days, write roles to 180 days, and admin roles to 90 days.

Each risk class also carries a cap: low 365 days, elevated 180 days, privileged 90 days, critical 30 days. Admin privilege is capped at 30 days regardless of class.

The effective expiry of a grant is the minimum of three values: the duration the requester asked for, the default for the role's privilege, and the applicable cap. A request that asks for longer than the cap allows is not rejected; it proceeds at the cap, and the over-ask is recorded on the request.

## 7. Blocked requests

Some requests are refused outright rather than routed for approval.

A request is blocked when granting it would create a segregation of duties conflict under the Segregation of Duties Standard. The refusal cites the specific rule, for example SoD-STD 2.2. A conflict exists when the person already holds the paired role or has an approved request for it that has not yet been executed.

A request is blocked when the person it is for is offboarding or has left the firm. Leavers receive no new access under any approval path; see the Joiner, Mover, Leaver Procedure.

A blocked request can only be revisited after the conflict or status that caused the block is resolved. Approvers cannot override a block.

## 8. Requesting access

Employees request access through the access request assistant or the IT helpdesk portal. A request states the system, the role, the person the access is for, the duration wanted, and the business reason. Requests made by a manager on behalf of a team member follow the same classification and approval rules as requests people make for themselves, except for the self-approval and self-requested admin rules in section 5.

## 9. Records

Every request, classification, approval, refusal, grant, and revocation is recorded with a timestamp and the identity of each actor. These records are retained for the period our regulator requires and are the primary evidence produced during supervisory inspections and the quarterly reviews described in the Access Review Procedure.

## 10. Exceptions

Exceptions to this policy require written approval from the Head of Information Security, a stated end date, and an entry in the exceptions log. An exception never permits an approver to clear a blocked request or extend a grant past the admin cap of 30 days.
