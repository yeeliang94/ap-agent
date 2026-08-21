"""What every claims mutation's request body may contain.

Until now each route took `body: dict` and reached into it — `body["map"]
["employees"][0]["folder"]` — so a body of the wrong SHAPE became a
KeyError or a TypeError, which is an HTTP 500: the server saying "I broke"
about a request that was simply malformed. Declaring the shape here makes
that a 422 naming the field, for free, before the handler runs.

Two deliberate limits on how far the typing goes:

  - VALUES that already have an explanation stay plain `str` and are
    checked in the handler. "decision must be 'accepted' or 'dismissed'"
    and "Rate for 'Car' must be a number per km" are 400s a reviewer can
    act on; a Literal would turn them into 422 type errors.
  - `expected_revision` is optional HERE so that `_revision_check` can go
    on telling the two routes that never send it apart from the ones that
    must (400 "expected_revision is required").

The wire format is unchanged: same keys, same optionality. Bodies that
carry a shape the model does not name (`extra="allow"` on the map) keep it,
because the confirmed map is stored as it arrives.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RevisionBody(BaseModel):
    """Every mutation carries the revision the screen last saw."""
    model_config = ConfigDict(extra="ignore")

    expected_revision: int | None = None


# ---- the confirmed map ------------------------------------------------------

class MapFileBody(BaseModel):
    """One file and what the reviewer says it is. `extra="allow"`: the map
    is stored as it arrives, and the AI's own fields (its reason for the
    role) travel back with it."""
    model_config = ConfigDict(extra="allow")

    path: str = Field(max_length=400)
    role: str = Field(default="", max_length=40)


class MapEmployeeBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    folder: str = Field(max_length=300)
    is_employee: bool = True
    name: str = Field(default="", max_length=120)
    er_code: str = Field(default="", max_length=60)
    report_file: str | None = Field(default=None, max_length=400)
    report_tab: str | None = Field(default=None, max_length=100)
    mileage_tab: str | None = Field(default=None, max_length=100)
    no_report: bool = False
    skip: bool = False
    files: list[MapFileBody] = Field(default_factory=list)


class ClaimMapBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    employees: list[MapEmployeeBody]
    root_files: list[MapFileBody] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RememberedRole(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=40)


class ConfirmMapBody(RevisionBody):
    map: ClaimMapBody
    remember: list[RememberedRole] = Field(default_factory=list)


# ---- Map & Group actions ----------------------------------------------------

class CreateCaseBody(RevisionBody):
    label: str = ""
    artifact_ids: list[str] = Field(default_factory=list)


class UpdateCaseBody(RevisionBody):
    label: str | None = None
    roles: dict[str, Any] | None = None
    state: str | None = None


class ClaimantBody(RevisionBody):
    """Either a name and identifier, or confirm the proposed one as it is."""
    name: str = ""
    identifier: str = ""
    confirm: bool = False


class OwnershipResolutionBody(ClaimantBody):
    """Reviewer attestation that every assigned file belongs to one claimant."""
    note: str = Field(default="", max_length=500)


class MergeCaseBody(RevisionBody):
    into: str = ""


class SplitCaseBody(RevisionBody):
    artifact_ids: list[str] = Field(default_factory=list)
    label: str = ""


class MoveArtifactBody(RevisionBody):
    case_id: str = ""      # "" means out of every case


class ArtifactRoleBody(RevisionBody):
    role: str = ""
    remember: bool = False


class DispositionBody(RevisionBody):
    disposition: str = ""
    reason: str = ""


# ---- review actions ---------------------------------------------------------

class DecideFlagBody(RevisionBody):
    decision: str = ""
    note: str = ""
    disposition: str = ""      # only for ARTIFACT_UNRESOLVED


class CorrectRowBody(RevisionBody):
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class CategoryBody(RevisionBody):
    category: str = ""
    gl: str = ""
    reason: str = ""


# ---- settings ---------------------------------------------------------------

class ClaimsSettingsBody(BaseModel):
    """The shape only: each profile VALUE is checked in settings_schema,
    where a wrong one gets a sentence instead of a type error."""
    model_config = ConfigDict(extra="ignore")

    profile: dict[str, Any] | None = None
    playbook: Any = None
    forget_last_map: bool = False
