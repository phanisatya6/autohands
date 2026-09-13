from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from autohands.core.errors import ConfigError
from autohands.core.models import (
    ActionKind,
    BusinessOutcomeSpec,
    Capability,
    Checkpoint,
    ElementInfo,
    ErrorDisposition,
    ErrorPlan,
    Locator,
    LocatorStrategy,
    OutputSpec,
    ParamSpec,
    SecuritySpec,
    Step,
    SurfaceSpec,
    TenantOverride,
)
from autohands.replay.engine import AutoAllowOperator, FileOperator, ReplaySession


class FakePage:
    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(ms / 1000)


class FakeSurface:
    """Deterministic stand-in for the browser driver. Drives a FakeWorld."""

    def __init__(self, world: "FakeWorld") -> None:
        self.world = world
        self.page = FakePage()
        self.url_val = world.base_url + world.entry
        self.hidden: set[str] = set()
        self.filled: dict[str, str] = {}

    def navigate(self, url: str) -> None:
        self.url_val = url
        self.world.on_navigate(url)

    def url(self) -> str:
        return self.url_val

    def title(self) -> str:
        return self.world.title

    def list_elements(self):
        return [e for e in self.world.elements() if e.name not in self.hidden]

    def submit_click(self, element) -> None:
        self.world.on_click(element)

    def submit_fill(self, element, text: str) -> None:
        self.filled[element.name] = text
        self.world.on_fill(element, text)

    def submit_select(self, element, option: str) -> None:
        self.filled[element.name] = option

    def press_key(self, key: str) -> None:
        pass

    def read_text(self, element, attribute: str = "text") -> str:
        if attribute == "value":
            return self.filled.get(element.name, "")
        return self.world.cell_value(element)

    def wait_for(self, text: str, timeout_ms: int = 15000) -> bool:
        return text in self.world.body

    def page_find_text(self, text: str) -> bool:
        return text in self.world.body

    def fingerprint(self) -> dict:
        return {"url": self.url_val, "body": self.world.body}

    def screenshot(self, path: str) -> None:
        pass


class FakeWorld:
    def __init__(self, base_url: str = "https://bank.example", entry: str = "/") -> None:
        self.base_url = base_url
        self.entry = entry
        self.title = "Dashboard"
        self.body = "Available Balance RM 1,250.00"
        self.clicks: list[str] = []
        self.fills: list[str] = []
        self._elements = {
            "Proceed": {"role": "button"},
            "Available Balance": {"key": "Available Balance:Available Balance RM 1,250.00"},
        }

    def elements(self):
        out = []
        for i, (name, spec) in enumerate(self._elements.items()):
            out.append(
                ElementInfo(
                    ref=f"el{i}",
                    name=name,
                    role=spec.get("role", ""),
                    tag="button" if spec.get("role") == "button" else "td",
                    table_cell_key=spec.get("key", ""),
                )
            )
        return out

    def cell_value(self, element) -> str:
        key = element.table_cell_key or ""
        if ":" in key:
            return key.split(":", 1)[1]
        return element.name

    def on_click(self, element) -> None:
        self.clicks.append(element.name)
        if element.name == "Pay Now":
            self.title = "Pay"
            self.body = "Payment sent"

    def on_fill(self, element, text: str) -> None:
        self.fills.append(text)

    def on_navigate(self, url: str) -> None:
        pass


class AlwaysMissingSurface(FakeSurface):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hidden.add("Proceed")


class FlakySurface(AlwaysMissingSurface):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._snapshots = 0

    def list_elements(self):
        self._snapshots += 1
        if self._snapshots > 2:
            self.hidden.discard("Proceed")
        return [e for e in self.world.elements() if e.name not in self.hidden]


def base_capability() -> Capability:
    return Capability(
        name="balance",
        surface=SurfaceSpec(base_url="https://bank.example", entry_path="/"),
        parameters=[ParamSpec(name="amount", type="decimal")],
        steps=[
            Step(
                description="continue to dashboard",
                action=ActionKind.CLICK,
                locator=Locator(strategy=LocatorStrategy.TEXT, value="Proceed"),
            )
        ],
        outputs=[
            OutputSpec(name="balance", locator=Locator(strategy=LocatorStrategy.TABLE_CELL, value="Available Balance:"))
        ],
        checkpoint=Checkpoint(value="Available Balance", timeout_ms=2000),
        security=SecuritySpec(allowed_domains=["bank.example"]),
    )


def session_for(cap: Capability, tmp_path: Path, **kwargs) -> ReplaySession:
    kwargs.setdefault("params", {"amount": "250.00"})
    kwargs.setdefault("operator", AutoAllowOperator())
    return ReplaySession(capability=cap, run_dir=tmp_path / "run", **kwargs)


def test_success_replay_and_outputs(tmp_path: Path):
    cap = base_capability()
    result = session_for(cap, tmp_path).run_on_surface(FakeSurface(FakeWorld()))
    assert result.outcome.value == "success"
    assert result.outputs["balance"] == "Available Balance RM 1,250.00"
    assert (tmp_path / "run" / "trace.jsonl").exists()


