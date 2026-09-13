# AutoHands

A computer-use automation system. An LLM **discovers** how to drive a legacy UI that exposes no
API, records the recipe as a versioned **capability artifact**, and a deterministic engine
**replays** that artifact with no model in the loop — cheaper, faster, auditable, and safe.

```
human goal ──> LLM discovery agent ──> capability artifact ──> deterministic replay engine
                  (sees screenshots)         (JSON, versioned)      (no model, guardrails)
```

Built as the interface.ai take-home assignment. See `DESIGN.md` for the full design rationale.

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate            # (Windows PowerShell)
pip install -e .
playwright install chromium
```

Run the self-contained demo (spins up a Flask legacy-style bank app locally, seeds two
capabilities via a **scripted dry-run brain** so no API key is needed, then replays 5 scenarios):

```bash
python -m autohands run-demo --operator-mode auto
```

Demo replays (all deterministic, no model):

| replay | scenario | outcome |
|---|---|---|
| `01-balance-success` | happy path | `success`, `available_balance = "Available Balance RM 1,250.00"` |
| `02-balance-transient` | injected transient error on the legacy gate | `success` after recoverable retry |
| `03-balance-unavailable` | balance service down | `business_outcome = "balance service temporarily unavailable"` |
| `04-pay-escalated` | high-risk payment step | `success` via human operator escalation (file- or auto-approved) |
| `05-pay-closed` | payment on a closed account | `business_outcome = "payment declined: account closed"` |

Evidence for every run — screenshots, URL, DOM fingerprint, structured trace — is written to
`artifacts/runs/<label>/`.

## CLI

```
python -m autohands discover   --name check-balance --goal "read the available balance" \
                               --url http://127.0.0.1:5000/ --dry-run
python -m autohands list
python -m autohands replay     --artifact check-balance --params amount=250.00
python -m autohands replay     --artifact check-balance --entry "/?fault=busy" \
                               --operator file --escalate-root escalations
python -m autohands console    --port 5100
python -m autohands run-demo   --operator-mode auto
```

- `discover` — run the live agent. `--dry-run` uses the scripted brain; otherwise it calls OpenAI
  (`OPENAI_API_KEY` in `.env`, `OPENAI_DRY_RUN=1` forces the scripted brain). A real discovery
  produces screenshots + reasoning steps as evidence in the run directory.
- `replay` — deterministic replay of a published capability. `--operator auto|file|console`
  chooses who answers escalation tickets (auto-approve in a sandbox, a file-backed queue consumed
  by the operator console, or interactive console).
- `console` — the human operator approval web UI (port 5100 default).
- `run-demo` — full demo, `--operator-mode file` (default) waits for file decisions before
  proceeding, use `--decide-after N` to auto-approve pending tickets after N seconds.

## Tests

```bash
.venv\Scripts\python.exe -m pytest -q
```

38 unit tests cover schema validation, parameter id rendering, locator cascade resolution,
allowlist/redaction/risk classification, and full replay dispositions (success, recoverable retry,
hard failure, business outcome, escalation gate, tenant override) against a browser-free
`FakeSurface`.

## Layout

```
autohands/           package
  core/              models (schema v1), store, logging, errors
  safety/            allowlist, redaction, risk classification
  drivers/           surface abstraction + Playwright web driver
  agent/             LLM client + discovery agent (produces artifacts)
  replay/            deterministic replay engine + operators (auto/file/console)
  demo/              demo seeding + 5-replay orchestration
demo_app/            legacy Flask bank app + operator console
tests/               pytest suite (no browser needed)
artifacts/
  capabilities/      published capability artifacts (JSON)
  runs/              evidence per run (screenshots, fingerprints, traces)
escalations/         operator ticket queue (file-backed)
```