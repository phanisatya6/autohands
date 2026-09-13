from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ARTIFACT_SCHEMA_VERSION = 1


def uuid4_hex() -> str:
    return uuid.uuid4().hex[:12]


def _list_or_empty(v: Any) -> Any:
    return v if v is not None else []


class LocatorStrategy(str, Enum):
    ROLE_NAME = "role_name"
    TEXT = "text"
    LABEL = "label"
    CSS = "css"
    TABLE_CELL = "table_cell"
    POSITION = "position"


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    FILL = "fill"
    CLICK = "click"
    SELECT = "select"
    PRESS = "press"
    WAIT_FOR = "wait_for"
    EXTRACT = "aggregate_extract"
    EXPECT = "expect"


class ErrorDisposition(str, Enum):
    RECOVERABLE = "recoverable"
    EXPECTED_BUSINESS_OUTCOME = "expected_business_outcome"
    HARD_FAILURE = "hard_failure"
    ESCALATE = "escalate"


class ErrorPlan(BaseModel):
    disposition: ErrorDisposition = ErrorDisposition.HARD_FAILURE
    guard: str = ""
    on_transient: int = 2
    expected_texts: list[str] = Field(default_factory=list)


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    fallbacks: list[Locator] = Field(default_factory=list)
    reason: str = ""


class Step(BaseModel):
    id: str = Field(default_factory=uuid4_hex)
    description: str = ""
    action: ActionKind
    locator: Locator | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    on_error: ErrorPlan | None = None
    checkpoint: str = ""


class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "boolean", "decimal"] = "string"
    required: bool = True
    description: str = ""
    sensitive: bool = False


class OutputSpec(BaseModel):
    name: str
    type: Literal["text", "number"] = "text"
    locator: Locator
    attribute: Literal["text", "value", "href"] = "text"
    description: str = ""


class Checkpoint(BaseModel):
    kind: Literal["text", "url_contains"] = "text"
    value: str = ""
    timeout_ms: int = 15000


class BusinessOutcomeSpec(BaseModel):
    label: str
    expected_texts: list[str] = Field(default_factory=list)


class SurfaceSpec(BaseModel):
    kind: Literal["web"] = "web"
    base_url: str
    entry_path: str = "/"
    user_agent: str = ""


class SecuritySpec(BaseModel):
    allowed_domains: list[str]
    allowed_domains_are_suffixes: bool = True
    allowed_actions: list[ActionKind] = Field(
        default_factory=lambda: [
            ActionKind.NAVIGATE,
            ActionKind.FILL,
            ActionKind.CLICK,
            ActionKind.SELECT,
            ActionKind.PRESS,
            ActionKind.WAIT_FOR,
            ActionKind.EXTRACT,
        ]
    )
    max_steps: int = 30
    risk_policy: dict[str, str] = Field(default_factory=dict)
    sensitive_param_names: list[str] = Field(default_factory=list)


class TenantOverride(BaseModel):
    tenant_id: str
    entry_path: str | None = None
    base_url: str | None = None


class Capability(BaseModel):
    id: str = Field(default_factory=uuid4_hex)
    name: str
    description: str = ""
    schema_version: int = ARTIFACT_SCHEMA_VERSION
    version: str = "1.0.0"
    created_at: int = Field(default_factory=time.time_ns)
    updated_at: int = Field(default_factory=time.time_ns)
    source_goal: str = ""
    surface: SurfaceSpec
    parameters: list[ParamSpec] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    checkpoint: Checkpoint
    business_outcomes: list[BusinessOutcomeSpec] = Field(default_factory=list)
    security: SecuritySpec
    tenant_overrides: list[TenantOverride] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator(
        "parameters", "steps", "outputs", "tenant_overrides", "business_outcomes", mode="before",
    )
    def _list_defaults(cls, v: Any) -> Any:
        return _list_or_empty(v)

    def param_ids(self) -> set[str]:
        return {p.name for p in self.parameters}

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class ElementInfo(BaseModel):
    ref: str
    role: str = ""
    name: str = ""
    label: str = ""
    value: str = ""
    tag: str = ""
    control_type: str = ""
    visible: bool = True
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    id_attr: str = ""
    class_name: str = ""
    css_path: str = ""
    table_cell_key: str = ""
    aria_label: str = ""


class ActionResult(BaseModel):
    ok: bool = True
    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class RunOutcome(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"
    ESCALATION_REQUESTED = "escalation_requested"
    STOPPED_MAX_STEPS = "stopped_max_steps"
    STOPPED_TIMEOUT = "stopped_timeout"
    STOPPED_GOAL_ASSUMED = "stopped_goal_assumed"


class RunResult(BaseModel):
    outcome: RunOutcome
    message: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome: str = ""
    step_failed: int | None = None
    step_failed_description: str = ""
    expected: str = ""
    observed: str = ""
    evidence_dir: str = ""
    elapsed_ms: int = 0