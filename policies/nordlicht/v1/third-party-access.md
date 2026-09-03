# Third-party and contractor access policy

Document id: NL-POL-009
Version: 1.0
Owner: Head of IT Security
Effective date: 2025-02-01
Review cycle: annual

## 1 Purpose and scope

Nordlicht Maschinenbau GmbH works with external parties: freelance engineers, software contractors, machine suppliers performing remote maintenance, auditors, and temporary staff. This policy governs their access to company systems and premises. It applies to every person who is not an employee and needs an account, a badge, or a network connection. The Access Control Policy, the Segregation of Duties Standard, and the Acceptable Use Policy apply to external users in full; this document adds the restrictions specific to them.

## 2 Sponsor requirement

Every external account has a sponsor: a named employee, at least at team lead level, in the department the external party works for. There is no external access without a sponsor, and an account whose sponsor leaves the company or moves departments is suspended until a new sponsor is registered.

The sponsor requests the access, answers for its business need, receives all notifications about the account, and is the mandatory reviewer for it in the quarterly cycle of the Access Review Procedure. The sponsor also confirms that a contract covering confidentiality and data protection is in place before the first request; the IT helpdesk portal requires the contract reference on the request.

## 3 Contractor accounts

External users receive personal accounts in a separate naming scheme that makes them recognizable as external in every system and every log. One person, one account; a supplier with three technicians needs three accounts. Accounts shared within a supplier company are prohibited, and a supplier rotating staff onto an existing account is treated as credential sharing under the Password and Account Recovery Procedure.

Contractor accounts authenticate with the same password and multi-factor rules as employee accounts. Remote access happens through the company VPN with a vpn.user grant; supplier-side remote maintenance tools connect only through the access path IT administration has approved for that supplier, and only for the duration of a maintenance session the sponsor has confirmed.

## 4 Access scope

External users have no department, so no role template applies to them and nothing is provisioned automatically. Every role is an individual request by the sponsor and is classified under the Access Control Policy as non-standard access: elevated for low and medium sensitivity systems, privileged for high sensitivity systems, critical for any role on erp, payments, hris, or prod-k8s.

Requests are limited to what the contracted work requires. A contractor developing software receives gitlab.developer and the project resources, not the union of everything the team has. Access to hris is not granted to external parties except auditors under a specific engagement, and access to payments is not granted to external parties at all.

## 5 Shorter expiries

The expiry caps of the Access Control Policy are the outer limit for everyone; for external accounts the defaults are shorter. A contractor grant expires after 90 days or at the contract end date, whichever comes first, and grants on high or critical systems expire after 30 days or at contract end. The effective expiry remains the minimum of the requested duration, the role default, and the cap. Renewals are new requests by the sponsor, and each renewal confirms that the contract is still active.

## 6 No admin access without exception approval

External users do not receive admin roles. Not erp.admin, not hris.admin, not gitlab.maintainer, and no administrative access to prod-k8s or infrastructure. This is the default answer, and sponsors should not request it.

The single path around this default is a written exception approved by the Head of IT Security and the system owner before the request is submitted. The exception names the person, the role, the task, and the end date. Even under an exception, the grant follows the Privileged Access Standard: 30-day cap, full approver chain, session logging, and multi-factor authentication. An admin session by an external user under an exception is supervised by a named employee of IT administration where the system allows it. Exceptions are reviewed at every quarterly access review and lapse at their end date without renewal by default.

## 7 Physical access

External badges follow the Physical Access Policy. A contractor badge is bound to the contractor account, expires with it, and opens only the zones the work requires. Supplier technicians in the production halls carry current safety instruction, and the server room rules of that policy apply to external maintenance staff without exception: IT escort, individually logged entry.

## 8 Offboarding at contract end

The sponsor records the contract end date on the account when it is created and updates it when the contract changes. On that date the account and all its grants are revoked automatically, the badge stops working, and new requests naming the account are blocked, mirroring the leaver rules of the Joiner, Mover, Leaver Procedure. The sponsor confirms within five working days that company data in the contractor's possession has been returned or destroyed as the contract requires.

An engagement ending early does not wait for the recorded date; the sponsor reports the end and revocation follows the same day. A contractor returning for a new engagement gets a fresh set of requests, not a reactivated account, and the first 30 days of the new engagement carry the elevated classification floor that the Joiner, Mover, Leaver Procedure applies to new joiners.

## 9 Sponsor accountability

Access misused by an external account is investigated like misuse by an employee, and the findings go to the sponsor's department head as well. A sponsor who cannot say what their external account is currently doing with its access has failed the role; sponsorship transfers to another employee, or the account is revoked.
