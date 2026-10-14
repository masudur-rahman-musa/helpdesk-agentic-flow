# Password and account recovery procedure

Document id: NL-PRC-007
Version: 1.0
Owner: Head of IT Security
Effective date: 2025-02-01
Review cycle: annual

## 1 Purpose and scope

This procedure sets the rules for passwords, multi-factor authentication, account lockout, and account recovery at Nordlicht Maschinenbau GmbH. It applies to every account on company systems, employee and contractor alike. Account recovery is the step most often attacked in companies of our size, because it is easier to convince a helpdesk than to crack a password. The identity verification rules in section 5 are therefore the core of this document.

## 2 Password rules

Passwords for company accounts are at least 14 characters long. Length matters more than forced complexity, so there are no mandatory character classes; a long phrase of ordinary words is acceptable and encouraged. The systems reject passwords that appear in known breach lists, contain the company name, or match the account name.

Passwords do not expire on a schedule. A password is changed when there is a reason: suspected compromise, appearance in a breach list, or use of the recovery process in section 5. Reusing a company password on any private service is prohibited, as is reusing a private password for a company account. The company provides a password manager as part of the m365 environment, and employees use it rather than files, notes, or browser sync to private accounts.

Each employee has exactly one personal account per system. Shared accounts are prohibited; the only exception is the sealed break-glass credentials described in the Privileged Access Standard.

## 3 Multi-factor authentication

Multi-factor authentication is mandatory for VPN access, for m365 access from outside company premises, and for every session under a privileged or critical role as defined in the Privileged Access Standard. The approved second factors are the company authenticator app and hardware tokens issued by IT. SMS is not an approved factor.

Employees enroll at least two factors so that losing a phone does not force account recovery. A lost or replaced factor is reported and de-enrolled through the IT helpdesk portal the same day. Approving an authentication prompt you did not initiate is treated as an incident: deny it and report it to it-security@nordlicht.example.

## 4 Lockout

An account locks after 10 consecutive failed password attempts. The lock lasts 15 minutes and then lifts automatically; repeated lock cycles on one account alert the IT security office. The lockout counter and duration are the same for all systems that support central authentication.

A locked account is not, by itself, a recovery case. Employees wait out the lock or, if they suspect someone else caused the failed attempts, report it. IT staff never lift a lock early on the phone without the verification in section 5.

## 5 Identity verification for recovery

Account recovery means any action that restores or changes control of an account: a password reset, de-enrolling or replacing an MFA factor, or unlocking an account outside the automatic window. Every recovery runs through the IT helpdesk, and every recovery requires identity verification. There are no exceptions for seniority, urgency, or how well the helpdesk agent knows the caller. A convincing voice on the phone is exactly what an attacker sounds like.

Verification uses one of the following paths, in descending order of preference.

In person: the employee appears at the IT helpdesk with their company badge. This is the default path for anyone on site.

Video with a second person: for remote employees, a video call in which the employee shows their face and their manager, or another employee known to the helpdesk agent, joins the call and confirms the identity. The confirming person is recorded on the ticket.

Callback with HR data check: where video is not possible, the helpdesk calls back on the mobile number stored in the HR System before the incident, never a number provided in the request, and verifies employment details against the HR record.

Recovery of an account that holds a privileged or critical role additionally requires confirmation by the employee's manager before the reset executes. Requests to redirect a recovery, for example "send the reset link to my private address", are refused; recovery credentials go only to enrolled factors or are handed over in person.

After every recovery the account owner receives a notification on all remaining channels, so that a recovery the owner did not request is noticed at once. Every recovery is logged with the verification path used and the acting helpdesk agent.

## 6 No credential sharing

Nobody shares a password, an MFA factor, or an active session, with anyone, for any reason. Not with a manager, not with IT, not with a colleague covering an absence. IT staff never ask for a password; a request for one is by definition an attack and is reported. Absence cover is arranged through access of one's own, requested for the covering employee under the Access Control Policy, never through a borrowed login. A credential that has been shared, even once, even with good intent, is treated as compromised and is reset.

## 7 Compromise response

An employee who suspects their password or factor is compromised resets it immediately through self-service where available and reports to it-security@nordlicht.example. The IT security office can lock any account without notice when compromise is suspected. After a confirmed compromise, the account's grants are reviewed with the manager before the account is re-enabled, and privileged roles are re-requested from scratch under the Privileged Access Standard.

## 8 Records

Recovery tickets, verification paths, lockout alerts, and compromise reports are retained for one year and are included in the quarterly reporting under the Access Review Procedure. Helpdesk agents who deviate from the verification paths in section 5 must record the deviation and the reason on the ticket at the time it happens, and every such deviation is reviewed by the Head of IT Security.
