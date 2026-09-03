# Segregation of Duties Standard

Document id: CU-POL-002
Version: 1.0
Owner: Head of Information Security
Effective date: 2025-01-06
Review cycle: annual

## 1. Rationale

Cuan Financial Services Ltd moves customer money and holds customer identity records. In a regulated payments firm the basic control against internal fraud and against honest mistakes is the four-eyes principle: no single person may both create a sensitive action and approve it, or both change a record and audit the change. Our regulator expects this separation to be designed into system roles, not policed after the fact.

This standard defines the role pairings no one person may hold at the same time. The pairings are enforced automatically when access is requested and again when a grant is executed, as described in section 3. The Access Control Policy (CU-POL-001) refers to these rules when it blocks a request, and other documents cite them by section number, for example SoD-STD 2.3.

The standard applies to employees, contractors, and any other account holder. It applies regardless of seniority. A department head is subject to the same pairings as a new joiner.

## 2. Conflicting role pairs

A person is in conflict when they hold both roles of a pair, or hold one role and have an approved but not yet executed request for the other. The six rules follow.

### 2.1 kyc.analyst and kyc.approver

The KYC analyst prepares a customer due diligence case: collecting documents, running screening, and writing the assessment. The KYC approver signs the case off. The analyst who prepares a case must not approve it. If one person could do both, a fabricated or negligently assembled customer file would pass without any independent check, and the firm could onboard a customer it should have refused. Any request that would give one person both roles is blocked and the refusal cites this rule.

### 2.2 payments-gateway.initiator and payments-gateway.approver

The payment initiator enters a payment into the Payments Gateway. The payment approver releases it. Initiating and approving the same payment must be two people. Combining the roles lets one person move firm or customer funds to any destination alone, which is the single largest fraud exposure the firm has. Requests combining these roles are blocked without exception.

### 2.3 hris.admin and hris.payroll-approver

The HR System administrator can create and edit employee records. The payroll approver releases payroll runs. One person holding both can create a fictitious employee record and approve salary payments to it: the classic ghost employee scheme. The roles are held by different people, and a request that would combine them is blocked.

### 2.4 prod-aws.deployer and prod-aws.auditor

The production deployer ships changes to Production AWS. The production auditor reviews deployment records and access logs for exactly those changes. Deploying to production and auditing those deployments must be separated, or the audit trail is reviewed by the person it is meant to check. A combined holder could deploy an unauthorised change and then sign off their own log review. Requests combining these roles are blocked.

### 2.5 core-banking.admin and payments-gateway.approver

The Core Banking administrator can alter accounts, limits, and the control settings of the core system. Core banking administrators must not also approve payments. The combination lets one person weaken a control in Core Banking, for example a limit or a hold, and then approve the payment that exploits the weakened control. This pairing crosses two systems; the check applies across systems exactly as within one. Requests are blocked.

### 2.6 ledger.editor and payments-gateway.initiator

The ledger editor posts and corrects entries in the Ledger. The payment initiator creates outbound payments. Editing the ledger and initiating payments lets one person misdirect funds and then adjust the accounting record that would have exposed the misdirection. The two roles are kept apart, and requests combining them are blocked.

## 3. Detection

Conflicts are detected at two points.

At request time, every access request is checked against the requester's or target person's current roles and their approved pending requests. A request that would complete any pair in section 2 is blocked before it reaches an approver, and the block names the rule. Approvers cannot override the block; this is stated in the Access Control Policy.

At execution time, the check runs again immediately before a grant is provisioned. The gap between approval and execution can be hours or days, and roles can change in that window: a mover may have gained a template role, or another approved request may have executed first. A grant that passed at request time but conflicts at execution time is not provisioned. It is returned to the information security office with the rule cited, and the requester is notified.

The two-stage check exists because an approved but unexecuted request is itself treated as held access for conflict purposes. This closes the window in which a person could file two individually clean requests that together form a pair.

## 4. Remediation of existing conflicts

Conflicts can pre-date this standard or arise through department transfers. When a conflict is found, whether by the automated check, a quarterly review under the Access Review Procedure (CU-POL-005), or any other route, the following applies.

The department manager is notified within one business day. Within five business days the person must give up one role of the pair. The manager chooses which role is retained, based on the person's current duties, and requests revocation of the other through the IT helpdesk portal. If no choice is made within five business days, the information security office revokes the more privileged role of the pair, or if both carry the same privilege, the more recently granted one.

While a conflict stands, any action that requires both roles together is suspended for that person. The conflict, the decision, and the revocation are recorded and retained as inspection evidence.

## 5. No compensating exceptions

Some firms permit segregation conflicts with compensating controls such as extra logging. Cuan does not. The firm is large enough at roughly 180 staff to keep every pair in section 2 separated, and no exception to this standard is granted. A team that believes a pairing is unworkable raises it with the Head of Information Security, who may amend this standard through its review cycle, but may not waive a rule for an individual.

## 6. Related documents

The Access Control Policy defines how blocked requests are handled. The Access Review Procedure defines how conflicts found during quarterly review are treated. The Privileged Access Standard governs the admin roles named in rules 2.3 and 2.5. The Joiner, Mover, Leaver Procedure requires a conflict check on every department transfer.
