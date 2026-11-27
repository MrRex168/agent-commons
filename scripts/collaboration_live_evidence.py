"""Public, fixed-shape evidence for the isolated typed collaboration demo."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_commons.peer_protocol import Root

SUMMARY = "analysis.summary@1"
REPORT = "report.markdown@1"
MAX_EVIDENCE_BYTES = 8192


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fingerprints(EvidenceModel):
    atlas: Root
    nova: Root
    orion: Root


class Capabilities(EvidenceModel):
    nova: tuple[Literal["analysis.summary@1"]]
    orion: tuple[Literal["report.markdown@1"]]


class TaskEvidence(EvidenceModel):
    origin: Literal["nova-source", "orion", "nova-target"]
    skill: Literal["analysis.summary@1", "report.markdown@1"]
    task_id: UUID
    state: Literal["completed"]
    artifact_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Migration(EvidenceModel):
    reference_id: UUID
    follow_id: UUID
    location_before: Annotated[int, Field(strict=True, ge=1, le=1)]
    location_after: Annotated[int, Field(strict=True, ge=2, le=2)]


class Checks(EvidenceModel):
    database_isolation: Literal["pass"]
    signed_discovery: Literal["pass"]
    skill_scoped_enrollment: Literal["pass"]
    structured_handoff: Literal["pass"]
    unauthorized_skill_rejected: Literal["pass"]
    cross_owner_task_rejected: Literal["pass"]
    cross_owner_history_denied: Literal["pass"]
    idempotent_retry: Literal["pass"]
    conflicting_retry_rejected: Literal["pass"]
    restart_history_preserved: Literal["pass"]
    migration_history_preserved: Literal["pass"]
    root_reference_and_follow_preserved: Literal["pass"]
    stale_location_rejected: Literal["pass"]
    old_credential_rejected_at_destination: Literal["pass"]
    source_task_not_transferred: Literal["pass"]
    fresh_destination_grant: Literal["pass"]
    revoked_grant_rejected: Literal["pass"]
    isolated_workers_executed: Literal["pass"]
    cleanup_completed: Literal["pass"]


class ReportBase(EvidenceModel):
    schema_version: Literal[1] = 1
    topology: Literal["isolated-loopback-https"] = "isolated-loopback-https"

    def json_report(self) -> str:
        payload = self.model_dump_json(indent=2) + "\n"
        if len(payload.encode()) > MAX_EVIDENCE_BYTES:
            raise ValueError("Evidence exceeds its byte limit")
        return payload


class FailedReport(ReportBase):
    outcome: Literal["fail"] = "fail"


class Evidence(ReportBase):
    outcome: Literal["pass"] = "pass"
    nodes: tuple[Literal["atlas"], Literal["nova-source"], Literal["orion"], Literal["nova-target"]]
    identities: Fingerprints
    advertised_skills: Capabilities
    authorized_skills: Capabilities
    tasks: tuple[TaskEvidence, TaskEvidence, TaskEvidence]
    migration: Migration
    checks: Checks
    runtime_seconds: Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)]

    @model_validator(mode="after")
    def verify_shape(self):
        if (
            len(set(self.identities.model_dump().values())) != 3
            or len({task.task_id for task in self.tasks}) != 3
            or [(task.origin, task.skill) for task in self.tasks]
            != [("nova-source", SUMMARY), ("orion", REPORT), ("nova-target", SUMMARY)]
        ):
            raise ValueError("Evidence topology does not match the collaboration scenario")
        return self
