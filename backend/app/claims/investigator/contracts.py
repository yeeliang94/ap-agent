"""The deep-module interface of the claims investigator (hardening H1).

    async def investigate(request: InvestigationRequest,
                          tools: InvestigationTools) -> InvestigationResult

Everything a caller (the runner) or a test needs to know is here: what
goes in, what comes out, and the normalized domain records in between —
Source Artifacts, Evidence Items, Claim Cases with a Claimant that may be
unknown, Evidence Assignments, Claim Lines, Flags, the run-local
Investigation Plan and the tool-execution record. HOW the result was
produced (prompts, tools chosen, retries, file formats, the model
provider) stays behind the seam: the legacy structured-folder adapter and
the tool-using investigator both return this shape.

Design rules baked into the types (CLAIMS-AGENT-HARDENING.md, "Design
principles"):
  - a Claim Case may exist without a Claimant; a Claimant proposed by the
    AI is `proposed`, never `confirmed`
  - a Source Artifact always holds exactly one disposition; `unresolved`
    is the only non-terminal one and blocks output
  - Reported Total and Calculated Lines Total are separate fields; the
    former may be None and is never filled from the latter
  - money is text ("45.00") — Decimal-safe, never float
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---- vocabulary --------------------------------------------------------------

Disposition = Literal["used", "duplicate", "irrelevant", "unreadable", "unresolved"]
TERMINAL_DISPOSITIONS = ("used", "duplicate", "irrelevant", "unreadable")
InspectionState = Literal["not_inspected", "inspected", "failed"]
# The roles the investigator may propose for a Source Artifact. "unknown"
# is a role too: it says the file was looked at and could not be placed.
ArtifactRole = Literal["report", "receipts", "approval", "report_copy", "listing", "roster",
                       "policy", "other", "unknown"]
ClaimantState = Literal["confirmed", "proposed", "unknown"]
CaseState = Literal["proposed", "confirmed", "blocked", "excluded"]
AssignmentState = Literal["proposed", "confirmed", "rejected"]
AssignmentBasis = Literal["exact_identifier", "explicit_name", "filename_rule", "report_reference",
                          "folder_structure", "reviewer_decision", "ai_inference"]
LineOrigin = Literal["reported", "evidence_derived", "reviewer_entered"]
EvidenceKind = Literal["receipt", "map_trip", "report_line", "approval", "other"]
Strategy = Literal["structured", "full_dump", "evidence_only"]


class Citation(BaseModel):
    """Where a value or finding comes from: an artifact (by manifest id and
    path) and a place inside it — a cell/row for a workbook, a page and
    position or region for a document."""
    artifact_id: str = ""
    path: str = ""
    sheet: str = ""
    cell: str = ""
    row: int | None = None
    page: int | None = None
    position: str = ""
    region: list[int] | None = None  # x0, y0, x1, y1 in page pixels
    note: str = Field(default="", max_length=300)

    def as_flag_cite(self) -> dict:
        """The delivered flag `cite` shape ({"file","page","position"} or {"sheet","row"})."""
        if self.sheet or self.row is not None:
            return {"sheet": self.sheet, "row": self.row or 0}
        return {"file": self.path, "page": self.page or 0, "position": self.position}


class Budget(BaseModel):
    """Resource limits for one investigation. Every adapter fails closed at
    these; a correction or tie-break cannot open a bigger budget."""
    wall_seconds: int = Field(default=600, ge=1)
    model_requests: int = Field(default=160, ge=0)
    tool_calls: int = Field(default=400, ge=0)
    bytes_read: int = Field(default=200 * 1024 * 1024, ge=0)
    pages_read: int = Field(default=2000, ge=0)


class ManifestEntry(BaseModel):
    """One file of the immutable run snapshot."""
    id: str
    path: str  # run-relative, forward slashes
    size: int = 0
    sha256: str = ""
    media_type: str = ""  # workbook / pdf / image / other
    pages: int | None = None
    sheets: list[str] = Field(default_factory=list)
    snapshot: str = ""  # path under the run workspace where the copy lives


class SourceArtifact(BaseModel):
    """One submitted file, whatever it turns out to be."""
    id: str
    path: str
    sha256: str = ""
    media_type: str = ""
    size: int = 0
    pages: int | None = None
    sheets: list[str] = Field(default_factory=list)
    inspection_state: InspectionState = "not_inspected"
    failure_reason: str = ""
    proposed_role: ArtifactRole = "unknown"
    role_reason: str = Field(default="", max_length=400)
    role_citations: list[Citation] = Field(default_factory=list)
    disposition: Disposition = "unresolved"
    disposition_reason: str = Field(default="", max_length=400)
    disposition_by: Literal["", "adapter", "reviewer"] = ""
    # An artifact the reviewer must look at (unknown role, or a
    # potentially material file that could not be read).
    needs_confirmation: bool = False

    @model_validator(mode="after")
    def _terminal_needs_reason(self):
        if self.disposition in ("irrelevant", "unreadable", "duplicate") and not self.disposition_reason:
            raise ValueError(f"disposition {self.disposition!r} needs a reason")
        return self

    @property
    def terminal(self) -> bool:
        return self.disposition in TERMINAL_DISPOSITIONS


class EvidenceItem(BaseModel):
    """One extracted item from a Source Artifact."""
    id: str
    artifact_id: str
    kind: EvidenceKind
    values: dict[str, Any] = Field(default_factory=dict)
    confidence: dict[str, str] = Field(default_factory=dict)
    extraction_method: str = ""
    citation: Citation = Field(default_factory=Citation)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Claimant(BaseModel):
    name: str = ""
    identifier: str = ""  # the ER(...) code or employee code
    state: ClaimantState = "unknown"
    basis: str = Field(default="", max_length=400)
    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _confirmed_needs_name(self):
        if self.state == "confirmed" and not self.name.strip():
            raise ValueError("a confirmed Claimant needs a name")
        return self


class ClaimLine(BaseModel):
    """One amount considered for payment. Universal money fields are
    stable; source-specific values live under attributes."""
    id: str
    case_id: str = ""
    origin: LineOrigin = "reported"
    kind: str = "expense"  # expense / mileage / derived (delivered vocabulary)
    date: str = ""
    description: str = ""
    claimed_amount: str | None = None
    currency: str = "MYR"
    rate: str | None = None
    home_amount: str | None = None
    category: str = ""
    gl: str = ""
    purpose: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    citation: Citation = Field(default_factory=Citation)
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceAssignment(BaseModel):
    id: str
    evidence_id: str = ""   # an Evidence Item, or
    artifact_id: str = ""   # a whole Source Artifact (a receipts bundle)
    case_id: str
    line_id: str = ""
    state: AssignmentState = "proposed"
    basis: AssignmentBasis = "ai_inference"
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(default="", max_length=400)
    citations: list[Citation] = Field(default_factory=list)


class ClaimCase(BaseModel):
    """One proposed payment-listing decision."""
    id: str
    claimant: Claimant = Field(default_factory=Claimant)
    state: CaseState = "proposed"
    grouping_basis: str = Field(default="", max_length=400)
    citations: list[Citation] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    line_ids: list[str] = Field(default_factory=list)
    category: str = ""
    gl: str = ""
    reported_total: str | None = None
    reported_total_citation: Citation | None = None
    lines_total: str | None = None
    # Delivered worker input, kept so the structured path is unchanged:
    # {"report_file","report_tab","mileage_tab","receipt_files","ignored",
    #  "unplaced","no_report"}. Empty for a case the investigator built.
    roles: dict[str, Any] = Field(default_factory=dict)
    # A folder label / grouping label for the screen (was: the folder).
    label: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(default="", max_length=400)


class FlagProposal(BaseModel):
    code: str
    reason: str
    basis: str = ""
    cite: dict[str, Any] = Field(default_factory=dict)
    case_id: str = ""
    row_id: str = ""
    evidence_id: str = ""
    artifact_id: str = ""
    status: Literal["open", "info"] = "open"


class ToolExecution(BaseModel):
    id: str
    tool: str
    elapsed_ms: int = 0
    input_hashes: list[str] = Field(default_factory=list)
    output_hash: str = ""
    truncated: bool = False
    error_code: str = ""
    note: str = Field(default="", max_length=300)


class InvestigationPlan(BaseModel):
    """What the agent decided to inspect, calculate, group and verify —
    for THIS run only. Stored for audit and replay, never reused."""
    strategy: Strategy | None = None
    objective: str = ""
    steps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    rounds: int = 0
    adapter: str = ""


class InvestigationRequest(BaseModel):
    run_id: str
    workspace: str  # the run's immutable workspace path
    manifest: list[ManifestEntry] = Field(default_factory=list)
    instructions: str = ""
    objective: str = ""
    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    references: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    # The delivered survey (folders, files, peeks). The legacy adapter
    # reads it; the investigator builds its own inventory from the
    # manifest and only uses it as a hint. Optional.
    survey: dict[str, Any] = Field(default_factory=dict)


class InvestigationResult(BaseModel):
    artifacts: list[SourceArtifact] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    cases: list[ClaimCase] = Field(default_factory=list)
    assignments: list[EvidenceAssignment] = Field(default_factory=list)
    lines: list[ClaimLine] = Field(default_factory=list)
    flags: list[FlagProposal] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    plan: InvestigationPlan = Field(default_factory=InvestigationPlan)
    tool_executions: list[ToolExecution] = Field(default_factory=list)
    # Compatibility: the delivered map dict (employees/root_files/notes)
    # so the delivered Map screen and confirm-map route keep working.
    map: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    notes: list[tuple[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_disposition_each(self):
        seen: set[str] = set()
        for a in self.artifacts:
            if a.id in seen:
                raise ValueError(f"artifact {a.id} appears twice in the result")
            seen.add(a.id)
        case_ids = {c.id for c in self.cases}
        for asg in self.assignments:
            if asg.case_id not in case_ids:
                raise ValueError(f"assignment {asg.id} points at unknown case {asg.case_id}")
        return self

    # ---- the questions callers ask ----------------------------------------------

    def unresolved_artifacts(self) -> list[SourceArtifact]:
        return [a for a in self.artifacts if a.disposition == "unresolved"]

    def blocking_conditions(self) -> list[str]:
        """Plain reasons output must stay locked (adapter-neutral)."""
        why = [f"{a.path}: no disposition yet" for a in self.unresolved_artifacts()]
        for c in self.cases:
            if c.state in ("proposed", "confirmed") and c.claimant.state != "confirmed":
                why.append(f"case {c.label or c.id}: claimant {c.claimant.state}")
        for f in self.flags:
            if f.status == "open":
                why.append(f"{f.code}: {f.reason[:80]}")
        return why
