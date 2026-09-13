from __future__ import annotations

import json
from pathlib import Path

import pytest

from autohands.core.models import (
    ActionKind,
    BusinessOutcomeSpec,
    Capability,
    Checkpoint,
    ErrorDisposition,
    ErrorPlan,
    Locator,
    LocatorStrategy,
    OutputSpec,
    ParamSpec,
    SecuritySpec,
    Step,
    SurfaceSpec,
)
from autohands.core.store import ArtifactStore, render_params, validate_capability


def build_capability(**overrides) -> Capability:
    cap = Capability(
        name="pay-bill",
        description="pay a bill",
        source_goal="Make a payment",
        surface=SurfaceSpec(base_url="https://bank.example", entry_path="/"),
        parameters=[
            ParamSpec(name="amount", type="decimal", required=True, description="Payment amount"),
            ParamSpec(name="pin", type="string", sensitive=True),
        ],
        steps=[
            Step(
                id="s1",
                action=ActionKind.NAVIGATE,
                description="go to pay page",
                args={"url": "https://bank.example/pay?amt={param.amount}"},
            ),
            Step(
                id="s2",
                action=ActionKind.FILL,
                description="enter amount",
                locator=Locator(strategy=LocatorStrategy.TEXT, value="Amount"),
                args={"text": "{param.amount}"},
            ),
        ],
        checkpoint=Checkpoint(value="Payment sent"),
        security=SecuritySpec(allowed_domains=["bank.example"]),
    )
    return cap.model_copy(deep=True, update=overrides)


def test_validate_capability_ok():
    assert validate_capability(build_capability()) == []


def test_validate_capability_undeclared_param():
    cap = build_capability()
    cap.steps.append(Step(action=ActionKind.CLICK, description="x", args={"text": "{param.ghost}"}))
    problems = validate_capability(cap)
    assert any("ghost" in problem for problem in problems)


def test_validate_capability_empty_steps_and_version():
    cap = build_capability(steps=[])
    problems = validate_capability(cap)
    assert any("no steps" in problem for problem in problems)

    cap2 = build_capability()
    cap2.schema_version = 999
    problems2 = validate_capability(cap2)
    assert any("schema_version" in problem for problem in problems2)


def test_validate_capability_extra_keys_rejected():
    payload = build_capability().model_dump(mode="json")
    payload["surprise_field"] = True
    with pytest.raises(Exception):
        Capability(**payload)


def test_render_params():
    rendered = render_params(
        "to {param.amount} with pin {param.pin}",
        {"amount": "250.00", "pin": "1234"},
    )
    assert rendered == "to 250.00 with pin 1234"


def test_store_round_trip(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    cap = build_capability()
    path = store.save(cap)
    assert path.exists()
    loaded = store.load(cap.id)
    assert loaded.name == cap.name
    assert len(loaded.steps) == len(cap.steps)
    assert loaded.parameters[0].sensitive is False
    assert loaded.parameters[1].sensitive is True
    assert store.list()[0].id == cap.id


def test_store_rejects_future_schema(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    cap = build_capability(schema_version=999)
    with pytest.raises(Exception):
        store.save(cap)


def test_business_outcomes_roundtrip():
    cap = build_capability()
    cap.business_outcomes.append(
        BusinessOutcomeSpec(label="declined", expected_texts=["Payment declined"])
    )
    payload = json.loads(cap.model_dump_json(exclude_none=True))
    assert payload["business_outcomes"][0]["label"] == "declined"
    assert Capability(**payload).business_outcomes[0].expected_texts == ["Payment declined"]


def test_error_plan_defaults():
    step = Step(action=ActionKind.CLICK)
    assert step.on_error is None
    plan = ErrorPlan()
    assert plan.disposition == ErrorDisposition.HARD_FAILURE
    assert plan.expected_texts == []