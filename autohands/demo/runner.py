from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ..agent.discovery import DiscoverySession
from ..agent.llm import DryRunBrain
from ..core import errors
from ..core.models import (
    Capability,
    ErrorDisposition,
    ErrorPlan,
    BusinessOutcomeSpec,
)
from ..core.store import ArtifactStore
from ..replay.engine import AutoAllowOperator, FileOperator, Operator, ReplaySession

ROOT = Path(__file__).resolve().parent.parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_demo_server(port: int):
    from demo_app.app import create_app

    app = create_app()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def patch_artifact(cap: Capability) -> Capability:
    """Install the demo error-handling plans (what a human reviewer would do before publishing)."""
    for step in cap.steps:
        if step.action.value == "click" and step.locator and step.locator.value == "Proceed":
            step.on_error = ErrorPlan(disposition=ErrorDisposition.RECOVERABLE, on_transient=15)
        if step.action.value in ("click", "fill", "select"):
            if step.on_error is None:
                step.on_error = ErrorPlan(disposition=ErrorDisposition.RECOVERABLE, on_transient=3)
    if cap.name == "check-balance":
        cap.checkpoint.value = "RM"
        cap.business_outcomes.append(
            BusinessOutcomeSpec(label="balance service temporarily unavailable", expected_texts=["Balance temporarily unavailable"])
        )
    if cap.name == "pay-bill":
        cap.business_outcomes.append(
            BusinessOutcomeSpec(label="payment declined: account closed", expected_texts=["Payment declined"])
        )
    cap.security.max_steps = max(cap.security.max_steps, 40)
    return cap


class DemoState:
    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.store = ArtifactStore(self.workdir / "artifacts" / "capabilities")
        self.runs_dir = self.workdir / "artifacts" / "runs"
        self.escalations_dir = self.workdir / "escalations"


def seed_demo(port: int, state: DemoState, force: bool = False) -> list[Capability]:
    base = f"http://127.0.0.1:{port}"
    wanted = [
        ("check-balance", "Read the currently available balance shown on the dashboard page and report it.", "/"),
        ("pay-bill", "Make a payment of 250.00 MYR from the Zara account using the Pay Now flow, without submitting personal data.", "/"),
    ]
    seeded: list[Capability] = []
    for name, goal, entry in wanted:
        existing = next((c for c in state.store.list() if c.name == name), None)
        if existing is not None and not force:
            existing.surface.base_url = base
            existing.surface.entry_path = entry
            seeded.append(patch_artifact(existing))
            state.store.save(existing)
            continue
        discovery_dir = state.runs_dir / f"seed-{name}"
        session = DiscoverySession(name=name, goal=goal, url=f"{base}{entry}", brain=DryRunBrain(), run_dir=discovery_dir)
        cap = session.run()
        cap = patch_artifact(cap)
        state.store.save(cap)
        seeded.append(cap)
    return seeded


def _run(
    cap: Capability,
    state: DemoState,
    label: str,
    entry: str,
    params: dict[str, Any] | None = None,
    operator: Operator | None = None,
) -> Any:
    base = cap.surface.base_url
    effective = cap.model_copy(deep=True)
    effective.surface.entry_path = entry
    run_dir = state.runs_dir / label
    session = ReplaySession(
        capability=effective,
        params=params or {},
        run_dir=run_dir,
        operator=operator or AutoAllowOperator(),
    )
    result = session.run()
    print(f"[{label}] outcome={result.outcome.value} business_outcome={result.business_outcome!r} message={result.message[:140]}")
    if result.outputs:
        print(f"           outputs={result.outputs}")
    print(f"           evidence -> {run_dir}")
    return result


def run_demo(timeout_s: int = 600, operator_mode: str = "file", decide_after: float | None = None) -> None:
    workdir = ROOT
    state = DemoState(workdir)
    state.runs_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    server = threading.Thread(target=_start_demo_server, args=(port,), daemon=True)
    server.start()
    time.sleep(1.2)

    print("== seeding capability artifacts via scripted discovery (no LLM needed) ==")
    caps = seed_demo(port, state, force=False)
    caps_by_name = {c.name: c for c in caps}

    balance: Capability = caps_by_name["check-balance"]

    print("\n== replay 1/5: success replay of check-balance (deterministic, no model) ==")
    _run(balance, state, "01-balance-success", "/")

    print("\n== replay 2/5: injected transient error, recoverable retry (fault=busy on gate) ==")
    _run(balance, state, "02-balance-transient", "/?fault=busy")

    print("\n== replay 3/5: injected business outcome (balance service unavailable) ==")
    _run(balance, state, "03-balance-unavailable", "/?fault=unavailable")

    pay: Capability = caps_by_name["pay-bill"]

    print("\n== replay 4/5: risky action triggers human escalation on the operator console ==")
    print("    'Pay Now' and 'Authorize Payment' are classified high-risk -> runtime asks an operator.")
    escalations = state.escalations_dir
    escalations.mkdir(parents=True, exist_ok=True)
    operator: Operator
    if operator_mode == "auto":
        operator = AutoAllowOperator()
    elif decide_after is not None:
        autos = threading.Timer(decide_after, _write_all_allow, args=(escalations,))
        autos.daemon = True
        autos.start()
        operator = FileOperator(escalations, timeout_s=timeout_s)
    else:
        operator = FileOperator(escalations, timeout_s=timeout_s)
    print("    Watch the operator console (run `autohands escalate-console`) to approve or reject.")
    _run(pay, state, "04-pay-escalated", "/", params={"amount": "250.00"}, operator=operator)

    print("\n== replay 5/5: injected business outcome during payment (account closed) ==")
    _run(pay, state, "05-pay-closed", "/?fault=closed", params={"amount": "250.00"})


def _write_all_allow(root: Path) -> None:
    root = Path(root)
    if not root.exists():
        return
    for path in root.glob("*.json"):
        if path.name.endswith(".decision.json"):
            continue
        decision_path = root / f"{path.stem}.decision.json"
        if not decision_path.exists():
            import json

            decision_path.write_text(
                json.dumps({"decision": "allow", "note": "auto-approved by demo timer"}), encoding="utf-8"
            )


def _find_capability_by_name(state: DemoState, name: str) -> Capability:
    for cap in state.store.list():
        if cap.name == name:
            return patch_artifact(cap)
    raise errors.ConfigError(f"no capability named {name!r} in {state.store.root}")


# re-export for the CLI
find_capability = _find_capability_by_name