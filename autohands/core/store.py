from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .errors import AutoHandsError
from .models import ARTIFACT_SCHEMA_VERSION, Capability


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, cap: Capability) -> Path:
        self._check_schema_version(cap)
        path = self.root / f"{cap.id}.json"
        path.write_text(cap.to_json(), encoding="utf-8")
        return path

    def load(self, cap_id: str) -> Capability:
        path = self.root / f"{cap_id}.json"
        if not path.exists():
            path = self.root / (cap_id if cap_id.endswith(".json") else f"{cap_id}.json")
        if not path.exists():
            raise AutoHandsError(f"no capability artifact found for {cap_id} in {self.root}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            return Capability.model_validate(payload)
        except ValidationError as exc:
            raise AutoHandsError(f"artifact {path} failed schema validation: {exc}") from exc

    def list(self) -> list[Capability]:
        caps: list[Capability] = []
        for path in sorted(self.root.glob("*.json")):
            caps.append(self.load(path.stem))
        return caps

    def _check_schema_version(self, cap: Capability) -> None:
        if cap.schema_version > ARTIFACT_SCHEMA_VERSION:
            raise AutoHandsError(
                f"artifact schema_version {cap.schema_version} is newer than supported {ARTIFACT_SCHEMA_VERSION}"
            )


def validate_capability(cap: Capability) -> list[str]:
    problems: list[str] = []
    if not cap.steps:
        problems.append("capability has no steps")
    if not cap.checkpoint.value:
        problems.append("capability has an empty checkpoint")
    if cap.schema_version != ARTIFACT_SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {cap.schema_version}")
    used_params: set[str] = set()
    for step in cap.steps:
        if step.action.value in {"fill", "select"} and not step.locator:
            problems.append(f"step '{step.description}' needs a locator")
        for value in step.args.values():
            if isinstance(value, str):
                for ref in _param_refs(value):
                    used_params.add(ref)
    declared = cap.param_ids()
    for ref in sorted(used_params - declared):
        problems.append(f"step references undeclared parameter '{ref}'")
    for spec in cap.security.allowed_domains:
        if not spec:
            problems.append("security allowlist contains an empty domain")
    return problems


def _param_refs(text: str) -> list[str]:
    out: list[str] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        end = text.find("}", start)
        if end < 0:
            break
        name = text[start + 1 : end]
        if name.startswith("param."):
            out.append(name[len("param.") :])
        index = end + 1
    return out


def render_params(text: str, values: dict[str, object], sensitive: bool = False) -> str:
    out = text
    for name, value in values.items():
        label = "***" if sensitive else str(value)
        out = out.replace("{" + f"param.{name}" + "}", label)
    return out