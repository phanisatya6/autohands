from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import errors
from .core.models import Capability
from .core.store import ArtifactStore, validate_capability

ROOT = Path(__file__).resolve().parent.parent


def _store() -> ArtifactStore:
    return ArtifactStore(ROOT / "artifacts" / "capabilities")


def _load_capability(store: ArtifactStore, artifact: str) -> Capability:
    candidate = Path(artifact)
    if candidate.exists():
        import json as _json

        return Capability.model_validate(_json.loads(candidate.read_text(encoding="utf-8")))
    try:
        return store.load(artifact)
    except errors.AutoHandsError:
        pass
    for cap in store.list():
        if cap.name == artifact:
            return cap
    raise errors.ConfigError(f"no capability found for {artifact!r}")


def cmd_discover(args: argparse.Namespace) -> int:
    from .agent.discovery import DiscoverySession
    from .agent.llm import DryRunBrain, LLMClient
    from .core.models import Capability as _Capability

    if not args.name or not args.goal or not args.url:
        print("discover requires --name, --goal and --url", file=sys.stderr)
        return 2
    from .demo.runner import patch_artifact

    store = _store()
    run_dir = Path(args.run_dir) if args.run_dir else ROOT / "artifacts" / "runs" / f"discover-{args.name}"
    if args.dry_run:
        brain: Any = DryRunBrain()
    else:
        brain = LLMClient(api_key=args.api_key)
    session = DiscoverySession(name=args.name, goal=args.goal, url=args.url, brain=brain, run_dir=run_dir)
    cap = patch_artifact(session.run())
    problems = validate_capability(cap)
    path = store.save(cap)
    print(f"discovered capability: id={cap.id} name={cap.name}")
    print(f"artifact written: {path}")
    print(f"steps={len(cap.steps)} parameters={[p.name for p in cap.parameters]} outputs={[o.name for o in cap.outputs]}")
    if problems:
        print("validation warnings:", *problems, sep="\n  - ", file=sys.stderr)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .demo.runner import _write_all_allow
    from .replay.engine import AutoAllowOperator, ConsoleOperator, FileOperator, ReplaySession

    store = _store()
    cap = _load_capability(store, args.artifact)
    params = dict(args.params or [])
    run_dir = Path(args.run_dir) if args.run_dir else ROOT / "artifacts" / "runs" / f"run-{cap.id[:8]}"
    if args.entry:
        cap = cap.model_copy(deep=True)
        cap.surface.entry_path = args.entry
    if args.operator == "auto":
        operator: Any = AutoAllowOperator()
    elif args.operator == "console":
        operator = ConsoleOperator()
    else:
        escalations = Path(args.escalate_root) if args.escalate_root else ROOT / "escalations"
        if args.decide_after is not None:
            import threading

            timer = threading.Timer(float(args.decide_after), _write_all_allow, args=(escalations,))
            timer.daemon = True
            timer.start()
        operator = FileOperator(escalations, timeout_s=int(args.timeout))
    session = ReplaySession(
        capability=cap,
        params=params,
        run_dir=run_dir,
        operator=operator,
        tenant_id=args.tenant,
        headless=not args.headful,
    )
    result = session.run()
    print(json.dumps(result.model_dump(mode="json", exclude_none=True), indent=2))
    print(f"evidence: {run_dir}")
    return 0 if result.outcome.value in ("success", "business_outcome") else 1


def cmd_list(args: argparse.Namespace) -> int:
    store = _store()
    for cap in store.list():
        print(f"{cap.id:12} {cap.name:24} v{cap.version:6} steps={len(cap.steps)} params={[p.name for p in cap.parameters]}")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    from demo_app.console import start

    root = Path(args.escalate_root) if args.escalate_root else ROOT / "escalations"
    return_code = [0]

    def _serve():
        start(root, port=args.port)

    import threading

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    print(f"operator console running: http://127.0.0.1:{args.port}  (root={root})")
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    return return_code[0]


def cmd_run_demo(args: argparse.Namespace) -> int:
    from .demo.runner import run_demo

    run_demo(timeout_s=int(args.timeout), operator_mode=args.operator_mode, decide_after=args.decide_after)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autohands", description="LLM-discovered, deterministic computer-use capabilities")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="run the live agent to build a capability artifact")
    p.add_argument("--name", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--api-key", default=None)
    p.add_argument("--dry-run", action="store_true", help="use the scripted brain (no API key)")
    p.add_argument("--run-dir", default=None)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("replay", help="deterministically replay a published capability (no model)")
    p.add_argument("--artifact", required=True, help="capability id, name, or path to artifact JSON")
    p.add_argument("--params", nargs="*", default=[], metavar="K=V")
    p.add_argument("--tenant", default=None)
    p.add_argument("--entry", default=None, help="override entry path (e.g. '/?fault=busy')")
    p.add_argument("--run-dir", default=None)
    p.add_argument("--operator", choices=["auto", "file", "console"], default="auto")
    p.add_argument("--escalate-root", default=None)
    p.add_argument("--timeout", default="900")
    p.add_argument("--decide-after", default=None, help="auto-allow escalations after N seconds")
    p.add_argument("--headful", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("list", help="list recorded capability artifacts")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("console", help="run the human operator escalation console")
    p.add_argument("--port", type=int, default=5100)
    p.add_argument("--escalate-root", default=None)
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("run-demo", help="full deterministic demo with injected errors + escalation")
    p.add_argument("--operator-mode", choices=["file", "auto"], default="file")
    p.add_argument("--decide-after", type=float, default=None, help="auto-allow pending escalations after N sec")
    p.add_argument("--timeout", default="900")
    p.set_defaults(func=cmd_run_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except errors.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except errors.AutoHandsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())