def test_recoverable_retry_succeeds(tmp_path: Path):
    cap = base_capability()
    cap.steps[0].on_error = ErrorPlan(disposition=ErrorDisposition.RECOVERABLE, on_transient=3)
    result = session_for(cap, tmp_path).run_on_surface(FlakySurface(FakeWorld()))
    assert result.outcome.value == "success"
    assert "Proceed" in result.outputs or result.outputs.get("balance")


def test_recoverable_retry_exhausted_hard_failure(tmp_path: Path):
    cap = base_capability()
    cap.steps[0].on_error = ErrorPlan(disposition=ErrorDisposition.RECOVERABLE, on_transient=2)
    result = session_for(cap, tmp_path).run_on_surface(AlwaysMissingSurface(FakeWorld()))
    assert result.outcome.value == "hard_failure"
    assert result.step_failed == 1


def test_business_outcome_detected(tmp_path: Path):
    cap = base_capability()
    cap.business_outcomes.append(
        BusinessOutcomeSpec(label="payment declined: account closed", expected_texts=["Payment declined"])
    )

    class DeclinedWorld(FakeWorld):
        def __init__(self):
            super().__init__()
            self.body = "Payment declined: account closed"

    result = session_for(cap, tmp_path).run_on_surface(FakeSurface(DeclinedWorld()))
    assert result.outcome.value == "business_outcome"
    assert "account closed" in result.business_outcome


def test_expected_texts_on_step_plan(tmp_path: Path):
    cap = base_capability()
    cap.steps[0].on_error = ErrorPlan(
        disposition=ErrorDisposition.EXPECTED_BUSINESS_OUTCOME,
        expected_texts=["Payment declined"],
    )

    class DeclinedWorld(FakeWorld):
        def __init__(self):
            super().__init__()
            self.body = "Payment declined: insufficient funds"

    result = session_for(cap, tmp_path).run_on_surface(FakeSurface(DeclinedWorld()))
    assert result.outcome.value == "business_outcome"


def test_checkpoint_not_met_goal_assumed(tmp_path: Path):
    cap = base_capability()

    class ErrorPageWorld(FakeWorld):
        def __init__(self):
            super().__init__()
            self.body = "Unexpected error page. Try again later."

    result = session_for(cap, tmp_path).run_on_surface(FakeSurface(ErrorPageWorld()))
    assert result.outcome.value == "stopped_goal_assumed"


def test_allowlist_violation_hard_failure(tmp_path: Path):
    cap = base_capability()
    cap.steps = [
        Step(description="navigate away", action=ActionKind.NAVIGATE, args={"url": "https://evil.example/x"})
    ]
    result = session_for(cap, tmp_path).run_on_surface(FakeSurface(FakeWorld()))
    assert result.outcome.value == "hard_failure"
    assert "allowlist" in result.message


def test_missing_required_param_rejected(tmp_path: Path):
    cap = base_capability()
    cap.parameters = [ParamSpec(name="amount", type="decimal", required=True)]
    with pytest.raises(ConfigError):
        session_for(cap, tmp_path, params={}).run_on_surface(FakeSurface(FakeWorld()))


def test_tenant_override_reroutes_entry(tmp_path: Path):
    cap = base_capability()
    cap.tenant_overrides.append(TenantOverride(tenant_id="acme-eu", entry_path="/eu"))
    surface = FakeSurface(FakeWorld(entry="/"))
    result = session_for(cap, tmp_path, tenant_id="acme-eu").run_on_surface(surface)
    assert surface.url_val.endswith("/eu")
    assert result.outcome.value == "success"


def test_escalation_gate_via_risk_policy_and_file_operator(tmp_path: Path):
    cap = base_capability()
    cap.steps[0].description = "start a payment of {param.amount}"
    cap.security.risk_policy = {"high": "escalate"}
    escalations = tmp_path / "escalations"
    writer = _DecisionDaemon(escalations)
    operator = FileOperator(escalations, timeout_s=15)
    writer.start()
    try:
        result = session_for(cap, tmp_path, operator=operator).run_on_surface(FakeSurface(FakeWorld()))
    finally:
        writer.stop()
    assert result.outcome.value == "success"
    assert (escalations / "t0-1.decision.json").exists() or len(list(escalations.glob("*.json"))) >= 1


class _DecisionDaemon:
    """Writes an 'allow' decision next to any ticket the FileOperator emits."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop = False

    def start(self) -> None:
        import json

        def loop():
            while not self._stop:
                for path in self.root.glob("t*.json"):
                    if path.name.endswith(".ticket.json"):
                        continue
                    decision = path.with_name(path.stem + ".decision.json")
                    if not decision.exists():
                        try:
                            decision.write_text(json.dumps({"decision": "allow", "note": "test-operator"}), encoding="utf-8")
                        except OSError:
                            pass
                time.sleep(0.1)

        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop = True