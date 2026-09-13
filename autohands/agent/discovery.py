from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, sync_playwright

from ..core import errors
from ..core.log import EventLogger
from ..core.models import (
    ActionKind,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    OutputSpec,
    ParamSpec,
    SecuritySpec,
    Step,
    SurfaceSpec,
)
from ..drivers.web import PlaywrightSurface
from .llm import DryRunBrain, LLMClient

ACTION_MAP = {
    "click": ActionKind.CLICK,
    "fill": ActionKind.FILL,
    "select": ActionKind.SELECT,
    "press": ActionKind.PRESS,
    "navigate": ActionKind.NAVIGATE,
    "wait_for": ActionKind.WAIT_FOR,
    "extract": ActionKind.EXTRACT,
    "finish": None,
}

PARAM_TYPE_HINT = {
    "string": "string",
    "text": "string",
    "integer": "integer",
    "int": "integer",
    "decimal": "decimal",
    "number": "decimal",
    "float": "decimal",
    "boolean": "boolean",
    "bool": "boolean",
}


class DiscoverySession:
    def __init__(
        self,
        name: str,
        goal: str,
        url: str,
        brain: LLMClient | DryRunBrain,
        run_dir: Path,
        model_hint: str = "",
    ) -> None:
        self.name = name
        self.goal = goal
        self.url = url
        self.brain = brain
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logger = EventLogger(self.run_dir)
        self.steps: list[Step] = []
        self.outputs: list[OutputSpec] = []
        self.parameters: list[ParamSpec] = []
        self.param_examples: dict[str, str] = {}
        self.checkpoint_text = ""
        self.max_turns = int(model_hint) if model_hint else 25
        self.visited_domains: set[str] = set()

    def run(self) -> Capability:
        from urllib.parse import urlparse

        start_parsed = urlparse(self.url)
        self.visited_domains.add((start_parsed.hostname or "").lower())
        with sync_playwright() as p:
            browser: Browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            surface = PlaywrightSurface(page)
            try:
                surface.navigate(self.url)
                return self._loop(surface, start_parsed)
            finally:
                browser.close()

    def _loop(self, surface: PlaywrightSurface, start_parsed: Any) -> Capability:
        from urllib.parse import urlparse

        turns = 0
        finish: dict[str, Any] | None = None
        last_page = ""
        page_steps = 0
        while turns < self.max_turns:
            turns += 1
            elements = surface.snapshot()
            screen = surface.screenshot_bytes()
            page_key = surface.url().split("?")[0]
            if page_key == last_page:
                page_steps += 1
            else:
                last_page = page_key
                page_steps = 1
            context = {
                "step": turns,
                "page_steps": page_steps,
                "url": surface.url(),
                "title": surface.title(),
                "goal": self.goal,
                "elements": [
                    {
                        "ref": e.ref,
                        "role": e.role or e.tag,
                        "name": e.name,
                        "type": e.control_type,
                        "key": e.table_cell_key,
                        "x": e.x,
                        "y": e.y,
                    }
                    for e in elements[:120]
                ],
            }
            decision = self._decide(surface, screen, context)
            self.logger.log("agent_decision", turn=turns, decision=decision)
            action = decision.get("action")
            if action == "finish":
                finish = decision.get("done") or decision
                self.checkpoint_text = finish.get("outcome_text") or finish.get("checkpoint", "")
                break
            self._execute(surface, elements, decision, turns)
            if action in ("navigate", "click", "fill", "select", "press", "wait_for"):
                declared = decision.get("declare_parameter")
                if declared and declared.get("name") not in self.param_examples:
                    example = decision.get("text") or ""
                    self.parameters.append(
                        ParamSpec(
                            name=declared["name"],
                            type=PARAM_TYPE_HINT.get(str(declared.get("type")), "string"),
                            description=declared.get("description", ""),
                            sensitive=("pin" in declared["name"].lower() or "password" in declared["name"].lower()),
                        )
                    )
                    self.param_examples[declared["name"]] = example
                    last = self.steps[-1]
                    if isinstance(last.args.get("text"), str):
                        last.args["text"] = _substitute_example(
                            last.args["text"], {declared["name"]: example}
                        )
            self.logger.log("after_step", step=turns, url=surface.url())

        self._apply_param_rewrites(finish)
        return self._build_artifact(start_parsed, finish)

    def _decide(self, surface: PlaywrightSurface, screen: bytes, context: dict[str, Any]) -> dict[str, Any]:
        if isinstance(self.brain, DryRunBrain):
            return self.brain.decide(context)
        system = _SYSTEM_PROMPT
        messages = [{"role": "user", "content": f"Goal: {self.goal}\nCurrent URL: {context['url']}\nPage title: {context['title']}\nElement list (ref, role, name, type):\n" + _format_elements(context["elements"])}]
        messages.append(self.brain.screenshot_messages(screen, f"Current screen (step {context['step']}). Decide the next action."))
        return self.brain.complete_json(system, messages)

    def _execute(self, surface: PlaywrightSurface, elements: list[Any], decision: dict[str, Any], turn: int) -> None:
        action = decision.get("action")
        target_ref = decision.get("target_ref") or ""
        target = _find_ref(elements, target_ref)
        args: dict[str, Any] = {}
        description = decision.get("description", "")

        if action == "navigate":
            url = decision.get("text", "")
            from urllib.parse import urlparse

            self.visited_domains.add((urlparse(url).hostname or "").lower())
            surface.navigate(url)
            self.steps.append(Step(action=ActionKind.NAVIGATE, description=description, args={"url": url}))
            return

        if action == "wait_for":
            checkpoint = decision.get("checkpoint") or decision.get("text") or ""
            if checkpoint:
                surface.wait_for(checkpoint, 3000)
            self.steps.append(Step(action=ActionKind.WAIT_FOR, description=description, args={"text": checkpoint}))
            return

        if action == "press":
            surface.press_key(decision.get("text", "Enter"))
            self.steps.append(Step(action=ActionKind.PRESS, description=description, args={"key": decision.get("text", "Enter")}))
            return

        if action == "extract":
            self._record_output(surface, target, decision, description)
            return

        if target is None:
            raise errors.StepError(
                None, f"{action}", expected="a target element", observed=f"no element with ref {target_ref}",
                recoverable=True,
            )

        if action == "click":
            surface.submit_click(target)
            self.steps.append(
                Step(
                    action=ActionKind.CLICK,
                    description=description,
                    locator=_locator_for(target),
                    args={},
                )
            )
        elif action == "fill":
            surface.submit_fill(target, decision.get("text", ""))
            self.steps.append(
                Step(
                    action=ActionKind.FILL,
                    description=description,
                    locator=_locator_for(target),
                    args={"text": decision.get("text", "")},
                )
            )
        elif action == "select":
            surface.submit_select(target, decision.get("text", ""))
            self.steps.append(
                Step(
                    action=ActionKind.SELECT,
                    description=description,
                    locator=_locator_for(target),
                    args={"value": decision.get("text", "")},
                )
            )
        else:
            raise errors.StepError(None, action, expected="a known action", observed="unknown action kind", recoverable=True)

    def _record_output(self, surface: PlaywrightSurface, target: Any, decision: dict[str, Any], description: str) -> None:
        if target is None:
            return
        name = (decision.get("output") or {}).get("name") or "value"
        loc = _output_locator_for(target)
        self.outputs.append(OutputSpec(name=name, type="text", locator=loc, description=description))

    def _apply_param_rewrites(self, finish: dict[str, Any] | None) -> None:
        if not finish:
            return
        declared = finish.get("params") or {}
        for step in self.steps:
            if step.action in (ActionKind.FILL, ActionKind.SELECT):
                key = "text" if step.action == ActionKind.FILL else "value"
                value = step.args.get(key)
                if isinstance(value, str):
                    step.args[key] = _substitute_example(value, declared)

    def _build_artifact(self, start_parsed: Any, finish: dict[str, Any] | None) -> Capability:
        base_url = f"{start_parsed.scheme}://{start_parsed.netloc}"
        entry_path = start_parsed.path or "/"
        host = (start_parsed.hostname or "").lower()
        checkpoint_text = self.checkpoint_text or (finish or {}).get("outcome_text", "")
        return Capability(
            name=self.name,
            description=self.goal,
            source_goal=self.goal,
            surface=SurfaceSpec(base_url=base_url, entry_path=entry_path),
            parameters=self.parameters,
            steps=self.steps,
            outputs=self.outputs,
            checkpoint=Checkpoint(kind="text", value=checkpoint_text),
            security=SecuritySpec(
                allowed_domains=[host],
                max_steps=len(self.steps) * 2 + 5,
            ),
        )


