# Data Classification Standard

Document id: CU-POL-010
Version: 2.0
Owner: Head of Information Security
Effective date: 2026-03-02
Review cycle: annual
Revision history: 2.0 adds a short paragraph on classifying new datasets and rewords the residency section for clarity; classes, handling rules, and the residency rule are unchanged from 1.0.

## 1. Purpose and scope

This standard defines how Cuan Financial Services Ltd classifies its data and what each class requires in handling, storage, transfer, and disposal. It applies to all data the firm creates, receives, or processes, in any system and any format, including paper. Every employee and contractor applies it; system owners apply it when configuring systems; and the Access Control Policy (CU-POL-001) relies on it, since a system's sensitivity tier reflects the classes of data it holds.

## 2. The four classes

The firm uses four classes: public, internal, confidential, and pii. Where one document or dataset contains data of several classes, the whole is handled at the highest class present.

### 2.1 Public

Public data is approved for release outside the firm. Examples: published product terms and fee schedules, marketing pages, job advertisements, and wiki pages that have been explicitly cleared for external sharing. Approval for release comes from the department head and, for anything touching regulated products, Risk & Compliance. Data is not public merely because it is uninteresting; it is public when someone approved its release.

### 2.2 Internal

Internal data is ordinary business information whose disclosure would embarrass or mildly disadvantage the firm but not harm customers. Examples: most wiki content, internal procedures, project plans, m365 documents without customer or personnel data, datadog dashboards and metrics, and non-customer github repositories. Internal is the default class for firm data that carries no label.

### 2.3 Confidential

Confidential data would damage the firm, a customer, or a counterparty if disclosed, but does not identify natural persons on its own. Examples: ledger balances and journals in the Ledger, aggregated financial and risk reporting from the Data Warehouse, unpublished financial results, credit models and their parameters, security architecture and incident records, contract terms with providers, and source code for payment processing in github. The registers and evidence packs kept for our regulator are confidential.

### 2.4 Pii

Pii is data that identifies or can identify a natural person: customers, employees, applicants, or third-party contacts. Examples: customer records in Core Banking and the crm, payment sender and recipient details in the Payments Gateway, customer due diligence files and screening results in KYC, support tickets containing customer details in support-desk, employee records and payroll data in the HR System, and CCTV footage under the Physical Access Policy (CU-POL-008). Pii carries the obligations of data protection law in addition to this standard, and customer due diligence records carry retention duties from our regulatory obligations.

## 3. Handling rules per class

Public data has no handling restrictions beyond accuracy and the release approval above.

Internal data stays within the firm and its contracted providers. It is shared with third parties only under a confidentiality agreement, per the Third-party and Contractor Access Policy (CU-POL-009). It travels on firm systems, not personal accounts, as the Acceptable Use Policy (CU-POL-006) requires.

Confidential data is shared inside the firm on need only, and outside the firm only with Risk & Compliance sign-off and contract terms in place. It is encrypted in transit always and at rest wherever the storing system supports it. It is not placed on the wiki, in support-desk tickets, or in datadog, which are not approved stores for this class. Extracts from the Ledger or the Data Warehouse for analysis are deleted when the task ends.

Pii carries every confidential rule plus the following. Access follows the roles in the Access Control Policy; looking up a person without a task need is prohibited under the Acceptable Use Policy. Pii is not copied into lower-tier systems: no customer details in the wiki, in datadog, in commit messages or code in github, or in m365 files shared firm-wide. Test and development environments use generated data, never production pii. Disclosure of pii to anyone outside the firm, beyond processors under contract, goes through Risk & Compliance. Suspected pii exposure is reported to the information security office at infosec@cuan.example the same day it is discovered.

## 4. Residency

The firm applies one residency rule. Public and internal data may be stored with and processed by service providers outside the EU. Confidential and pii data stay within the EU without exception to the rule's scope: storage, processing, backup, and support access all remain in EU locations.

In practice this means a provider is engaged for confidential or pii data only when its contract fixes EU data locations and restricts remote access from outside the EU, and the outsourcing register entry under the Third-party and Contractor Access Policy records this. The rule also binds internal choices: an engineer moving Data Warehouse extracts or KYC files to a non-EU region of Production AWS breaches this standard even though the provider is contracted. System owners configure allowed regions so the wrong choice fails rather than depending on memory. Exceptions require the Head of Information Security and Risk & Compliance together, in writing, with a stated end date; no exception is available for KYC files or payment data.

## 5. Labeling

Documents and datasets carry their class where the format allows: a footer label on documents, a classification field in m365, and a repository-level label in github. Systems that hold a single class throughout, such as KYC for pii, are labeled at system level in the inventory kept under the Access Control Policy, and everything within them inherits that class. The author of a document assigns the label at creation; the default for unlabeled firm data is internal, which is why confidential and pii material must be labeled actively. Mislabeling discovered later is corrected by the data owner and, where data was mishandled as a result, reported as an incident.

A new dataset is classified before it is populated, not after. Whoever creates a report, an extract, or a system integration decides the class of what it will contain first, and asks the information security office when the answer is unclear. Classifying after the fact means the data has already travelled under the wrong rules.

## 6. Retention and disposal

Data is kept as long as a business or regulatory duty requires and then destroyed. Customer due diligence and transaction records follow the retention periods our regulatory obligations set. Access and audit records under the Access Review Procedure (CU-POL-005) and the Password and Account Recovery Procedure (CU-POL-007) are retained for six years. CCTV is retained 90 days.

Disposal is destruction, not deletion by convenience. Electronic media leaving firm control are wiped with verified tooling or physically destroyed by a contracted provider that issues destruction certificates; the certificates are retained. Paper carrying confidential or pii data goes in the locked shredding consoles, never in ordinary waste or recycling. Decommissioned systems are wiped before disposal or return to a lessor, and the wipe is recorded in the inventory.
