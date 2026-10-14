# Data classification standard

Document id: NL-STD-010
Version: 2.0
Owner: Head of IT Security
Effective date: 2026-03-01
Review cycle: annual
Revision history: 2.0 clarifies wording on labeling and unlabeled data; the classes and handling rules are unchanged.

## 1 Purpose and scope

This standard defines how data at Nordlicht Maschinenbau GmbH is classified and how each class is handled. Every document, dataset, and system record the company creates or receives carries exactly one class. The class decides where the data may live, who may see it, how it moves, and how it dies. The Access Control Policy governs who gets access to the systems; this standard governs what the data inside them requires.

The standard applies to all employees and, through the Third-party and Contractor Access Policy, to external parties handling company data.

## 2 The four classes

### 2.1 Public

Data the company has released or would release without harm. Examples: the product catalogue, published technical specifications, press releases, and job advertisements. Public data carries no handling restrictions beyond accuracy: only Sales, HR, or the managing directors release data as public, because classification as public is a publication decision, not a convenience.

### 2.2 Internal

Data meant for employees but not for the outside world. This is the default class for company data that carries no specific sensitivity. Examples: wiki content, general process documentation, internal announcements, organization charts, and grafana dashboards showing operational metrics. Internal data is visible to anyone with access to the system that holds it, which is why the wiki and grafana, both low sensitivity systems under the Access Control Policy, may hold nothing above this class.

### 2.3 Confidential

Data whose disclosure would cause real damage: lost contracts, lost technical advantage, or legal exposure. Examples: CAD models and drawings in plm, manufacturing parameters and routings in mes, supplier terms and bank details in vendor-master, pricing and margin data in erp and finance-dash, customer contracts in crm, payment data in payments, and source code in gitlab. Confidential data stays in the system that owns it. Extracts are made only for a defined purpose, are shared only with named recipients who hold access to the source system or a documented need, and are deleted when the purpose ends.

### 2.4 Pii

Data relating to an identified or identifiable person: personnel records, salary and payroll data, applicant data, working time and absence records, badge movement logs, and any customer or supplier contact data that identifies an individual. Pii is a class of its own because legal duties attach to it regardless of business sensitivity. Pii is handled under the confidential rules plus the data protection requirements in section 4. The primary stores for employee pii are hris and the badge system; pii does not belong in the wiki, in grafana, in chat messages, or in shared m365 folders with broad membership.

Where data could fall into two classes, the higher-protection class wins, and a dataset containing any pii is handled as pii.

## 3 Handling rules per class

Storage. Public and internal data may live anywhere on company systems. Confidential data lives in the line-of-business system responsible for it, and copies outside that system are exceptions with an owner and an end date. Pii lives in hris, the badge system, or the specific systems approved for it. Private devices and private cloud services hold no company data of any class, as the Acceptable Use Policy states.

Transmission. Internal data moves freely inside company systems. Confidential and pii data leave the company only encrypted, only to recipients under contract, and only through channels IT administration has approved. Sending confidential data to a private email address, the sender's own included, is a violation.

Access. Access to confidential and pii data follows least privilege through the roles in the Access Control Policy, and grants on the systems holding it are reviewed quarterly under the Access Review Procedure. Screens showing confidential or pii data are locked when unattended, and printouts are collected from the printer immediately.

Discussion. Confidential and pii matters are not discussed in open-plan areas, in public places, or in the presence of visitors, per the escort rules of the Physical Access Policy.

## 4 EU processing requirement

The company processes all data in the European Union. Every system in the Access Control Policy inventory, every backup location, and every cloud service the company uses stores and processes data in EU regions only. A new system or service enters the inventory only after IT administration has confirmed EU processing in the contract, and configuration options that would replicate data outside the EU are disabled.

Remote access from outside the EU, for example an employee traveling, is access to data that remains in the EU and is permitted through the VPN; moving copies of confidential or pii data onto devices outside the EU is not. Suppliers and contractors processing company data are bound to EU processing by contract before access is granted, as the Third-party and Contractor Access Policy requires.

## 5 Labeling

New documents state their class on the first page or in the file properties: public, internal, confidential, or pii. Systems that hold a single class throughout, such as hris for pii, count as labeled at the system level, and their exports inherit the label. Unlabeled data is treated as internal at minimum, and as confidential when it plausibly came from a confidential source. When in doubt about the origin, ask the likely owning department before sharing. Mislabeling data downward to ease sharing is a violation of this standard.

## 6 Disposal

Data is deleted when its purpose has ended and no retention law requires keeping it. Commercial and tax retention periods are tracked by Finance; personnel record retention is tracked by HR. Disposal is class-appropriate: confidential and pii data are deleted from the system and its backups within the backup rotation period, paper in these classes goes into the locked shredding containers, and storage media leaving the company are wiped or destroyed by IT administration with a recorded certificate. Leaver data handling follows the Joiner, Mover, Leaver Procedure; the leaver's personal working files are deleted 90 days after the leaving date unless their manager has claimed specific files for a documented business purpose.