def _substitute_example(text: str, examples: dict[str, str]) -> str:
    out = text
    for name, value in examples.items():
        if value and value in out:
            out = out.replace(value, "{" + f"param.{name}" + "}")
    return out


def _find_ref(elements: list[Any], ref: str) -> Any | None:
    if not ref:
        return None
    for element in elements:
        if element.ref == ref:
            return element
    return None


def _format_elements(elements: list[dict[str, Any]]) -> str:
    lines = []
    for element in elements:
        lines.append(
            f"[{element['ref']}] {element.get('role') or element.get('type')} :: {element.get('name', '')}"
        )
    return "\n".join(lines)


def _locator_for(element: Any) -> Locator | None:
    if not element:
        return None
    name = (getattr(element, "name", "") or "").strip()
    label = (getattr(element, "label", "") or "").strip()
    if name:
        return Locator(
            strategy=LocatorStrategy.TEXT,
            value=name[:120],
            reason="stable accessible text observed during discovery",
            fallbacks=[Locator(strategy=LocatorStrategy.LABEL, value=label[:120])] if label else [],
        )
    if label:
        return Locator(strategy=LocatorStrategy.LABEL, value=label[:120])
    return Locator(
        strategy=LocatorStrategy.CSS,
        value=(getattr(element, "css_path", "") or ""),
    )


