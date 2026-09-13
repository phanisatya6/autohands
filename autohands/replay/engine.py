from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Browser, sync_playwright

from ..core import errors
from ..core.log import EventLogger
from ..core.models import (
    ActionKind,
    Capability,
    Locator,
    RunOutcome,
    RunResult,
    SecuritySpec,
)
from ..drivers.surface import LocatorResolver
from ..drivers.web import PlaywrightSurface
from ..safety.allowlist import Allowlist
from ..safety.risk import classify_risk, route_decision

EscalationDecision = dict[str, Any]


class Operator:
    def request(self, ticket_id: str, context: dict[str, Any]) -> EscalationDecision:
        raise NotImplementedError


class AutoAllowOperator(Operator):
    def request(self, ticket_id: str, context: dict[str, Any]) -> EscalationDecision:
        return {"decision": "allow", "note": "auto-approved"}


class FileOperator(Operator):
    def __init__(self, root: Path, timeout_s: int = 900) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s

    def request(self, ticket_id: str, context: dict[str, Any]) -> EscalationDecision:
        ticket_path = self.root / f"{ticket_id}.json"
        ticket_path.write_text(json.dumps(context, default=str, indent=2), encoding="utf-8")
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            decision_path = self.root / f"{ticket_id}.decision.json"
            if decision_path.exists():
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
                decision_path.unlink()
                return decision
            time.sleep(2)
        return {"decision": "abort", "note": "operator timeout"}


class ConsoleOperator(Operator):
    def request(self, ticket_id: str, context: dict[str, Any]) -> EscalationDecision:
        print("\n--- ESCALATION TICKET", ticket_id, "---")
        print(json.dumps(context, default=str, indent=2)[:2000])
        print("--- type allow / abort / retry ---")
        while True:
            answer = input("operator decision: ").strip().lower()
            if answer in ("allow", "abort", "retry"):
                return {"decision": answer, "note": "entered on operator console"}
            print("invalid; expected allow|abort|retry")


