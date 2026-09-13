from __future__ import annotations

import base64
import json
import os
from typing import Any

from ..core.errors import ConfigError


class LLMClient:
    """Thin wrapper over an OpenAI-compatible chat-completions endpoint."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str = "gpt-4o-mini") -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "") or None
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        if not self.api_key:
            raise ConfigError(
                "OPENAI_API_KEY is not set. Add it to .env or pass --api-key. "
                "Set OPENAI_DRY_RUN=1 to use the scripted demo brain instead."
            )
        try:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        except Exception as exc:
            raise ConfigError(f"cannot initialize OpenAI client: {exc}") from exc

    def complete_json(
        self,
        system: str,
        user_messages: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in user_messages:
            content: Any = message["content"]
            if isinstance(content, str):
                messages.append({"role": message["role"], "content": content})
                continue
            if isinstance(content, list):
                messages.append({"role": message["role"], "content": content})
                continue
            messages.append({"role": message["role"], "content": str(content)})
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        raw = (response.choices[0].message.content or "").strip()
        return _extract_json(raw)

    def screenshot_messages(self, image_bytes: bytes, caption: str) -> dict[str, Any]:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        image_url = f"data:image/png;base64,{encoded}"
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"model returned non-JSON: {raw[:200]!r}: {exc}") from exc


class DryRunBrain:
    """Scripted brain used for tests and for OPENAI_DRY_RUN=1 demos.

    It simulates a competent agent for the demo surfaces so the demo can be
    shown without an API key: it names elements from the snapshot by label
    and stops exploring once the goal is achieved.
    """

    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        elements = context.get("elements", [])
        url = context.get("url", "")
        title = context.get("title", "")
        page_steps = context.get("page_steps", 1)
        goal = context.get("goal", "").lower()
        wants_payment = any(keyword in goal for keyword in ("pay", "transfer", "payment"))

        def find(hint: str) -> dict[str, Any] | None:
            needle = hint.lower()
            for element in elements:
                name = (element.get("name") or "").lower()
                label = (element.get("label") or "").lower()
                text = (element.get("text") or "").lower()
                key = (element.get("key") or "").lower()
                if needle in name or needle in label or needle in text or needle in key:
                    return element
            return None

        def find_with_key(hint: str) -> dict[str, Any] | None:
            needle = hint.lower()
            keyed = [e for e in elements if (e.get("key") or "").lower().startswith(needle)]
            if keyed:
                return sorted(keyed, key=lambda e: (e.get("y", 0), e.get("x", 0)))[0]
            return find(hint)

        if "/login" in url or (title and "sign in" in title.lower()):
            btn = find("proceed") or find("login") or find("sign in")
            if btn:
                return {"action": "click", "target_ref": btn["ref"], "text": "", "description": "continue to dashboard from the legacy gate"}
            gate = find("enter") or find("continue")
            if gate:
                return {"action": "click", "target_ref": gate["ref"], "text": "", "description": "authenticate at the gate"}
            return {"action": "wait_for", "target_ref": "", "text": "", "description": "wait for gate to settle"}
        if "/dashboard" in url or "dashboard" in title.lower():
            if not wants_payment and page_steps == 1:
                bal = find_with_key("available balance:") or find_with_key("available balance") or find("balance")
                if bal:
                    return {"action": "extract", "target_ref": bal["ref"], "text": "", "description": "read available balance", "output": {"name": "available_balance", "value": bal.get("text", "")}}
            if wants_payment:
                lnk = find("pay now") or find("make payment")
                if lnk:
                    return {"action": "click", "target_ref": lnk["ref"], "text": "", "description": "start a payment"}
            return {"action": "finish", "text": "", "description": "goal achieved",
                    "done": {"outcome_text": "Available Balance visible", "params": {}, "outputs": {}}}
        if "/pay" in url or "payment" in title.lower() and wants_payment:
            amt = find("amount")
            if amt and page_steps == 1:
                return {"action": "fill", "target_ref": amt["ref"], "text": "250.00", "description": "enter payment amount", "declare_parameter": {"name": "amount", "type": "decimal", "description": "Payment amount"}}
            ctx_badge = find("settle") or find("authorize") or find("confirm")
            if ctx_badge:
                return {"action": "click", "target_ref": ctx_badge["ref"], "text": "", "description": "authorize the payment"}
            return {"action": "finish", "text": "", "description": "payment page reached",
                    "done": {"outcome_text": "Payment sent", "params": {"amount": "250.00"}, "outputs": {}}}
        if "/result" in url or "result" in title.lower() and wants_payment:
            if "decline=1" in url:
                return {"action": "finish", "text": "", "description": "payment rejected by business rule",
                        "done": {"outcome_text": "Payment declined", "params": {"amount": "250.00"}, "outputs": {}}}
            return {"action": "finish", "text": "", "description": "payment recorded",
                    "done": {"outcome_text": "Payment sent", "params": {"amount": "250.00"}, "outputs": {}}}
        return {"action": "wait_for", "target_ref": "", "text": "", "description": "no-op observation turn"}