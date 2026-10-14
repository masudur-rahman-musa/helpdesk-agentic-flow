# Access Control Policy

Document id: CU-POL-001
Version: 2.0
Owner: Head of Information Security
Effective date: 2026-02-02
Review cycle: annual
Revision history: 2.0 restructures the policy around the life of a request, adds a worked classification example, an exceptions process section, and evidence retention detail. No classification rule, approval chain, or expiry limit changed from 1.0.

## 1. Why this policy exists

Cuan Financial Services Ltd is a regulated payments and lending firm. For any account on any of our systems, we must be able to show who approved it, for what reason, and when it ends. That evidence starts here. This policy sets out the rules that govern how employees and contractors gain, keep, and lose access to the firm's information systems.

The policy binds every person who holds a Cuan account and every route by which access can be asked for: the access request assistant, the IT helpdesk portal, or a direct approach to the information security office. It covers every system the firm runs or subscribes to. Companion documents fill in specific areas: the Segregation of Duties Standard (CU-POL-002), the Joiner, Mover, Leaver Procedure (CU-POL-003), the Privileged Access Standard (CU-POL-004), and the Access Review Procedure (CU-POL-005).

## 2. The life of a request

An access request moves through five stages: it is submitted, screened for blocks, classified into a risk class, approved by the chain that class requires, and provisioned with an expiry. Sections 3 to 8 follow that order. A request states five things: the target system, the role wanted, the person the access is for, the duration asked for, and the business reason. A manager may submit on behalf of a team member; such a request is treated exactly as if the team member had submitted it, apart from the self-approval and self-requested admin rules in section 7.

## 3. System sensitivity tiers

The information security office keeps an inventory of every system and assigns each one a sensitivity tier when it is onboarded. Four tiers exist.

Critical: systems holding customer money, customer identity data, or the means to change either. These are Core Banking (core-banking), Payments Gateway (payments-gateway), KYC (kyc), the HR System (hris), and Production AWS (prod-aws).

High: systems holding financial records or aggregated customer data. These are Ledger (ledger) and Data Warehouse (data-warehouse).

Medium: systems supporting daily work with limited customer or internal data: crm, support-desk, vpn, github, and m365.

Low: systems holding operational or public material: datadog and wiki.

A system that has not yet been assigned a tier is handled as critical until the information security office classifies it. The safe assumption is the strict one.

## 4. Standard and non-standard requests

Every department has a role template, defined in the Joiner, Mover, Leaver Procedure: the set of system roles a member of that department is expected to hold for ordinary work. A request is standard when the role sits in the template of the requester's department, and non-standard when it does not. A non-standard request always carries a stated business reason, because nothing outside the template is presumed necessary.

## 5. Screening for blocks

Before classification, two checks can stop a request outright.

Segregation of duties. A request that would give one person both roles of a pair listed in the Segregation of Duties Standard is refused, and the refusal names the rule breached, for example SoD-STD 2.2. A pairing counts as complete when the person already holds the other role, or holds an approved request for it that has not yet been executed.

Offboarding status. A request for a person who is offboarding or has left the firm is refused at every risk class. There is no approval path around this; see the Joiner, Mover, Leaver Procedure.

No approver can override either block. A blocked request is not queued or escalated; it can only be resubmitted after the underlying conflict or status changes.

## 6. Risk classification

Each surviving request is placed in one of four risk classes: low, elevated, privileged, or critical. The rules below are applied in order, and the highest class that matches wins.

A role on a critical tier system is always classified critical, whatever the privilege level and whether or not the role is in a template. A request for admin privilege on any system is classified at least privileged. A role outside the requester's department template classifies as privileged on a high tier system and elevated on a low or medium tier system. A template role on a low or medium tier system classifies as low. A template role on a high tier system classifies as low for read privilege and elevated for write privilege.

One adjustment follows classification. When the person the access is for has been employed for fewer than 30 days, a request that came out low is raised to elevated. The Joiner, Mover, Leaver Procedure explains the reasoning.

### 6.1 A worked example

An Operations employee of two years asks for ledger.viewer for 200 days. Ledger is a high tier system; ledger.viewer is in the Operations template and carries read privilege, so the class is low and the grant auto-approves. The read default of 365 days and the low cap of 365 days both exceed the 200 days asked for, so the grant expires after 200 days. Had the same person asked for data-warehouse.analyst, a role absent from the Operations template on a high tier system, the class would be privileged, needing the department manager and the Data Warehouse owner, with expiry at the 90 day cap even though 200 days was requested.

## 7. Approval chains

Low class requests auto-approve with no human involved; the grant is recorded together with its classification.

Elevated class requests need the department manager of the person the access is for.

Privileged class requests need two approvals: that department manager and the owner of the target system.

Critical class requests need those two plus a third approver. The third approver is IT administration for production and infrastructure systems, and HR for the HR System.

Two overriding rules sit above every chain. First, no manager approves a request made for themselves. Where the approving manager is also the person the access is for, approval passes upward to the system owner, and where the system owner is the requester, to IT administration. Second, a request a person submits for admin access for themselves never auto-approves and always requires one approval level more than the class alone would demand. The Privileged Access Standard sets out both rules fully.

## 8. Expiry of grants

Every grant ends. No access at Cuan is permanent, and the Access Review Procedure exists to catch anything that outlives its purpose despite expiry.

Three numbers determine the end date. The privilege default: 365 days for read roles, 180 for write, 90 for admin. The class cap: 365 days for low, 180 for elevated, 90 for privileged, 30 for critical. And the duration the requester asked for. The effective expiry is the smallest of the three. Admin privilege additionally never exceeds 30 days, whatever the class. A request asking for more than the cap permits is not refused for that reason; it proceeds at the cap and the over-ask is noted on the record.

## 9. Exceptions and dispensations

An exception to this policy is granted only in writing by the Head of Information Security, carries a stated end date, and is entered in the exceptions log kept by the information security office. Each open exception is re-examined at the quarterly review that follows its grant. Two things no exception can ever do: permit an approver to clear a blocked request, or extend any grant beyond the 30 day admin cap. A dispensation request that asks for either is refused without escalation.

## 10. Records and regulator evidence

Every submission, block, classification, approval, refusal, grant, and revocation is written to the audit record with a timestamp and the identity of each acting person. The information security office retains these records for the period our regulator requires and assembles them into the evidence packs produced at supervisory inspection. The same records feed the quarterly reviews under the Access Review Procedure. A gap in this record is treated as a control failure and investigated, whether or not the underlying grant was proper.
