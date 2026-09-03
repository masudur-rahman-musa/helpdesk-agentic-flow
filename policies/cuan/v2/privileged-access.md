# Privileged Access Standard

Document id: CU-POL-004
Version: 2.0
Owner: Head of Information Security
Effective date: 2026-02-09
Review cycle: annual
Revision history: 2.0 rewords the break-glass and renewal sections and adds a short paragraph on renewal patterns; approval chains, caps, and the break-glass steps are unchanged from 1.0.

## 1. Purpose and scope

This standard sets the extra controls that apply to privileged and critical access at Cuan Financial Services Ltd. It supplements the Access Control Policy (CU-POL-001), which defines the risk classes; this document governs how the two highest classes are approved, bounded in time, used, and reviewed. It applies to employees and contractors, noting that contractors face further restrictions under the Third-party and Contractor Access Policy (CU-POL-009).

## 2. What counts as privileged or critical here

Two kinds of access fall under this standard.

First, admin roles on any system. The firm's admin roles are core-banking.admin, support-desk.supervisor, hris.admin, and github.maintainer. Admin privilege allows configuration changes, user management, or control changes on the system, which is why any request for admin privilege classifies as at least privileged regardless of the system's tier.

Second, any role on a critical system. The critical systems are Core Banking (core-banking), Payments Gateway (payments-gateway), KYC (kyc), the HR System (hris), and Production AWS (prod-aws). Even a read role such as prod-aws.auditor or core-banking.viewer classifies as critical, because these systems hold customer money, customer identity data, or the means to alter either, and every account on them widens the surface an attacker or an insider can use.

Roles such as payments-gateway.initiator, payments-gateway.approver, kyc.analyst, kyc.approver, hris.payroll-approver, and prod-aws.deployer are write roles on critical systems and therefore classify as critical requests.

## 3. Approval chain

Privileged requests require the department manager of the person the access is for and the owner of the target system. Critical requests require both of those plus a third approver: IT administration for production and infrastructure systems, or HR for the HR System. Approvals are collected in order and each approver sees the business reason, the requested duration, and the classification.

Two escalation rules apply on top of the chain.

A manager cannot approve their own request. When the person the access is for is also the department manager who would approve, the approval moves up: the system owner approves in the manager's place, and if the system owner is the requester, IT administration approves.

A self-requested admin role never auto-approves under any circumstances and escalates one approval level beyond what the class alone requires. In practice a person asking for admin access for themselves should expect at least the manager, the system owner, and where the class already required those, IT administration as well. The rule exists because self-granted administration is the most common route to unreviewed standing power.

Any request under this standard is still subject to the Segregation of Duties Standard (CU-POL-002). Rules SoD-STD 2.3 and SoD-STD 2.5 specifically involve admin roles; a request completing either pair is blocked before it reaches an approver.

## 4. Time limits

Admin privilege is capped at 30 days per grant, regardless of risk class and regardless of the role's 90-day default. Critical class requests are likewise capped at 30 days. Privileged class requests are capped at 90 days. The effective expiry is the minimum of the requested duration, the privilege default, and the cap, as set in the Access Control Policy; a request that over-asks proceeds at the cap and the over-ask is recorded.

There is no standing admin access at Cuan. A person whose duties need admin rights continuously renews through a fresh request each period, which puts a fresh approval and a fresh business reason on record each time. Renewal requests receive no shortened treatment; the full chain in section 3 applies every time.

The renewal pattern itself is information. A role renewed period after period without a duty that explains it is exactly what the quarterly review in section 8 exists to question, so approvers of a renewal should read the previous reasons, not only the current one.

## 5. Break-glass procedure

An incident can demand privileged access faster than the approval chain can move, for example a night-time production outage in Payments Gateway or a suspected compromise in Production AWS needing immediate containment.

For these cases IT administration holds sealed break-glass credentials for each critical production system. Using them requires all of the following. The user declares an incident in the IT helpdesk portal before or, where the portal itself is affected, immediately after use, naming the system, the reason, and the expected actions. The credentials grant a session, not a standing role, and the session ends when the incident ends or after 12 hours, whichever is first. The seal state of every break-glass credential is checked weekly by the information security office.

Every break-glass use triggers an after-the-fact review within two business days. The reviewer is the system owner together with the Head of Information Security; neither may review a use they performed. The review confirms the incident was genuine, the actions taken were proportionate, and no change made under break-glass remains in place without a normal approval. The credential is rotated after every use. A break-glass use that fails review is treated as unauthorised access and handled under the disciplinary process referenced in the Acceptable Use Policy (CU-POL-006).

## 6. Session logging

All privileged and critical access is logged at the session level, not only at grant level. For Production AWS, administrative sessions are recorded with the commands issued. For Core Banking, Payments Gateway, KYC, and the HR System, the systems' own audit trails capture every configuration change, user management action, and approval action with the acting account and timestamp. Logs are shipped to central monitoring, are not writable by the accounts they record, and are retained under the Data Classification Standard (CU-POL-010).

The prod-aws.auditor role exists to review these logs. Under SoD-STD 2.4 the auditor must not also hold prod-aws.deployer, so log review is always independent of deployment.

Privileged users must not disable, pause, or reroute session logging. Discovering that logging was off during a privileged session is reported to the information security office the same day and the session's actions are reconstructed and reviewed.

## 7. Conduct of privileged sessions

Privileged access is used only for the purpose stated in the request. Admin sessions are opened from firm-managed devices over the VPN, are not left unattended, and are closed when the task ends. Credentials for privileged accounts are never shared, cached in scripts, or stored outside the firm's password manager; the Password and Account Recovery Procedure (CU-POL-007) applies with no exceptions for administrators. Bulk data export through a privileged session follows the handling rules of the Data Classification Standard for the classes involved.

## 8. Review

Privileged and critical grants receive first attention in the quarterly reviews under the Access Review Procedure (CU-POL-005). Because these grants expire within 30 to 90 days, most will have lapsed between reviews; the review checks the pattern of renewals and challenges any role renewed repeatedly without a duty that explains it.
