# Third-party and Contractor Access Policy

Document id: CU-POL-009
Version: 1.0
Owner: Head of Information Security
Effective date: 2025-02-03
Review cycle: annual

## 1. Purpose and scope

This policy governs access to the systems of Cuan Financial Services Ltd by people who are not employees: contractors, consultants, staff of outsourced service providers, external auditors, and vendor support engineers. It supplements the Access Control Policy (CU-POL-001), which applies to third parties in full, and adds the restrictions in sections 4 to 7. Physical entry by third parties is covered by the Physical Access Policy (CU-POL-008).

As a regulated payments and lending firm, Cuan remains responsible to our regulator for work it outsources. Access granted to a third party is access the firm answers for, so third-party access is narrower, shorter, and more heavily sponsored than employee access.

## 2. Sponsorship

Every third-party account has a named internal sponsor: an employee at manager level or above in the department the work is for. The sponsor requests the account, approves its purpose, answers for its use, confirms its continued need at every quarterly review under the Access Review Procedure (CU-POL-005), and triggers its closure at contract end. A third-party account whose sponsor leaves the firm or moves department is suspended within five business days unless a new sponsor is registered. No sponsor, no account.

Sponsorship is individual. A sponsor may cover several accounts for one engagement but signs for each named person separately. Sponsoring an account for a person the sponsor cannot identify is a breach of this policy.

## 3. Before access is granted

Access for a third party is requested only after the engagement has a signed contract with confidentiality and data protection terms reviewed by Risk & Compliance, and after the provider's relevant staff are named individually. Where the engagement is an outsourcing arrangement under our regulatory obligations, the arrangement is entered in the firm's outsourcing register before any account is created, with the register entry recording the provider, the service, the systems accessed, the data classes involved per the Data Classification Standard (CU-POL-010), and the responsible executive. The information security office checks the register entry exists before provisioning.

## 4. Contractor accounts

Third parties receive individual named accounts in the form firstname.lastname@cuan.example, marked as contractor accounts in the directory with the sponsor and the contract end date recorded. Shared accounts for a provider are not created; every acting person is individually identifiable, which the Segregation of Duties Standard (CU-POL-002) and our audit obligations both require.

Contractor accounts are not attached to department role templates. Every role a contractor needs is requested individually and therefore classifies as non-standard under the Access Control Policy, which means nothing grants automatically: the minimum is elevated class with sponsor approval as the department manager approval, and roles on high sensitivity systems or critical systems classify as privileged or critical with the full approval chains of the Privileged Access Standard (CU-POL-004).

Contractors use firm-managed devices or, by written exception from the information security office, provider devices meeting the firm's endpoint requirements. The Password and Account Recovery Procedure (CU-POL-007) applies to contractor accounts without variation, including MFA and the recovery verification steps, with the sponsor standing in wherever that procedure requires a manager acknowledgement.

## 5. Shorter expiries

Contractor grants run shorter than the employee defaults in the Access Control Policy. Whatever the role's privilege, a contractor grant expires at the earliest of: 90 days, the class cap that would apply to an employee, and the recorded contract end date. Renewals are full requests with the sponsor's fresh approval; there is no rolling extension. A contract extension does not extend grants by itself; the new end date is recorded and each grant is renewed against it.

## 6. No admin without exception approval

Contractors do not hold admin privilege. A request for any admin role for a contractor, such as github.maintainer for an external build engineer, is refused by default and proceeds only with a written exception from the Head of Information Security, in addition to the full approval chain and the escalations the Privileged Access Standard imposes on admin requests. The exception states the task, the named person, and an end date, and the grant is capped at 30 days as all admin grants are. Break-glass credentials under the Privileged Access Standard are never issued to third parties.

Vendor support engineers who need temporary elevated access to their own product's back end work in a session opened and observed by a member of IT, under the vendor access steps agreed in the contract, and the session is logged as if it were a firm privileged session.

## 7. Monitoring and conduct

Third-party accounts are monitored the same way as employee accounts under the Acceptable Use Policy (CU-POL-006), and each named third-party user acknowledges that policy before first sign-in. Sponsors receive a monthly summary of their sponsored accounts' sign-in activity and report anomalies to the information security office. Third-party access from outside the EU requires prior review by the information security office against the residency rules in the Data Classification Standard.

## 8. Offboarding at contract end

On the contract end date recorded in the directory, all of the sponsored accounts for the engagement are disabled automatically: system roles, VPN, m365, and physical access cards under the Physical Access Policy. The sponsor confirms within five business days that firm data held by the provider is returned or destroyed as the contract requires and records the confirmation against the outsourcing register entry where one exists.

Early termination of an engagement is treated as an immediate leaver event: the sponsor notifies the IT helpdesk and accounts are disabled within one hour of notification, matching the dismissal timeline in the Joiner, Mover, Leaver Procedure (CU-POL-003). From the moment a contract end is recorded or notified, new access requests for the engagement's accounts are blocked, exactly as for offboarding employees under the Access Control Policy.

## 9. Records

The directory record, sponsor approvals, exception approvals, register entries, activity summaries, and offboarding confirmations for every third-party account are retained for six years and are produced to our regulator on request, alongside the outsourcing register itself.
