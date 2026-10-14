# Access Review Procedure

Document id: CU-POL-005
Version: 2.0
Owner: Head of Information Security
Effective date: 2026-02-16
Review cycle: annual
Revision history: 2.0 reorganises the procedure around the review cycle stages, adds a worked certification example, detail on the regulator evidence pack, and a clearer escalation ladder. The schedule, decision types, staleness rule, and suspension consequences are unchanged from 1.0.

## 1. Why reviews exist

Every grant at Cuan Financial Services Ltd was approved one at a time under the Access Control Policy (CU-POL-001), and each was reasonable on the day it was made. Reviews exist because circumstances drift: projects finish, duties change, people move. A periodic recertification of accumulated access is what our regulator expects of a payments firm, and the records each cycle produces are part of the standing evidence at inspection.

The review reaches every account on every system in the firm's inventory. That includes contractor accounts governed by the Third-party and Contractor Access Policy (CU-POL-009) and the service accounts IT owns.

## 2. The cycle

A review runs every quarter, opening in the first month of the quarter and examining the access picture as it stood on the last day of the quarter before. The information security office opens the cycle, issues the material, and tracks progress, and the whole cycle closes within 20 business days of opening.

Not everything is examined at the same depth every time. Privileged and critical class access, as the Privileged Access Standard (CU-POL-004) defines it, appears in every cycle and sits at the top of each manager's material. Grants on low and medium sensitivity systems get a full examination at least twice a year and a sample in the remaining cycles.

## 3. The certification task

Each department manager receives one listing covering every grant held by every person in their department. For each grant the listing shows the system, role, privilege, the risk class assigned at grant, the grant date, the expiry, and whether the role belongs to the department template. The manager records exactly one decision per grant.

Retain means the person needs the access for their current duties. The certification is about present need, never about history; that a person has held something a long time is no argument for keeping it.

Revoke means the need has ended. IT carries out the revocation within five business days of the cycle closing.

Adjust means a lesser role on the same system would do. The manager raises the lesser role as an ordinary request, and the greater role is revoked once the lesser one is granted.

### 3.1 A worked example

A Finance team member holds data-warehouse.analyst, in the Finance template, used last week: retain. The same person holds ledger.editor but has moved to purely analytical work and no longer posts entries: adjust, requesting ledger.viewer, with ledger.editor revoked when the viewer role lands. A third grant, crm.viewer from a support project that ended in the spring, shows no use for months: revoke.

### 3.2 Limits on who certifies

A manager who cannot judge a grant, such as a technical role they do not understand, asks the system owner before deciding; signing off blind counts as a missed review, not a done one. No manager certifies their own access. Each manager is reviewed by their own manager, and department heads are reviewed by the Head of Information Security.

## 4. Flagged categories

Two kinds of grant are marked in the material for particular attention.

A stale grant shows no recorded use in the past 90 days, on systems that produce usage records. Retaining a stale grant requires a written reason, such as a duty that recurs annually; without one, the default decision is revoke.

An out-of-template grant is a role outside the holder's department template. It was individually justified once; the question at review is whether that justification still holds. When such a grant survives two consecutive reviews, the information security office asks the department head whether the template itself should change through the process in the Joiner, Mover, Leaver Procedure (CU-POL-003).

## 5. Conflicts found at review

Alongside certification, every person's complete role set is checked against the Segregation of Duties Standard (CU-POL-002). Request-time and execution-time checks make conflicts rare, but transfers, template edits, and manual provisioning mistakes can still produce one.

A conflict found, for instance one person holding both roles named in SoD-STD 2.6, follows section 4 of that standard: the manager hears within one business day, and one role of the pair is surrendered within five. The department's review stays open until the conflict is resolved or the revocation is in flight. Because the automated checks should have prevented it, every conflict found at review is also logged as a control failure, examined for how it slipped through, and reported in the information security office's quarterly report.

## 6. Deadlines and the escalation ladder

The ladder for a manager's certification runs as follows. Certifications are due within 15 business days of opening. At day 10 the information security office sends a reminder. At day 15 the matter escalates to the manager's own manager. At day 20 the consequences in section 7 take effect and the incomplete review is reported to the department head and counted in the quarterly report to the executive.

A manager who repeatedly fails to complete reviews faces it as a performance matter. Certification is the control our regulator relies on managers to perform; declining to perform it is declining part of the job.

## 7. Suspension of unreviewed grants

When a review is still incomplete at day 20, every unreviewed grant that is out of template, stale, or of privileged class or above is suspended until the manager finishes. Suspension removes the access but keeps the grant record intact, so completing the review restores the access without a new request. Grants outside those categories remain live, but the review debt stands until cleared.

## 8. The evidence pack

Each cycle produces a defined set of records that the information security office retains as the inspection evidence pack: the opening listing per department; every decision with the deciding manager and a timestamp; the written reasons behind every retained stale grant; conflict findings with their resolution; IT's revocation confirmations; and the completion report for the cycle. The pack is retained for six years in line with the Data Classification Standard (CU-POL-010). When our regulator asks how the firm knows its access is right, this pack, cycle after cycle, is the answer, so its completeness is checked at close before a cycle is declared finished.

## 9. Targeted reviews

The Head of Information Security may open a review outside the quarterly rhythm: after an incident, after a reorganisation that moves many people at once, or at our regulator's request. A targeted review runs under this procedure with its scope narrowed and its deadline fixed at opening.

## 10. Reviews and expiry are different controls

Expiry under the Access Control Policy is the first control: most grants end by themselves within 30 to 365 days. The quarterly review is the second, and neither substitutes for the other. In particular, a manager does not skip a revoke decision because a grant will lapse soon anyway; access that is not needed is revoked at review, whatever term it has left.
