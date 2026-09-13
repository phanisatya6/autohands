from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..safety.redact import log_redacted


class EventLogger:
    def __init__(
        self,
        run_dir: Path,
        sensitive_params: set[str] | None = None,
        redact: bool = True,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "trace.jsonl"
        self.sensitive = set(sensitive_params or [])
        self.do_redact = redact

    def log(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {"ts": time.time(), "event": event, **fields}
        if self.do_redact:
            payload = log_redacted(payload)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")

    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out