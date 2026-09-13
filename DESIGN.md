# DESIGN

This document explains the architecture and the key decisions for the AutoHands system. It maps
the assignment requirements ("discovery", "reproducible artifact", "deterministic replay") onto the
code, and records what was deliberately cut.

> Assignments this is answering: interface.ai take-home. Reproduced from the extracted brief:
> an LLM-driven agent that runs clicks; the output is a flexible, durable metadata file; the
> replay the next day must still succeed even if the click positions changes; and covers
> deterministic availability semantics, multi-tenant, heterogeneity, escalations, and recording.

---

## #1 Architecture

Three phases, two of which involve the model:

```
DISCOVERY (LLM in the loop)          REPLAY (no model)
┌──────────────────────────┐         ┌──────────────────────────┐
│ human goal               │         │ capability artifact      │
│   │                      │         │   │                      │
│ DiscoverySession         │         │ ReplaySession            │
│   • LLMClient brain      │  JSON   │   • Allowlist gate       │
│   • screenshot each page │ ──────► │   • risk gate/escalate   │
│   • resolves locators    │ artifact │   • step executor       │
│   • emits capability     │         │   • outputs extraction   │
│                          │         │   • checkpoint verify    │
└──────────────────────────┘         └──────────────────────────┘
```

Key idea: **the artifact is the product, not the model.** Discovery spends whatever tokens it
needs; replay spends zero tokens and runs on any machine with the artifact + a browser driver.
Because replay is deterministic, results are reproducible, cheap to run at scale, and easy to
audit — there is no "which prompt produced this run" ambiguity.

Module map:

- `autohands/agent/discovery.py` — `DiscoverySession`: walks pages, calls the brain with the goal,
  current page snapshot + visible elements, receives a plan of steps keyed to locators, then
  executes/records each step and extracts output locators. Non-dry-run runs save screenshots and
  reasoning as discovery evidence.
- `autohands/agent/llm.py` — `LLMClient` (OpenAI chat completions, vision on screenshots) and
  `DryRunBrain` (scripted determinant brain used by `--dry-run` and the demo so the whole flow runs
  with no API key, yet still exercises every discovery code path).
