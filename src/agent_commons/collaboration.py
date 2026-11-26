"""Local typed collaboration contracts. This module has no execution entry point."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

EXTENSION_PREFIX = "urn:agent-commons:extension:collaboration:"
EXTENSION_URI = EXTENSION_PREFIX + "v1"
SKILL_PATTERN = r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*@[1-9][0-9]{0,5}$"
SkillKey = Annotated[str, Field(pattern=SKILL_PATTERN, max_length=80)]
SkillScopes = Annotated[list[SkillKey], Field(min_length=1, max_length=8)]
MAX_INPUT = 8192
MAX_OUTPUT = 8192
MAX_REQUEST = 16384


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


@dataclass(frozen=True)
class Skill:
    key: str
    description: str
    input_model: type[ContractModel]
    output_model: type[ContractModel]
    max_input_bytes: int = MAX_INPUT
    max_output_bytes: int = MAX_OUTPUT
    max_execution_seconds: int = 20
    input_media_type: Literal["application/json"] = "application/json"
    output_media_type: Literal["application/json"] = "application/json"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        TypeAdapter(SkillKey).validate_python(skill.key)
        if (
            skill.key in self._skills
            or not 0 < len(skill.description) <= 240
            or not 0 < skill.max_input_bytes <= MAX_INPUT
            or not 0 < skill.max_output_bytes <= MAX_OUTPUT
            or not 0 < skill.max_execution_seconds <= 60
            or skill.input_media_type != "application/json"
            or skill.output_media_type != "application/json"
        ):
            raise ValueError("Invalid or duplicate skill registration")
        for model in (skill.input_model, skill.output_model):
            if not isinstance(model, type) or not issubclass(model, ContractModel):
                raise ValueError("A strict local contract model is required")
            model.model_json_schema()
        self._skills[skill.key] = skill

    def get(self, key: str) -> Skill:
        if key not in self._skills:
            raise ValueError("Unknown skill or schema version")
        return self._skills[key]


Count = Annotated[int, Field(ge=0, le=128)]
Value = Annotated[int, Field(ge=-1_000_000, le=1_000_000)]


class AnalysisInput(ContractModel):
    values: list[Value] = Field(min_length=1, max_length=128)


class AnalysisResult(ContractModel):
    count: Count
    total: int = Field(ge=-128_000_000, le=128_000_000)
    minimum: Value
    maximum: Value


class ReportInput(ContractModel):
    title: str = Field(min_length=1, max_length=120)
    summary: AnalysisResult


class ReportResult(ContractModel):
    markdown: str = Field(min_length=1, max_length=4096)


registry = SkillRegistry()
registry.register(
    Skill(
        "analysis.summary@1", "Summarize a bounded integer series.", AnalysisInput, AnalysisResult
    )
)
registry.register(
    Skill(
        "report.markdown@1", "Describe an analysis summary as Markdown.", ReportInput, ReportResult
    )
)


def advertised_skill(key: str) -> dict:
    skill = registry.get(key)
    identifier, version = key.split("@")
    return {
        "id": identifier,
        "schemaVersion": int(version),
        "description": skill.description,
        "inputSchema": f"urn:agent-commons:skill:{key}:input",
        "outputSchema": f"urn:agent-commons:skill:{key}:output",
        "inputMode": skill.input_media_type,
        "outputMode": skill.output_media_type,
        "executionPolicy": {
            "state": "contract-only",
            "maxInputBytes": skill.max_input_bytes,
            "maxOutputBytes": skill.max_output_bytes,
            "maxExecutionSeconds": skill.max_execution_seconds,
        },
    }


class TypedRequest(ContractModel):
    skill: SkillKey
    message_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    deadline: AwareDatetime
    input: dict


def _unique_object(items: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in items:
        if name in result:
            raise ValueError("Duplicate JSON member")
        result[name] = value
    return result


def validate_typed_request(raw: bytes, *, now: datetime) -> tuple[TypedRequest, str]:
    """Validate bytes and produce the versioned digest for future admission."""
    if len(raw) > MAX_REQUEST:
        raise ValueError("Typed request exceeds the byte limit")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        request = TypedRequest.model_validate_json(_canonical(parsed))
        skill = registry.get(request.skill)
        if not now < request.deadline <= now + timedelta(seconds=skill.max_execution_seconds):
            raise ValueError("Invalid task deadline")
        if len(_canonical(request.input)) > skill.max_input_bytes:
            raise ValueError("Typed input exceeds the byte limit")
        typed_input = skill.input_model.model_validate(request.input)
        payload = request.model_dump(mode="json")
        payload["deadline"] = request.deadline.astimezone(UTC).isoformat()
        payload["input"] = typed_input.model_dump(mode="json")
        digest = hashlib.sha256(b"agent-commons/typed-task/v1\n" + _canonical(payload))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid typed collaboration request") from None
    return request, digest.hexdigest()


def validate_typed_output(key: str, value: object) -> dict:
    skill = registry.get(key)
    result = skill.output_model.model_validate(value).model_dump(mode="json")
    if len(_canonical(result)) > skill.max_output_bytes:
        raise ValueError("Typed output exceeds the byte limit")
    return result