class ReplaySession:
    def __init__(
        self,
        capability: Capability,
        params: dict[str, Any],
        run_dir: Path,
        operator: Operator | None = None,
        tenant_id: str | None = None,
        headless: bool = True,
    ) -> None:
        self.capability = capability
        self.params = params
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logger = EventLogger(
            self.run_dir,
            sensitive_params={p.name for p in capability.parameters if p.sensitive},
        )
        self.operator = operator or AutoAllowOperator()
        self.tenant_id = tenant_id
        self.headless = headless
        self.rendered: dict[str, str] = {}
        self.elapsed_ms = 0

    def run(self) -> RunResult:
        started = time.time()
        self._validate_params()
        base_url, entry_path = _resolved_surface(self.capability, self.tenant_id)
        self.allowlist = Allowlist(self.capability.security)
        self._render_params({p.name: self.params[p.name] for p in self.capability.parameters})
        with sync_playwright() as p:
            browser: Browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, user_agent=self.capability.surface.user_agent or None)
            self.surface = PlaywrightSurface(page)
            try:
                return self._run_on(self.surface, base_url, entry_path, started)
            finally:
                browser.close()

    def run_on_surface(self, surface: Any) -> RunResult:
        """Run against a caller-provided surface (used by unit tests, no browser)."""
        started = time.time()
        self._validate_params()
        base_url, entry_path = _resolved_surface(self.capability, self.tenant_id)
        self.allowlist = Allowlist(self.capability.security)
        self._render_params({p.name: self.params[p.name] for p in self.capability.parameters})
        self.surface = surface
        return self._run_on(surface, base_url, entry_path, started)

    def _run_on(self, surface: Any, base_url: str, entry_path: str, started: float) -> RunResult:
        try:
            result = self._replay(base_url, entry_path)
        except errors.EscalationRequested as esc:
            result = RunResult(
                outcome=RunOutcome.ESCALATION_REQUESTED,
                message=f"escalated ticket {esc.request_id}: {esc.message}",
                evidence_dir=str(self.run_dir),
                elapsed_ms=int((time.time() - started) * 1000),
            )
        except errors.StepError as exc:
            (self.run_dir / "failure.json").write_text(
                json.dumps(
                    {
                        "step": exc.step_index,
                        "expected": exc.expected,
                        "observed": exc.observed,
                        "recoverable": exc.recoverable,
                        "business_outcome": exc.business_outcome,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            result = RunResult(
                outcome=RunOutcome.BUSINESS_OUTCOME if exc.business_outcome else RunOutcome.HARD_FAILURE,
                message=str(exc),
                business_outcome=exc.business_outcome,
                step_failed=exc.step_index,
                step_failed_description=exc.step_description,
                expected=exc.expected,
                observed=exc.observed,
                evidence_dir=str(self.run_dir),
                elapsed_ms=int((time.time() - started) * 1000),
            )
        except errors.AllowlistViolation as exc:
            result = RunResult(
                outcome=RunOutcome.HARD_FAILURE,
                message=f"allowlist violation: {exc}",
                expected="all activity within allowed domains",
                observed=exc.subject,
                evidence_dir=str(self.run_dir),
                elapsed_ms=int((time.time() - started) * 1000),
            )
        except Exception as exc:  # pragma: no cover - defensive
            (self.run_dir / "failure.json").write_text(
                json.dumps({"error": str(exc), "type": type(exc).__name__}, indent=2),
                encoding="utf-8",
            )
            result = RunResult(
                outcome=RunOutcome.HARD_FAILURE,
                message=f"unexpected engine error: {exc}",
                evidence_dir=str(self.run_dir),
                elapsed_ms=int((time.time() - started) * 1000),
            )
        result.elapsed_ms = result.elapsed_ms or int((time.time() - started) * 1000)
        self.logger.log(
            "run_finished",
            outcome=result.outcome,
            message=result.message,
            elapsed_ms=result.elapsed_ms,
        )
        return result

    def _validate_params(self) -> None:
        missing = [
            p.name for p in self.capability.parameters if p.required and p.name not in self.params
        ]
        if missing:
            raise errors.ConfigError(f"missing required parameters: {missing}")

    def _render_params(self, values: dict[str, Any]) -> None:
        from ..core.store import render_params

        self.rendered = {name: render_params(str(value) if not isinstance(value, str) else value, values) for name, value in values.items()}

    def _replay(self, base_url: str, entry_path: str) -> RunResult:
        entry_url = f"{base_url.rstrip('/')}{entry_path or '/'}"
        self.surface.navigate(entry_url)
        self.logger.log("session_start", url=entry_url, capability=self.capability.id, version=self.capability.version)

        try:
            self.allowlist.assert_url_allowed(entry_url)
        except errors.AllowlistViolation:
            raise

        outputs: dict[str, Any] = {}
        max_steps = self.capability.security.max_steps
        step_index = 0
        for step_index, step in enumerate(self.capability.steps, start=1):
            if step_index > max_steps:
                return RunResult(
                    outcome=RunOutcome.STOPPED_MAX_STEPS,
                    message=f"exceeded max_steps={max_steps}",
                    step_failed=step_index,
                    step_failed_description=step.description,
                    evidence_dir=str(self.run_dir),
                )
            self._gate(step_index, step)
            result = self._run_step(step_index, step, outputs)
            if result is not None:
                return result
        result = self._extract_outputs(outputs)
        return self._verify_checkpoint(result)

    def _gate(self, step_index: int, step: Any) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(self.surface.url())
        domain = f"{parsed.netloc}{parsed.path}"
        risk = classify_risk(
            step.action.value,
            step.description,
            domain=domain,
            params=self.rendered,
        )
        decision = route_decision(risk, self.capability.security.risk_policy)
        self.logger.log("risk_gate", step=step_index, risk=risk, decision=decision, description=step.description)
        if decision == "block":
            raise errors.StepError(
                step_index, step.description, expected="action within policy", observed="blocked by risk policy", recoverable=False,
            )
        if decision == "escalate":
            self._escalate(step_index, step, risk)

    def _escalate(self, step_index: int, step: Any, risk: str) -> None:
        ticket_id = f"t{int(time.time())}-{step_index}"
        context = {
            "capability": self.capability.name,
            "step": step_index,
            "action": step.action.value,
            "description": step.description,
            "risk": risk,
            "url": self.surface.url(),
        }
        self.logger.log("escalation_requested", ticket_id=ticket_id, **context)
        decision = self.operator.request(ticket_id, context)
        self.logger.log("escalation_resolved", ticket_id=ticket_id, decision=decision)
        if decision.get("decision") == "abort":
            raise errors.EscalationRequested(ticket_id, "operator aborted execution", context)

    def _run_step(self, step_index: int, step: Any, outputs: dict[str, Any]) -> RunResult | None:
        self.logger.log("step_start", step=step_index, action=step.action.value, description=step.description)
        try:
            return self._execute_step(step_index, step, outputs)
        except errors.BusinessOutcome as exc:
            self._evidence(step_index, "business_outcome")
            return RunResult(
                outcome=RunOutcome.BUSINESS_OUTCOME,
                message=exc.business_outcome or exc.observed,
                business_outcome=exc.business_outcome or exc.observed,
                outputs=outputs,
                step_failed=step_index,
                step_failed_description=step.description,
                expected=exc.expected,
                observed=exc.observed,
                evidence_dir=str(self.run_dir),
            )
        except errors.StepError as exc:
            return self._resolve_failure(step_index, step, exc, outputs)

    def _resolve_failure(
        self, step_index: int, step: Any, first: errors.StepError, outputs: dict[str, Any]
    ) -> RunResult:
        plan = step.on_error
        if plan is None:
            self._evidence(step_index, "failure")
            return RunResult(
                outcome=RunOutcome.HARD_FAILURE,
                message=str(first),
                outputs=outputs,
                step_failed=step_index,
                step_failed_description=step.description,
                expected=first.expected,
                observed=first.observed,
                evidence_dir=str(self.run_dir),
            )
        if first.recoverable and plan.disposition.value in ("recoverable", "hard_failure"):
            attempts = max(int(plan.on_transient), 1)
            for attempt in range(1, attempts + 1):
                self.logger.log("retry", step=step_index, attempt=attempt)
                self.surface.page.wait_for_timeout(800)
                try:
                    return self._execute_step(step_index, step, outputs)
                except errors.StepError as again:
                    if not again.recoverable:
                        first = again
                        break
                    first = again
            return self._fail(step_index, step, first, outputs, "retries exhausted")
        outcome_text = _find_expected(self.surface, plan.expected_texts)
        if outcome_text:
            self._evidence(step_index, "business_outcome")
            return RunResult(
                outcome=RunOutcome.BUSINESS_OUTCOME,
                message=outcome_text,
                business_outcome=outcome_text,
                outputs=outputs,
                step_failed=step_index,
                step_failed_description=step.description,
                expected=plan.guard or "success condition",
                observed=outcome_text,
                evidence_dir=str(self.run_dir),
            )
        if plan.disposition.value == "escalate":
            self._escalate(step_index, step, "high")
            try:
                return self._execute_step(step_index, step, outputs)
            except errors.StepError as again:
                return self._fail(step_index, step, again, outputs, "escalation did not resolve")
        return self._fail(step_index, step, first, outputs, "plan disposition")

    def _fail(
        self, step_index: int, step: Any, exc: errors.StepError, outputs: dict[str, Any], phase: str
    ) -> RunResult:
        hit = self._check_business_outcome()
        if hit:
            self._evidence(step_index, "business_outcome")
            return RunResult(
                outcome=RunOutcome.BUSINESS_OUTCOME,
                message=hit,
                business_outcome=hit,
                outputs=outputs,
                step_failed=step_index,
                step_failed_description=step.description,
                expected=step.on_error.guard or phase if step.on_error else phase,
                observed=hit,
                evidence_dir=str(self.run_dir),
            )
        self._evidence(step_index, "failure")
        result = RunResult(
            outcome=RunOutcome.HARD_FAILURE,
            message=f"{phase}: {exc}",
            outputs=outputs,
            step_failed=step_index,
            step_failed_description=step.description,
            expected=exc.expected,
            observed=exc.observed,
            evidence_dir=str(self.run_dir),
        )
        self.logger.log("hard_failure", phase=phase, **result.model_dump(mode="json"))
        return result

    def _check_business_outcome(self) -> str:
        for spec in self.capability.business_outcomes:
            for text in spec.expected_texts:
                if text and self.surface.page_find_text(text):
                    return spec.label or text
        for step in self.capability.steps:
            plan = step.on_error
            if plan is None or not plan.expected_texts:
                continue
            for text in plan.expected_texts:
                if text and self.surface.page_find_text(text):
                    return text
        return ""

    def _execute_step(self, step_index: int, step: Any, outputs: dict[str, Any]) -> RunResult | None:
        action = step.action.value
        description = step.description
        self.allowlist.assert_action_allowed(action)

        if action == ActionKind.NAVIGATE.value:
            url = _render(step.args.get("url", ""), self.rendered)
            self.allowlist.assert_url_allowed(url)
            self.surface.navigate(url)
            self._evidence(step_index, "after")
            return None

        if action == ActionKind.WAIT_FOR.value:
            text = _render(step.args.get("text", ""), self.rendered)
            if text and not self.surface.wait_for(text, 10000):
                raise errors.StepError(step_index, description, expected=f"wait for '{text}'", observed="timeout waiting", recoverable=True)
            self._evidence(step_index, "after")
            return None

        if action == ActionKind.PRESS.value:
            self.surface.press_key(step.args.get("key", "Enter"))
            self._evidence(step_index, "after")
            return None

        if not step.locator:
            raise errors.StepError(step_index, description, expected="a locator", observed="step has no locator", recoverable=False)

        element = self._resolve(step_index, step)
        if action == ActionKind.CLICK.value:
            self.surface.submit_click(element)
            self._evidence(step_index, "after")
            return None

        if action == ActionKind.FILL.value:
            text = _render(str(step.args.get("text", "")), self.rendered)
            self.surface.submit_fill(element, text)
            self._evidence(step_index, "after")
            return None

        if action == ActionKind.SELECT.value:
            value = _render(str(step.args.get("value", "")), self.rendered)
            self.surface.submit_select(element, value)
            self._evidence(step_index, "after")
            return None

        if action == ActionKind.EXTRACT.value:
            value = self.surface.read_text(element, step.args.get("attribute", "text"))
            outputs[step.description] = value
            self.logger.log("extracted", step=step_index, key=step.description, value=value)
            self._evidence(step_index, "extract")
            return None

        raise errors.StepError(step_index, description, expected="a supported action", observed=f"unsupported action {action}", recoverable=False)

    def _resolve(self, step_index: int, step: Any) -> Any:
        resolver = LocatorResolver(self.surface)
        try:
            return resolver.resolve(step.locator)
        except errors.StepError as exc:
            raise errors.StepError(step_index, step.description, expected=exc.expected, observed=exc.observed, recoverable=exc.recoverable) from exc

    def _extract_outputs(self, outputs: dict[str, Any]) -> RunResult:
        resolver = LocatorResolver(self.surface)
        for spec in self.capability.outputs:
            try:
                element = resolver.resolve(spec.locator)
                value = self.surface.read_text(element, spec.attribute)
                outputs[spec.name] = value
                self.logger.log("output", name=spec.name, value=value)
            except errors.StepError as exc:
                outputs[spec.name] = ""
                self.logger.log("output_missing", name=spec.name, observed=exc.observed)
        return RunResult(outcome=RunOutcome.SUCCESS, outputs=outputs, evidence_dir=str(self.run_dir), message="capability completed")

    def _verify_checkpoint(self, result: RunResult) -> RunResult:
        cp = self.capability.checkpoint
        if cp.kind == "url_contains":
            if cp.value and cp.value in self.surface.url():
                return result
        else:
            if cp.value and self.surface.wait_for(cp.value, cp.timeout_ms):
                return result
            if cp.value:
                has = self.surface.page_find_text(cp.value)
                if has:
                    return result
        self._evidence(0, "checkpoint_fail")
        self.logger.log("checkpoint_not_met", expected=cp.value)
        hit = self._check_business_outcome()
        if hit:
            return RunResult(
                outcome=RunOutcome.BUSINESS_OUTCOME,
                message=hit,
                business_outcome=hit,
                outputs=result.outputs,
                evidence_dir=str(self.run_dir),
                expected=cp.value,
                observed=hit,
            )
        return RunResult(
            outcome=RunOutcome.STOPPED_GOAL_ASSUMED if result.outputs else RunOutcome.HARD_FAILURE,
            message=f"final checkpoint not met: expected '{cp.value}' but not observed",
            outputs=result.outputs,
            evidence_dir=str(self.run_dir),
            expected=cp.value,
            observed=self.surface.url(),
        )

    def _evidence(self, step_index: int, phase: str) -> None:
        try:
            step_dir = self.run_dir / f"step-{step_index:03d}"
            step_dir.mkdir(parents=True, exist_ok=True)
            self.surface.screenshot(str(step_dir / f"{phase}.png"))
            (step_dir / "fingerprint.json").write_text(
                json.dumps(self.surface.fingerprint(), indent=2), encoding="utf-8"
            )
            (step_dir / "url.txt").write_text(self.surface.url(), encoding="utf-8")
        except Exception as exc:
            self.logger.log("evidence_error", step=step_index, error=str(exc))
        self.logger.log("evidence_captured", step=step_index, phase=phase)


def _resolved_surface(capability: Capability, tenant_id: str | None) -> tuple[str, str]:
    base_url = capability.surface.base_url
    entry_path = capability.surface.entry_path
    if tenant_id:
        for override in capability.tenant_overrides:
            if override.tenant_id == tenant_id:
                base_url = override.base_url or base_url
                entry_path = override.entry_path or entry_path
    return base_url, entry_path


def _render(text: str, rendered: dict[str, str]) -> str:
    out = text
    for name, value in rendered.items():
        out = out.replace("{" + f"param.{name}" + "}", value if value is not None else "")
    return out


def _find_expected(surface: PlaywrightSurface, texts: list[str]) -> str:
    for text in texts:
        if text and surface.page_find_text(text):
            return text
    return ""