from __future__ import annotations

from urllib.parse import urlparse

from ..core.errors import AllowlistViolation
from ..core.models import SecuritySpec


class Allowlist:
    def __init__(self, spec: SecuritySpec) -> None:
        self.spec = spec
        self.actions = {a.value for a in spec.allowed_actions}

    def assert_url_allowed(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            raise AllowlistViolation(f"url '{url}' has no host", subject=url)
        for allowed in self.spec.allowed_domains:
            allowed = allowed.lower().lstrip("*.").lstrip(".")
            if host == allowed:
                return
            if self.spec.allowed_domains_are_suffixes and host.endswith("." + allowed):
                return
        raise AllowlistViolation(
            f"host '{host}' is not in allowlist {self.spec.allowed_domains}",
            subject=url,
        )

    def assert_route_allowed(self, path: str, permitted_prefixes: list[str]) -> None:
        for prefix in permitted_prefixes:
            if path.startswith(prefix) or prefix == "*":
                return
        if not permitted_prefixes:
            return
        raise AllowlistViolation(
            f"route '{path}' is not in permitted prefixes {permitted_prefixes}",
            subject=path,
        )

    def assert_action_allowed(self, action: str) -> None:
        if action not in self.actions:
            raise AllowlistViolation(f"action '{action}' not in allowed actions", subject=action)