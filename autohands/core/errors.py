from __future__ import annotations

from typing import Any

from .models import Capability


class AutoHandsError(Exception):
    pass


class ConfigError(AutoHandsError):
    pass


class AllowlistViolation(AutoHandsError):
    def __init__(self, message: str, subject: str = "") -> None:
        super().__init__(message)
        self.subject = subject


class StepError(AutoHandsError):
    def __init__(
        self,
        step_index: int | None,
        step_description: str,
        expected: str,
        observed: str,
        recoverable: bool = False,
        business_outcome: str = "",
        evidence: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            f"step {step_index} '{step_description}': expected {expected}, observed {observed}"
        )
        self.step_index = step_index
        self.step_description = step_description
        self.expected = expected
        self.observed = observed
        self.recoverable = recoverable
        self.business_outcome = business_outcome
        self.evidence = evidence or []


class LocatorFailure(StepError):
    pass


class BusinessOutcome(StepError):
    pass


class RecoverableFailure(StepError):
    pass


class EscalationRequested(AutoHandsError):
    def __init__(self, request_id: str, message: str, context: dict[str, Any]) -> None:
        super().__init__(f"escalation requested: {message}")
        self.request_id = request_id
        self.message = message
        self.context = context


class CheckpointNotMet(StepError):
    pass


def capability_to_dict(cap: Capability) -> dict[str, Any]:
    return cap.as_dict()