from __future__ import annotations

import pytest

from autohands.core import errors
from autohands.core.models import (
    ActionKind,
    ElementInfo,
    Locator,
    LocatorStrategy,
    SecuritySpec,
)
from autohands.drivers.surface import LocatorResolver
from autohands.safety.allowlist import Allowlist
from autohands.safety.redact import redact
from autohands.safety.risk import classify_risk, route_decision


# ---------- elements ----------

def el(ref: str, name: str, role: str = "", key: str = "") -> ElementInfo:
    return ElementInfo(ref=ref, name=name, role=role, table_cell_key=key)


class StubSurface:
    def __init__(self, elements) -> None:
        self._elements = list(elements)

    def list_elements(self):
        return list(self._elements)


def test_locator_cascade_fallback():
    surface = StubSurface([el("a", "Sign in now", role="link")])
    resolver = LocatorResolver(surface)
    found = resolver.resolve(
        Locator(
            strategy=LocatorStrategy.TEXT,
            value="This does not exist",
            fallbacks=[Locator(strategy=LocatorStrategy.TEXT, value="Sign in")],
        )
    )
    assert found.ref == "a"


def test_locator_text_substring():
    surface = StubSurface([el("b", "Available Balance RM 1,250.00")])
    resolver = LocatorResolver(surface)
    found = resolver.resolve(Locator(strategy=LocatorStrategy.TEXT, value="Available Balance"))
    assert found.ref == "b"


def test_locator_table_cell_prefix():
    surface = StubSurface([el("b", "balance cell", key="Available Balance:Available Balance RM 1,250.00")])
    resolver = LocatorResolver(surface)
    found = resolver.resolve(Locator(strategy=LocatorStrategy.TABLE_CELL, value="Available Balance:"))
    assert found.ref == "b"


def test_locator_missing_raises_recoverable():
    surface = StubSurface([el("a", "other", key="Other:1")])
    resolver = LocatorResolver(surface)
    with pytest.raises(errors.LocatorFailure) as exc:
        resolver.resolve(Locator(strategy=LocatorStrategy.TEXT, value="Nope"))
    assert exc.value.recoverable is True


# ---------- allowlist ----------

def _security(allowed=None, actions=None):
    return SecuritySpec(
        allowed_domains=allowed or ["bank.example"],
        allowed_actions=actions or [
            ActionKind.CLICK,
            ActionKind.FILL,
            ActionKind.NAVIGATE,
            ActionKind.EXTRACT,
        ],
    )


def test_allowlist_suffix_ok():
    allow = Allowlist(_security())
    allow.assert_url_allowed("https://sub.bank.example/deep/path")


def test_allowlist_exact_ok():
    allow = Allowlist(_security(["bank.example"]))
    allow.assert_url_allowed("https://bank.example/x")


def test_allowlist_blocks_foreign():
    allow = Allowlist(_security(["bank.example"]))
    with pytest.raises(errors.AllowlistViolation):
        allow.assert_url_allowed("https://evil.example/")


def test_allowlist_scheme_agnostic():
    allow = Allowlist(_security(["bank.example"]))
    allow.assert_url_allowed("http://bank.example/")  # allowlist governs host, not scheme


def test_allowlist_blocks_disallowed_action():
    allow = Allowlist(_security(actions=[ActionKind.CLICK]))
    with pytest.raises(errors.AllowlistViolation):
        allow.assert_action_allowed(ActionKind.FILL.value)


def test_allowlist_requires_host():
    allow = Allowlist(_security())
    with pytest.raises(errors.AllowlistViolation):
        allow.assert_url_allowed("javascript:alert(1)")


# ---------- risk ----------

def test_risk_pay_is_high():
    assert classify_risk("click", "start a payment", domain="bank.example") == "high"


def test_risk_painful_words_high():
    assert classify_risk("click", "finalize and submit the transfer", domain="bank.example") == "high"


def test_risk_plain_click_is_low():
    assert classify_risk("click", "continue to dashboard from the legacy gate", domain="bank.example/login") == "low"


def test_risk_domain_url_no_scheme_matters():
    assert classify_risk("click", "continue", domain="bank.example") == "low"


def test_risk_sensitive_param_is_high():
    assert classify_risk("fill", "enter the amount", params={"pin": "1234"}) == "high"


def test_route_decision_defaults():
    assert route_decision("low", {}) == "allow"
    assert route_decision("medium", {}) == "allow"
    assert route_decision("high", {}) == "escalate"


# ---------- redaction ----------

def test_redact_sensitive_keys():
    event = {"password": "hunter2", "token": "abc", "name": "Alice", "nested": {"pin": "1234"}}
    out = redact(event)
    assert out["password"] == "***"
    assert out["token"] == "***"
    assert out["nested"]["pin"] == "***"
    assert out["name"] == "Alice"


def test_redact_16_digit():
    event = {"card": "4111 1111 1111 1111"}
    assert redact(event)["card"] == "***"


# ---------- error taxonomy ----------

def test_error_taxonomy():
    base = errors.StepError(1, "step", "expected", "observed")
    assert isinstance(base, errors.AutoHandsError)
    assert not base.recoverable
    assert base.business_outcome == ""

    assert issubclass(errors.LocatorFailure, errors.StepError)
    assert issubclass(errors.BusinessOutcome, errors.StepError)
    assert issubclass(errors.RecoverableFailure, errors.StepError)

    biz = errors.BusinessOutcome(2, "pay", "success", "Payment declined", business_outcome="Payment declined")
    assert biz.business_outcome == "Payment declined"
    assert "step 2" in str(biz)