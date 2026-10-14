# Access Review Procedure

Document id: CU-POL-005
Version: 1.0
Owner: Head of Information Security
Effective date: 2025-01-20
Review cycle: annual

## 1. Purpose and scope

This procedure describes the periodic review of who holds what access at Cuan Financial Services Ltd. Requests are approved one at a time under the Access Control Policy (CU-POL-001); reviews look at the accumulated result. They catch grants that were right when made and are wrong now: the project ended, the person moved, the duty changed. Our regulator expects periodic recertification of access in a payments firm, and the records from these reviews are part of what we produce at inspection.

The review covers every account on every system in the firm's inventory, including contractor accounts under the Third-party and Contractor Access Policy (CU-POL-009) and service accounts owned by IT.

## 2. Schedule

Reviews run quarterly, in the first month of each quarter, over the access picture as of the last day of the previous quarter. The information security office opens the review, distributes the material, and tracks completion. Each review cycle closes within 20 business days of opening.

Privileged and critical access, as defined in the Privileged Access Standard (CU-POL-004), is reviewed in every cycle and is presented first in each manager's material. Grants on low and medium sensitivity systems are reviewed in full at least twice a year and sampled in the other cycles.

## 3. What managers certify

Each department manager receives a listing of every grant held by every member of their department: system, role, privilege, risk class at grant, grant date, expiry, and whether the role is in the department template. For each grant the manager records one of three decisions.

Retain: the person still needs the access for their current duties. The manager is certifying the need, not the history; "they have had it a while" is not a reason to retain.

Revoke: the access is no longer needed. IT revokes within five business days of the review closing.

Adjust: the person needs a lesser role on the same system, for example ledger.viewer instead of ledger.editor. The adjustment is raised as a normal request for the lesser role, and the greater role is revoked when it is granted.

A manager who cannot judge a grant, for example a technical role they do not understand, consults the system owner before deciding. Certifying without understanding is a missed review, not a completed one.

Managers do not certify their own grants. Each manager's access is reviewed by their own manager, and department heads are reviewed by the Head of Information Security.

## 4. Stale and out-of-template grants

The review material flags two categories for extra attention.

Stale grants are grants with no recorded use in the past 90 days, where the system produces usage records. A stale grant is retained only with a written reason, for example a duty that occurs annually. Absent a reason, the default decision is revoke.

Out-of-template grants are roles outside the holder's department template. These were individually justified when requested; the review asks whether the justification still stands. An out-of-template grant retained across two consecutive reviews prompts the information security office to ask the department head whether the template itself should change under the Joiner, Mover, Leaver Procedure (CU-POL-003).

## 5. Segregation of duties during review

The review runs a full conflict check of every person's role set against the Segregation of Duties Standard (CU-POL-002). The automated request-time and execution-time checks make conflicts rare, but transfers, template changes, and manual provisioning errors can still create them.

Any conflict found, for example one person holding both roles named in SoD-STD 2.6, is handled under section 4 of that standard: the manager is notified within one business day and one role of the pair is given up within five. The review does not close for that department until the conflict is resolved or the revocation is in flight. Conflicts found at review are also recorded as control failures and examined for how they got past the automated checks; the finding goes to the information security office's quarterly report.

## 6. Deadlines and escalation

Managers complete their certifications within 15 business days of the review opening. The information security office sends a reminder at day 10 and escalates to the manager's own manager at day 15.

A review still incomplete at day 20 has consequences. Every unreviewed grant that is out of template, stale, or privileged class or above is suspended until the manager completes the review. Suspension removes the access but preserves the grant record, so completing the review restores it without a fresh request. The incomplete review is reported to the department head and counted in the quarterly report to the executive.

Repeated failure to complete reviews is a performance matter for the manager, because certification is the control our regulator relies on managers to perform.

## 7. Evidence and retention

For each cycle the information security office retains: the opening listing per department, every decision with the deciding manager and timestamp, written reasons for retained stale grants, conflict findings and their resolution, revocation confirmations from IT, and the completion report. Records are retained for six years in line with the Data Classification Standard (CU-POL-010) and are the standing evidence pack for supervisory inspection.

## 8. Out-of-cycle reviews

The Head of Information Security may open a targeted review at any time: after an incident, after a reorganisation that moves many people at once, or on a request from our regulator. A targeted review follows this procedure with a narrowed scope and a deadline set at opening.

## 9. Interaction with expiry

Expiry under the Access Control Policy is the first line: most grants end on their own within 30 to 365 days. The quarterly review is the second line and is not a substitute for expiry. A manager must not treat "it will expire soon anyway" as a reason to skip a revoke decision; access that is not needed is revoked at review, whatever its remaining term.
