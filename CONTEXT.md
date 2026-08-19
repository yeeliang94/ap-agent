# Accounts Payable Claims

This context covers turning an arbitrary batch of claim files into reviewed,
evidence-backed payment-listing rows. A batch may be arranged by claimant or
may be an unstructured folder dump.

## Language

**Claims Run**:
One review of one immutable snapshot of files, instructions, reference material,
and explicit client settings.
_Avoid_: Job, batch process

**Source Artifact**:
One submitted file in a Claims Run, whether or not it ultimately supports a
claim. Every Source Artifact must receive a disposition.
_Avoid_: Document, attachment, evidence

**Evidence Item**:
One potentially claim-supporting item found inside a Source Artifact, such as a
receipt, map trip, approval, statement entry, or report line.
_Avoid_: File, receipt when the type is not yet known

**Claim Case**:
A proposed or confirmed set of Claim Lines and Evidence Items intended to
produce one payment-listing decision. Its Claimant may initially be unknown.
_Avoid_: Employee, employee folder, bundle

**Claimant**:
The person or party to whom a Claim Case belongs. A Claimant is not considered
confirmed merely because the AI proposed a name.
_Avoid_: Employee when ownership is not yet confirmed, vendor

**Claim Line**:
One amount being considered for payment, read from a claim summary or derived
from an Evidence Item.
_Avoid_: Receipt, spreadsheet row

**Evidence Assignment**:
A proposed, confirmed, or rejected relationship from an Evidence Item to a
Claim Case and optionally to a Claim Line.
_Avoid_: Match when the relationship has not been confirmed

**Disposition**:
The recorded outcome for a Source Artifact or Evidence Item: used, duplicate,
irrelevant, unreadable, or unresolved.
_Avoid_: Ignored, skipped

**Citation**:
The precise source location supporting a value or finding, such as a workbook
cell or a file page and region.
_Avoid_: Reference, source note

**Flag**:
A cited control finding that is either informational or requires a reviewer
decision before output can be released.
_Avoid_: Error, exception

**Investigation Plan**:
The run-local record of what the agent decided to inspect, calculate, group, and
verify. It exists for one Claims Run and is not a reusable company rule set.
_Avoid_: Company recipe, client template

**Explicit Client Profile**:
Reviewer-maintained facts such as mileage rates, tolerances, category values,
and receipt exceptions. It is never updated automatically from AI conclusions.
_Avoid_: Learned profile, company recipe

**Payment Listing Row**:
The reviewed row emitted for a Claim Case in the column order learned from the
current run's payment listing.
_Avoid_: Claim Case, output record
