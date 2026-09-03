# Password and Account Recovery Procedure

Document id: CU-POL-007
Version: 2.0
Owner: Head of Information Security
Effective date: 2026-02-23
Review cycle: annual
Revision history: 2.0 rewords the verification and pressure sections and adds a note on recovery for remote staff; verification steps and password rules are unchanged from 1.0.

## 1. Purpose and scope

This procedure sets the rules for passwords, multi-factor authentication, account lockout, and account recovery at Cuan Financial Services Ltd. It applies to every account on every firm system, held by an employee or a contractor, including privileged accounts under the Privileged Access Standard (CU-POL-004). Nothing in this procedure changes who may hold access; that is the Access Control Policy (CU-POL-001). This procedure is about proving you are the account holder.

Account recovery is a favourite route for attackers because it turns a social engineering conversation into a working credential. The verification steps in section 6 are therefore not courtesies to be waived for a colleague in a hurry. They are the control.

## 2. Passwords

Passwords for firm accounts must be at least 14 characters. Length matters more than symbol rules, so a long passphrase of unrelated words is acceptable and encouraged. There is no forced periodic change; a password changes when there is a reason: suspected compromise, appearance in a breach dataset, or recovery under section 6.

Each firm system gets its own password. Reusing a firm password on any external service, or reusing a personal password for a firm account, is prohibited. New passwords are screened against known breached password lists and common patterns; screening rejects, among others, the firm name and season-plus-year forms.

Staff use the firm's password manager for all work credentials. Passwords are not written down, stored in browser profiles on unmanaged devices, kept in plain files, or embedded in scripts, tickets, or wiki pages. A password discovered in any of those places is treated as compromised and rotated the same day.

## 3. Multi-factor authentication

MFA is mandatory on every firm account. The second factor is an approved authenticator app or a hardware key issued by IT. SMS codes are not an approved factor. Hardware keys are mandatory for admin roles and for all access to the critical systems named in the Privileged Access Standard.

MFA prompts are approved only for sign-ins the user is performing at that moment. Approving a prompt you did not initiate, even to silence repeated prompts, is a security incident; deny it and report to the IT helpdesk immediately. IT staff never ask a user to read out a code or approve a prompt on their behalf, and any such request is by definition fraudulent.

Enrolment of a new MFA device requires the same identity verification as account recovery in section 6, because moving MFA to an attacker's device is equivalent to a password reset.

## 4. Lockout

An account locks after 10 consecutive failed password attempts. The lock lasts 30 minutes and then lifts automatically, or the user can contact the IT helpdesk for an assisted unlock after verification under section 6. Repeated lockouts on one account within a day are flagged to the information security office as a possible attack rather than reset quietly.

Sessions on firm systems expire after inactivity. Screens lock automatically on firm devices after at most 10 minutes idle, and users lock manually when stepping away, as the Acceptable Use Policy (CU-POL-006) requires.

## 5. No credential sharing

No credential for a firm account is ever shared, in either direction, for any reason. This covers passwords, MFA devices, recovery codes, and active signed-in sessions. Managers do not hold their team's passwords. IT staff do not ask for passwords and have no mechanism that requires one; anyone claiming otherwise is not IT. Shared team duties are met through roles granted to each named individual under the Access Control Policy, never through a shared login. Where a system unavoidably requires a service account, IT owns it, stores its credential in the password manager with restricted access, and registers a named human owner.

A person who has shared a credential, or suspects theirs is known to anyone else, reports it to the IT helpdesk the same day. The password is rotated, sessions are revoked, and recent account activity is reviewed. Prompt self-reporting is treated as the right behaviour, not an offence.

## 6. Account recovery and identity verification

A user who has lost their password, lost their MFA device, or is locked out beyond the automatic lift recovers through the IT helpdesk only. There is no self-service recovery that bypasses MFA, and no recovery by email to a personal address.

Before any recovery action, the helpdesk verifies identity by all of the following, with no step skipped:

The request arrives from the user in person at the IT desk, or on a video call where the helpdesk agent can see the person's face alongside their HR System photo. For fully remote staff the video call is the normal route, and the same photo comparison applies.

The helpdesk confirms employment status and department in the HR System. Recovery is refused for any account whose holder is marked as offboarding or left; such accounts are handled under the Joiner, Mover, Leaver Procedure (CU-POL-003).

The helpdesk sends a one-time confirmation to the user's registered manager, who acknowledges that the person has asked for recovery. For recovery of accounts with privileged or critical access, manager acknowledgement is mandatory before the reset, not after.

Knowledge questions, such as start date or badge number, are never sufficient on their own, because that information is available to colleagues and to attackers who research the firm.

After verification, the helpdesk issues a one-time credential valid for one hour and usable once. The user sets a new password and re-enrols MFA at first sign-in. The old sessions are revoked at reset. Every recovery is logged with the requester, the verifying agent, the verification steps taken, and the manager acknowledgement, and the log is reviewed monthly by the information security office.

## 7. Recovery under pressure

Urgency is the attacker's main tool. A recovery request marked urgent follows exactly the steps in section 6. When anyone at any level of the firm pressures a helpdesk agent to skip verification, the agent refuses, completes the steps, and reports the pressure to the information security office. The refusal is backed by this procedure and no manager may override it.

## 8. Records

Password reset and recovery logs, MFA enrolment changes, and lockout events are retained for six years under the Data Classification Standard (CU-POL-010) and form part of the evidence produced to our regulator at inspection.
