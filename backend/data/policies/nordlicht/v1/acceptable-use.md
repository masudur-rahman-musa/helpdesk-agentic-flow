# Acceptable use policy

Document id: NL-POL-006
Version: 1.0
Owner: Head of IT Security
Effective date: 2025-02-01
Review cycle: annual

## 1 Purpose and scope

This policy sets the rules for using the IT systems of Nordlicht Maschinenbau GmbH. It applies to every employee and, through the Third-party and Contractor Access Policy, to every external user with a company account. It covers all company systems, including email and m365, the wiki, the VPN, the engineering and production systems, and any company device, whether used on site or remotely.

Access to these systems is granted under the Access Control Policy. This document governs how granted access may be used.

## 2 General principles

Company systems exist for company work. Employees use them under their own personal account, for the purposes their access was granted for, and in a way that does not endanger the systems, the data on them, or other users. Nobody may use another person's account, and nobody may let another person use theirs, with credentials or without. The Password and Account Recovery Procedure covers credential handling in detail; the rule that matters here is simple: actions performed under your account are your actions.

Employees handle data according to its class under the Data Classification Standard. In daily terms this means: confidential and pii data stay inside the systems approved for them, and nothing above internal class leaves the company through private channels.

## 3 Email and m365

The company mailbox and the m365 environment are the standard channels for business communication and documents. Employees send company data only to recipients who need it, check external recipient addresses before sending anything above internal class, and use the sharing functions of m365 instead of attaching confidential files to outbound mail where possible.

Auto-forwarding of company mail to external addresses is prohibited. Registering the company address for private services is prohibited. Employees who receive a suspicious message report it to it-security@nordlicht.example using the report function and do not open attachments or links from it. Falling for a well-made phishing mail is not a violation of this policy; failing to report it once noticed is.

## 4 Wiki

The wiki is the company's shared documentation space and is classified as a low sensitivity system. Because a large share of employees can read it, nothing above internal class may be stored there. No customer contract terms, no personnel data, no supplier bank details, no credentials, no unreleased design data. Design and manufacturing content belongs in plm and mes; the wiki may describe processes and link to those systems. Employees with wiki.editor keep pages they own accurate and mark outdated pages rather than leaving them ambiguous.

## 5 VPN and remote work

The VPN is the only approved path into internal systems from outside company premises. Employees connect with their personal vpn.user access and multi-factor authentication. Split-channel workarounds, private remote-access tools, and exposing internal services to the internet are prohibited.

Remote work happens on company-managed devices. Employees working remotely lock their screens when away, keep company data off private devices and private cloud storage, and avoid handling confidential or pii data on screens visible to others in public places.

## 6 Personal use

Occasional personal use of email, m365, and internet access is tolerated as long as it is minor in time and volume, does not interfere with work, creates no cost or legal risk for the company, and would not embarrass the company if it became visible. Personal use never extends to the line-of-business systems: erp, payments, hris, plm, mes, crm, vendor-master, finance-dash, prod-k8s, and gitlab are work-only. Storing personal photo or media collections on company systems, running private businesses through company accounts, and using company systems for political or commercial solicitation are prohibited.

The company can withdraw the tolerance for personal use at any time, generally or for individuals, without changing this policy.

## 7 Monitoring notice

The company logs technical events on its systems: logons, access grants and their use, VPN connections, email metadata, web gateway events, and administrative actions. Logging serves security, troubleshooting, and the audit duties described in the Access Review Procedure. Logs are evaluated for security purposes, in pseudonymized or aggregate form where the purpose allows it. Targeted evaluation of an individual's activity happens only on documented suspicion of a serious violation, with the involvement of HR and, where required, the works council, under applicable data protection law. The company does not read message content in routine operation.

## 8 Prohibited actions

The following are prohibited on all company systems, without needing individual listing elsewhere:

Sharing credentials or authentication factors, or bypassing authentication in any form. Attempting to obtain access beyond what was granted, including probing, scanning, or exploiting other systems, whether or not damage occurs. Disabling or tampering with security controls such as endpoint protection, logging, or screen locks. Installing unapproved software on company devices or connecting unapproved storage media. Copying company data to private accounts, devices, or cloud services. Using company systems to harass, discriminate, or distribute unlawful content. Deliberately introducing malicious code. Concealing a security incident.

Testing of security controls is legitimate only when ordered in writing by the Head of IT Security.

## 9 Reporting and consequences

Employees report suspected incidents, policy violations, and lost or stolen devices to it-security@nordlicht.example without delay. Prompt self-reporting of an honest mistake counts in the employee's favor.

Violations of this policy can lead to revocation of access, disciplinary measures under the employment contract, and, for unlawful acts, civil or criminal proceedings. Access revoked over a violation is re-granted only through the normal process in the Access Control Policy, with the violation on record for the approvers.
