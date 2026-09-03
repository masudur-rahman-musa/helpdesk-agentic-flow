# Privileged access standard

Document id: NL-STD-004
Version: 2.0
Owner: Head of IT Security
Effective date: 2026-03-01
Review cycle: annual
Revision history: 2.0 clarifies wording on break-glass use and session logging; duration limits and approver chains are unchanged.

## 1 Purpose and scope

This standard governs privileged and critical access at Nordlicht Maschinenbau GmbH. It supplements the Access Control Policy, which defines the risk classes; this document adds the controls that apply once a request falls into the privileged or critical class. It applies to employees and, with the further restrictions of the Third-party and Contractor Access Policy, to external accounts.

## 2 What counts as privileged here

Two kinds of access fall under this standard.

First, every role with admin privilege. In the current role catalogue these are erp.admin, hris.admin, and gitlab.maintainer. Admin roles can change a system's configuration, its users, or its permission structure, which means they can change the controls this standard relies on.

Second, every role on a critical system, whatever its privilege level. The critical systems are erp (ERP), payments (Payment Release), hris (HR System), and prod-k8s (Production Cluster). Even read access matters on these systems: erp.viewer sees company-wide financial data, hris.viewer sees personnel records, and prod-k8s.auditor sees the configuration of the production environment. Write roles such as payments.initiator, payments.approver, erp.buyer, erp.goods-receipt, hris.payroll-approver, and prod-k8s.deployer can move money, alter records, or change what runs in production.

Requests outside a department template on high sensitivity systems are also classified privileged under the Access Control Policy and are approved under section 4 of this standard.

## 3 Duration limits

Admin roles default to 90 days but are capped at 30 days in every case, regardless of the risk class of the request. Any role on a critical system is likewise capped at 30 days through the critical class cap. There are no standing admin grants at this company; an administrator who needs the role continuously renews it monthly, and each renewal is a fresh request with a fresh approval chain. This is deliberate. The renewal is the moment an approver can notice that the need has ended.

The effective expiry follows the general rule in the Access Control Policy: the minimum of the requested duration, the role default, and the cap. A request for a 90-day erp.admin grant proceeds as a 30-day grant and the over-ask is recorded.

## 4 Approver chain

Privileged requests require the department manager of the target person and the owner of the target system. Critical requests require both of those plus a third approver: IT administration for production and infrastructure systems (erp, payments, prod-k8s), or HR for the HR system (hris).

Approvers act in sequence, and each sees the justification, the duration, and the result of the segregation-of-duties check. A grant blocked under the Segregation of Duties Standard, for example a prod-k8s.deployer request from someone holding prod-k8s.auditor (SoD-STD 2.4), cannot be approved at any level.

Two rules tighten the chain further. A request in which a person asks for admin access for themselves never completes automatically and escalates one approval level beyond its class. And a manager cannot approve their own request; the approval falls to the next role in the chain, first the system owner, then IT administration. The combined effect is that no single person, at any seniority, can put admin access into their own hands.

## 5 Break-glass procedure

An emergency can require access faster than the approval chain can deliver it, for example a production outage at night where the on-call engineer needs prod-k8s.deployer immediately. For this case the company maintains break-glass accounts for prod-k8s and erp, held in sealed credentials by IT administration.

Break-glass use follows four steps. The on-call engineer contacts the IT administration on-call, who releases the credential and records the time, the person, and the stated reason. The credential grants access for a maximum of 24 hours. Within one working day, the person who used it writes a short factual account of what was done and why the normal process was too slow. Within five working days, the system owner and the Head of IT Security review the account against the session logs and either confirm the use as justified or open an incident.

Every break-glass use rotates the credential afterward, whether or not the use was justified. Break-glass is for emergencies with active damage or imminent risk; using it to bypass a pending approval is a policy violation. When in doubt whether a situation qualifies, the IT administration on-call makes the call and records the reasoning.

## 6 Session logging

All sessions under privileged or critical roles are logged. For prod-k8s, commands executed against the Production Cluster are recorded with timestamps and the acting account. For erp, hris, and payments, the systems' change and approval logs are retained and exported to the central log store daily. Logs are kept for one year, are protected from modification by the accounts they describe, and are readable by the IT security office and internal audit.

Administrators should assume their privileged sessions are reviewed, because they are: the IT security office samples privileged sessions monthly and reviews every session flagged by the quarterly review under the Access Review Procedure.

## 7 Account hygiene for privileged roles

Privileged roles are bound to personal accounts. Shared administrative accounts are prohibited outside the sealed break-glass credentials in section 5. Privileged sessions require multi-factor authentication as set out in the Password and Account Recovery Procedure, and administrators use their privileged role only for tasks that need it, performing routine work under their standard access.

## 8 Revocation

A privileged or critical grant is revoked immediately when the holder moves departments, begins offboarding, or no longer performs the task the grant was approved for. The holder and their manager are both responsible for reporting the end of need; waiting for the 30-day expiry when the need has already ended is a violation of this standard.
