# Access control policy

Document id: NL-POL-001
Version: 1.0
Owner: Head of IT Security
Effective date: 2025-02-01
Review cycle: annual

## 1 Purpose and scope

This policy governs how employees of Nordlicht Maschinenbau GmbH obtain, keep, and lose access to company IT systems. It applies to all employees, all departments, and every system operated by or on behalf of the company. Contractors and other external parties are covered by the Third-party and Contractor Access Policy in addition to this document.

Access is granted on the principle of least privilege. An employee receives the access their role requires and nothing more. Every grant has a named owner, a documented approval, and an expiry date. All requests are submitted through the IT helpdesk portal or the access request assistant; requests made by email, chat, or in person are not valid and will not be processed.

## 2 System sensitivity tiers

Every system is assigned to one of four sensitivity tiers. The tier of a system determines how requests for it are classified and approved.

Critical systems are those where misuse can cause direct financial loss, harm to employees, or a production outage. The critical systems are erp (ERP), payments (Payment Release), hris (HR System), and prod-k8s (Production Cluster).

High sensitivity systems hold valuable engineering, manufacturing, or supplier data, or control entry to company premises. The high systems are plm (CAD vault), mes (Manufacturing Execution), finance-dash, vendor-master, and badge (Physical Access).

Medium sensitivity systems hold internal business data with limited damage potential. The medium systems are crm, vpn, gitlab, and m365.

Low sensitivity systems hold broadly shared internal information. The low systems are wiki and grafana.

The Head of IT Security maintains this list. A new system must be assigned a tier before any access to it is granted.

## 3 Standard and non-standard access

Each department has a role template. The template lists the roles a member of that department receives automatically when they join, as described in the Joiner, Mover, Leaver Procedure. The templates are:

Engineering: plm.engineer, gitlab.developer, grafana.viewer, vpn.user, m365.user, wiki.editor, badge.holder.
Production: mes.operator, erp.viewer, vpn.user, m365.user, wiki.viewer, badge.holder.
Procurement: erp.buyer, vendor-master.editor, crm.viewer, vpn.user, m365.user, wiki.editor, badge.holder.
Finance: finance-dash.viewer, erp.viewer, payments.initiator, vpn.user, m365.user, wiki.viewer, badge.holder.
Sales: crm.editor, erp.viewer, vpn.user, m365.user, wiki.viewer, badge.holder.
HR: hris.viewer, vpn.user, m365.user, wiki.viewer, badge.holder.
IT: gitlab.developer, grafana.viewer, vpn.user, m365.user, wiki.editor, badge.holder.

A request for a role that is in the requester's department template is standard access. A request for any other role is non-standard access. Non-standard access is legitimate, for example when a Finance employee needs read access to the CAD vault for a costing project, but it always receives closer review than a template role.

## 4 Risk classification of requests

Every access request is classified into one of four risk classes: low, elevated, privileged, or critical. Classification follows these rules, applied in order:

Any role on a critical system (erp, payments, hris, prod-k8s) is classified critical, regardless of template membership or privilege level.

Any role that carries admin privilege is classified privileged at minimum. On a critical system the previous rule applies and the request is critical.

A request outside the requester's department template is classified elevated if the system is low or medium sensitivity, and privileged if the system is high sensitivity.

A request inside the template is classified low if the system is low or medium sensitivity. On a high sensitivity system, a template role with read privilege is low and a template role with write privilege is elevated.

If the person the access is for has been employed for fewer than 30 days, a request that would otherwise be low is lifted to elevated. This rule exists because new joiners are the most common vehicle for social engineering and for errors in role assignment.

## 5 Approvals

The risk class of a request determines who must approve it.

Low requests are granted automatically. No human approver is involved, and the grant is recorded with its classification.

Elevated requests require approval by the department manager of the person receiving the access.

Privileged requests require approval by the department manager and by the owner of the target system.

Critical requests require approval by the department manager, the system owner, and a third approver: IT administration for production and infrastructure systems, or HR for the HR system.

Approvers act in sequence. A request that any approver rejects is closed and the requester is informed of the reason. Approvals are recorded and are visible in the quarterly review described in the Access Review Procedure.

## 6 Special approval rules

A manager cannot approve a request that they submitted for themselves. In that case the next approver in the chain acts instead: first the system owner, then IT administration.

A request in which a person asks for admin access for themselves never completes automatically and always escalates one approval level beyond what its risk class would normally require. Details are in the Privileged Access Standard.

## 7 Expiry

Every grant expires. The default expiry depends on the privilege of the granted role: read roles expire after 365 days, write roles after 180 days, and admin roles after 90 days.

Each risk class also carries a cap: low 365 days, elevated 180 days, privileged 90 days, critical 30 days. Admin privilege is capped at 30 days regardless of risk class.

The effective expiry of a grant is the minimum of the requested duration, the role default, and the applicable cap. A request that asks for more than the cap is not rejected for that reason; it proceeds at the cap and the over-ask is recorded on the request. Renewal before expiry is a new request and is classified again from scratch.

## 8 Blocked requests

Some requests are blocked outright and no approval can override the block through the normal process.

A request is blocked if granting it would create a segregation-of-duties conflict as defined in the Segregation of Duties Standard. The blocking notice cites the specific rule, for example SoD-STD 2.1. A conflict exists when the target person already holds the paired role or has an approved request for it that has not yet been executed.

A request is blocked if the person it is for is in offboarding or has already left the company. The Joiner, Mover, Leaver Procedure defines when offboarding begins.

## 9 Responsibilities

Employees request only the access they need and report access they no longer need. Department managers approve within the deadlines set by the IT helpdesk portal and certify their team's access quarterly. System owners decide on privileged and critical requests for their systems and keep role definitions current. The IT security office operates the request process, maintains the sensitivity tiers, and audits grants.

## 10 Exceptions and enforcement

Exceptions to this policy require a written, time-limited approval by the Head of IT Security and are reviewed at each quarterly access review. Circumventing the access request process, sharing credentials, or using access for a purpose other than the one it was granted for is a violation of this policy and is handled under the disciplinary provisions of the employment contract. Suspected violations are reported to it-security@nordlicht.example.
