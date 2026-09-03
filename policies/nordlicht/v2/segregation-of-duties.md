# Segregation of duties standard

Document id: NL-STD-002
Version: 2.0
Owner: Head of IT Security
Effective date: 2026-03-01
Review cycle: annual
Revision history: 2.0 clarifies wording in the rationale and detection sections; the conflict rules 2.1 through 2.6 are unchanged.

## 1 Rationale and scope

Some combinations of access allow a single person to both commit and conceal a damaging act. No level of trust in an individual removes that risk, and segregation of duties protects the individual as much as the company: a person who cannot approve their own payment cannot be pressured into doing so, and cannot be suspected of it. This standard defines the role combinations that no single person at Nordlicht Maschinenbau GmbH may hold and describes how conflicts are detected and resolved.

This standard is subordinate to the Access Control Policy. It applies to all employees and, through the Third-party and Contractor Access Policy, to all external accounts. The rules in section 2 are cited by other documents and by the access request assistant as SoD-STD 2.1 through SoD-STD 2.6.

## 2 Conflict rules

Each rule names a pair of roles. Holding both roles at once is a conflict. The rules are exhaustive: only the pairs listed here block a request, and any extension of this list requires a new version of this standard approved by the Head of IT Security.

### 2.1 payments.initiator and payments.approver

Initiating and approving the same payment must be two people. A person who holds both roles can create a payment to any account and release it without a second look. This is the most direct fraud path in the company, and it is also the pairing most often requested by accident, typically when a Finance employee covers for an absent colleague. The request is blocked; the correct handling is a temporary delegation of the approver role to another Finance employee who does not initiate payments.

### 2.2 vendor-master.editor and payments.approver

Editing supplier bank data and approving payments enables invoice fraud. A person with both roles can change a supplier's bank account to one they control, approve the next legitimate invoice, and change the account back. Because each individual step looks routine, this pattern is hard to detect after the fact. The pairing is blocked without exception. Procurement employees, who hold vendor-master.editor by template, therefore cannot receive payments.approver, and Finance employees holding payments.approver cannot receive edit rights on vendor-master.

### 2.3 erp.buyer and erp.goods-receipt

Ordering goods and confirming receipt of the same goods hides phantom purchases. A person with both roles can raise a purchase order to a colluding supplier and confirm delivery of goods that never arrived, at which point payment follows automatically. The pairing is blocked. In small teams where the same person handles ordering and warehouse intake, the receipt confirmation must be reassigned to another employee rather than combined.

### 2.4 prod-k8s.deployer and prod-k8s.auditor

Deploying to production and auditing those deployments must be separated. The auditor role exists to give an independent record of what changed on the Production Cluster and when. A person who both deploys and audits can alter or omit the record of their own changes. The pairing is blocked. IT employees who need both functions in the course of their work alternate between the roles across separate, non-overlapping grants, and never hold both at once.

### 2.5 hris.admin and hris.payroll-approver

Administering HR records and approving payroll enables ghost employees. A person with both roles can create an employee record that corresponds to no real person, attach a bank account, and approve the resulting salary payments. The pairing is blocked. HR system administration is held by a small number of HR and IT staff who are excluded from payroll approval for the full duration of their admin grant.

### 2.6 erp.admin and payments.initiator

ERP administrators can alter controls and must not initiate payments. The erp.admin role can change approval workflows, tolerance limits, and posting rules inside the ERP. A person who can weaken those controls must not also be able to feed transactions into them. The pairing is blocked. Because erp.admin is an admin role on a critical system, it is already limited to 30 days under the Access Control Policy; during that window the holder cannot receive payments.initiator, and an existing payments.initiator grant blocks the admin request.

## 3 Detection at request time

Every access request is checked against section 2 before any approval step runs. A conflict exists if the person the request is for already holds the paired role, or has an approved request for the paired role that has not yet been executed. Pending approved requests count because otherwise two conflicting requests submitted in quick succession could both pass. A conflicting request is blocked automatically and the notice cites the specific rule, for example "blocked by SoD-STD 2.2". Approvers cannot override the block. If both roles are genuinely needed in one team, the team requests them for two different people. The access request assistant explains the block and names the rule when it declines such a request.

## 4 Re-check at execution time

Approval and execution of a grant are separate steps, and time can pass between them. The conflict check therefore runs a second time at the moment the grant is executed. If a conflicting role was granted in the interval, execution stops and the request returns to a blocked state with the rule cited. This second check is not a formality; it is the control that catches races the request-time check cannot see.

## 5 Remediation of existing conflicts

Conflicts can predate this standard or arise through error. When the quarterly review described in the Access Review Procedure finds a person holding a conflicting pair, the department manager must resolve it within 10 working days. Resolution means revoking one of the two roles; which one is revoked is the manager's decision, made with the affected system owners. Until the conflict is resolved, the IT security office flags both grants, and neither role can be renewed. A conflict that remains open past the deadline is escalated to the Head of IT Security and the responsible department head, and the less recently used of the two roles is revoked by IT administration.

## 6 Records

Every blocked request, every execution-time stop, and every remediation is recorded with the rule number, the roles involved, and the identities of the requester and the target person. These records are retained for three years and are available to internal audit. Questions about a specific block go to it-security@nordlicht.example and must reference the request number.