- `autohands/core/models.py` — the artifact schema (see #2).
- `autohands/replay/engine.py` — `ReplaySession`: navigation, per-step gating, execution,
  retry/error handling, output extraction, checkpoint verification, evidence capture, and
  operator escalation. Abstracts the surface so the same engine runs headless browsers or an
  in-memory `FakeSurface` in tests (`run_on_surface`).
- `autohands/drivers/*` — `Surface` (abstract action/observation interface) and `PlaywrightSurface`
  (browser implementation; snapshot captures roles, accessible names, table-cell keys, CSS paths,
  and coordinates).
- `autohands/safety/*` — allowlist, redaction, risk classification (see #6).
- `autohands/demo/runner.py` + `demo_app/` — the legacy-style demo bank app and the 5-scenario
  orchestration used by `run-demo`.

The engine never blocks on the network or on model latency during replay; the only waits are
bounded retry sleeps and the checkpoint `timeout_ms`, so a replay has a hard, predictable cost.

---

## #2 Artifact schema

A capability is a standalone JSON file (`schema_version: 1`) that fully describes one automated
task. It is validated on load (`extra: "forbid"`), so unknown fields fail loudly instead of being
silently ignored. `autohands/core/store.py` also rejects loading artifacts from future schema
versions.

```jsonc
{
  "id": "8f3a1c09c2ab",          // stable across re-publishing
  "name": "pay-bill",
  "schema_version": 1,           // artifact format version, not capability version
  "version": "1.0.0",
  "source_goal": "Make a payment of 250.00 MYR ...",
  "surface": { "base_url": "http://127.0.0.1:53881", "entry_path": "/" },
  "parameters": [
    { "name": "amount", "type": "decimal", "required": true,
      "description": "Payment amount", "sensitive": false }
  ],
  "steps": [
    { "id": "…", "action": "click", "description": "choose Pay Now",
      "locator": { "strategy": "role_name", "value": "link Pay Now",
                   "fallbacks": [ {"strategy": "position", "value": "420,180"} ] },
      "args": {}, "on_error": { "disposition": "recoverable", "on_transient": 3 } },
    { "id": "…", "action": "fill", "locator": { "strategy": "label", "value": "amount" },
      "args": { "text": "{param.amount}" } }
  ],
  "outputs": [
    { "name": "available_balance", "type": "text",
      "locator": { "strategy": "table_cell", "value": "Available Balance:" } }
  ],
  "checkpoint": { "kind": "text", "value": "RM", "timeout_ms": 15000 },
  "business_outcomes": [
    { "label": "balance service temporarily unavailable",
      "expected_texts": ["Balance temporarily unavailable"] } ],
  "security": { "allowed_domains": ["bank.example"], "allowed_actions": ["navigate","click",…],
                "max_steps": 40, "risk_policy": {} },
  "tenant_overrides": [ { "tenant_id": "acme-eu", "entry_path": "/eu", "base_url": null } ]
}
```

Locators are **semantic, not positional**: `role_name`, `text`, `label`, `table_cell`, `css`, and
`position`. The replay resolver (`autohands/drivers/surface.py`) tries the semantic strategy first
and only falls back to coordinates as a last resort, which keeps replays alive when the page layout
shifts. A `position` locator is always accompanied by `reason: "why"` so a human can review it.

Parameters are declared (name/type/required/sensitive) and referenced in step args as
`{param.name}`; `store.render_params` substitutes them and raises on undeclared references during
validation. Sensitive parameters are redacted from traces and never echoed to operators.

`on_error` plans and `business_outcomes` are first-class schema fields, because "what should happen
when the happy path is unavailable" is part of the task contract, not an afterthought.

---

## #3 Determinism & error handling

Replay is deterministic: same artifact + same parameters + same surface state ⇒ same run, with
every decision logged in `trace.jsonl`. The model is never in the loop, so there is no sampling,
no tool-choice drift, and no nondeterministic latency.

Failure handling is governed by a small taxonomy of dispositions (`core/errors.py`,
`models.py::ErrorDisposition`):

- **Recoverable** — transient conditions (element briefly absent, spinner, not-yet-updated table).
  The engine retries with a bounded sleep up to `on_transient` attempts, then records a hard
  failure with evidence.
- **Expected business outcome** — the task partially ran and the surface indicates a *definite*
  terminal condition (e.g. "Payment declined: account closed"). These are expressed either on a
  step (`on_error.expected_texts`) or on the capability (`business_outcomes`). Replay stops with
  `outcome=business_outcome` and the label as the machine-read result — not an exception.
- **Hard failure** — a genuine bug/missing element with the expectation text captured for
  debugging.
- **Escalate** — a step that is risky or whose resolution a human should own (see #5).

Order matters: recoverable primitives are retried *before* any business outcome match is
consulted, so a truly transient state does not get misreported as a terminal outcome; the business
outcome check additionally runs on hard-fail and checkpoint-miss as a final sweep
(`_check_business_outcome`).

Retry behavior was validated by the demo: `02-balance-transient` injects a gate fault that hides
the expected element on first load; replay clicks through and succeeds, while
`03-balance-unavailable` (same artifact, a different injected state) cleanly reports the business
outcome.

A note on "the same click the next day": the demo replays the *same* artifact against pages whose
coordinates shift by injected faults; because locators are semantic (`table_cell`, `role_name`),
the replay still lands. THIS is the property the assignment asks for.

---

## #4 Heterogeneity & multi-tenant

**Heterogeneity** is handled at the driver boundary, not in the engine. `Surface` is a small
interface (`list_elements`, `click`/`fill`/`select`, `read_text`, `navigate`, `wait_for`,
`page_find_text`, `screenshot`, `fingerprint`). `PlaywrightSurface` is the web implementation; the
replay tests use `FakeSurface`, so the full engine logic (retry, escalation, outcomes, checkpoints,
output extraction) is exercised with no browser. Adding a desktop or mobile surface is a new
implementation of the same interface; capability artifacts don't change.

Legacy web surfaces get extra extraction affordances: the driver snapshot builds a
`table_cell_key` (`label:value`) for `<td>` cells so a balance that only exists inside an
unlabeled table is locatable without JS, and it reads accessible `name`s with placeholder/label
fallbacks — the things a legacy app actually exposes.

**Multi-tenant**: the same artifact is reused across tenants via `tenant_overrides`, which can
swap `base_url` and/or `entry_path` per `tenant_id` (`engine._resolved_surface`). Discovery runs
once on a reference tenant; every other tenant replays without re-discovery. A replay invocation
passes `--tenant acme-eu`, and the runtime resolves the right surface. This keeps one artifact per
task (reviews, versioning, observability accrue to one object) instead of N copies.

---

## #5 Escalation & handoff

Escalation is a **human decision queue**, not a wall. The risk gate (see #6) marks a step as
`high`; the engine then issues a ticket to an `Operator` and *pauses* execution until a decision —
the same pause-and-resume contract a human agent would need.

Three operators (`replay/engine.py`):

- `AutoAllowOperator` — approves immediately (sandboxes, demos, low-assurance environments).
- `FileOperator` — writes `escalations/<ticket>.json` and polls for
  `escalations/<ticket>.decision.json`. The human operator console (`demo_app/console.py`, run via
  `autohands console`) lists pending tickets and writes decisions. Decisions are polled, so the
  console can be a separate process or machine.
- `ConsoleOperator` — interactive approve/reject/retry on stdin.

The ticket carries the context a human needs: capability name, step index, action, description,
risk classification, and the current URL. Evidence (screenshots) lives alongside. On `abort`, the
engine raises `EscalationRequested` and the run ends `outcome=escalation_requested`; on `allow`,
execution resumes at the exact step that paused. The demo proves the full cycle:
`04-pay-escalated` raises two high-risk payment steps and only completes after tickets are
approved.

---

## #6 Safety

Four guardrails, each implemented as an independent module so they are individually auditable and
testable:

1. **Network allowlist** (`safety/allowlist.py`) — every navigation URL is checked against
   `security.allowed_domains`; navigation to a foreign host aborts the run before any action.
   Wildcard/suffix matches are explicit (`*.bank.example`), and there is no implicit scheme
   bypass (the check is on the host).

2. **Action allowlist** (`security.allowed_actions`) — only declared action kinds can execute;
   `assert_action_allowed` runs before every step. This makes an artifact self-describing about
   what it is *allowed* to do, not just what it does.

3. **Risk classification** (`safety/risk.py`) — free-text descriptions and rendered parameters are
   scanned for high-risk vocabulary (payment, transfer, delete, authorize…, and sensitive fields
   like pin/cvv/account_number). The engine logs every gate decision (`risk_gate` event) so policy
   enforcement is observable, not implicit.

4. **Redaction** (`safety/redact.py`) — sensitive keys (`password`, `token`, `pin`, `cvv`, …) and
   16-digit-card-like values are rewritten to `***` before any event hits the trace. `ReplaySession`
   knows each capability's sensitive parameters (`sensitive=True`) and seeds them into the log
   redactor, so a secret never lands in `trace.jsonl`, evidence fingerprints, or escalation
   tickets.

Plus: `max_steps` bounds the artifact (no unbounded loops), and `validate_capability()` rejects
artifacts that reference undeclared parameters, empty step lists, or future schema versions.

---

## #7 Cuts

Deliberately not built (and why):

- **Multi-step/stateful auth inside discovery** — logging in via OTP or MFA is a real-world
  blocker but out of scope; artifacts assume an already-authenticated surface or a `base_url` that
  points behind SSO. The `Surface` seam is where a session-reuse driver would slot in.
- **A true model-provider-agnostic agent framework (Anthropic/Hugging Face tools)**
  — `LLMClient` is a thin OpenAI wrapper; the interface (`brain`) is already decoupled, so a second
  provider is a small addition, but only one is wired.
- **Scheduling / retry-across-days / queue semantics** — replay is synchronous per invocation;
  running 1,000 tenants daily is a `for` loop over `ReplaySession`, not a scheduler.
- **Webhook/auth-based operator console** — FileOperator polling exchanges are JSON files by
  design for transparency; a real service would put the same queue on S3/Redis.
- **Full self-repair of a broken artifact** — if a locator no longer exists and every fallback
  fails, replay reports a hard failure with evidence; an automatic re-discovery retry loop was cut
  for predictability (nondeterministic, costly, and it can mask real regressions).
- **Idempotency keys / exactly-once side effects** — payment idempotency belongs to the
  application being driven; the capability artifact can express it as a step (e.g. fill
  "reference" with `{params.txnId}`) but the engine does not invent it.