def _output_locator_for(element: Any) -> Locator:
    key = getattr(element, "table_cell_key", "") or ""
    if key:
        header = key.rsplit(":", 1)[0] + ":"
        return Locator(
            strategy=LocatorStrategy.TABLE_CELL,
            value=header[:120],
            reason="row header prefix; value cell read fresh at replay time",
        )
    return _locator_for(element) or Locator(strategy=LocatorStrategy.TEXT, value="")


_SYSTEM_PROMPT = """You are an autonomous web agent that records a reusable, deterministic task (a "capability") by actually performing it in a live browser.

You are given the page URL, the page title, a raw element listing (ref, role, name, control-type), and a screenshot.

Reply with ONE JSON object only, choosing one action:
- {"action":"click","target_ref":<ref>,"description":"why"} — click the element (buttons, links named by text/label).
- {"action":"fill","target_ref":<ref>,"text":<value>,"description":"...","declare_parameter":{"name":<snake_name>,"type":"string","description":"..."}} — type text into the field; if this field is an input to the task (its value should become a parameter of the capability), declare it and the text you typed is its example value.
- {"action":"select","target_ref":<ref>,"text":<option text>,"description":"...","declare_parameter":{...}} — choose an option from a select/radio.
- {"action":"press","text":"Enter","description":"..."} — press a key (rare).
- {"action":"navigate","text":<absolute url>,"description":"..."} — go to a concrete URL.
- {"action":"extract","target_ref":<ref>,"output":{"name":"snake_name","value":<text shown>},"description":"..."} — pull a value that should be reported as an output (e.g. a balance). The value will be re-read at replay time.
- {"action":"wait_for","text":<expected text>,"description":"..."} — wait until text appears (for SPA transitions).
- {"action":"finish","done":{"outcome_text":<short text that will be present when replay succeeded>,"params":{<name>:<example value>},"outputs":{<name>:<example value>}}} — task goal achieved; report the confirmation text.

Rules:
- Prefer clicking elements by their visible text/label/role, not raw position.
- Only declare parameters for values an end-user of the task would input (amounts, selections, ids, credentials). Never declare the target URL as a parameter.
- Do not perform destructive irreversible actions (transfers of real money, deletions) — if the page asks for one, STOP with finish.
- Keep "checkpoint" reasoning internal; only supply text for wait_for/extract/finish.
- Reply with valid JSON only."""