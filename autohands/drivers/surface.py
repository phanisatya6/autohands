from __future__ import annotations

from typing import Any

from ..core import errors
from ..core.models import ElementInfo, Locator, LocatorStrategy


class Surface:
    """Abstract live surface. Subclasses submit actions and observe state."""

    def submit_click(self, element: ElementInfo) -> dict[str, Any]:
        raise NotImplementedError

    def submit_fill(self, element: ElementInfo, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def submit_select(self, element: ElementInfo, option: str) -> dict[str, Any]:
        raise NotImplementedError

    def read_text(self, element: ElementInfo) -> str:
        raise NotImplementedError

    def list_elements(self) -> list[ElementInfo]:
        raise NotImplementedError


class LocatorResolver:
    """Resolves a Locator spec against a surface by cascading strategies."""

    def __init__(self, surface: Surface) -> None:
        self.surface = surface

    def resolve(self, locator: Locator) -> ElementInfo:
        candidates = [locator, *locator.fallbacks]
        failures: list[str] = []
        for candidate in candidates:
            try:
                return self._one(candidate)
            except errors.LocatorFailure as exc:
                failures.append(f"{candidate.strategy.value}={candidate.value} ({exc.observed})")
        raise errors.LocatorFailure(
            None,
            f"resolve {locator.strategy.value}={locator.value}",
            expected="an element matching the locator cascade",
            observed="; ".join(failures) or "no candidates",
            recoverable=True,
        )

    def _one(self, locator: Locator) -> ElementInfo:
        elements = self.surface.list_elements()
        matcher = _MATCHERS[locator.strategy]
        for element in elements:
            if matcher(element, locator.value):
                return element
        for element in elements:
            if _matches_fuzzy(element, locator.strategy, locator.value):
                return element
        raise errors.LocatorFailure(
            None,
            f"locator {locator.strategy.value}={locator.value}",
            expected="an element",
            observed="no match among visible elements",
            recoverable=True,
        )


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _matches_role_name(element: ElementInfo, value: str) -> bool:
    return _normalize(element.role + " " + element.name) == _normalize(value)


def _matches_text(element: ElementInfo, value: str) -> bool:
    return _normalize(element.name) == _normalize(value) or value in element.name


def _matches_label(element: ElementInfo, value: str) -> bool:
    return value in (element.label or "") or _normalize(element.label or "") == _normalize(value)


def _matches_css(element: ElementInfo, value: str) -> bool:
    return element.css_path == value or element.id_attr == value.lstrip("#") or element.class_name == value.lstrip(".")


def _matches_table_cell(element: ElementInfo, value: str) -> bool:
    if not element.table_cell_key:
        return False
    if value.endswith(":"):
        return element.table_cell_key.startswith(value)
    return element.table_cell_key == value


def _matches_position(element: ElementInfo, value: str) -> bool:
    # value is "x,y" in element coordinates; matches the element whose center is nearest.
    if "," not in value:
        return False
    tx, _, ty = value.partition(",")
    try:
        tx, ty = int(tx), int(ty)
    except ValueError:
        return False
    cx = element.x + element.w // 2
    cy = element.y + element.h // 2
    return abs(cx - tx) < 8 and abs(cy - ty) < 8


_MATCHERS = {
    LocatorStrategy.ROLE_NAME: _matches_role_name,
    LocatorStrategy.TEXT: _matches_text,
    LocatorStrategy.LABEL: _matches_label,
    LocatorStrategy.CSS: _matches_css,
    LocatorStrategy.TABLE_CELL: _matches_table_cell,
    LocatorStrategy.POSITION: _matches_position,
}


def _matches_fuzzy(element: ElementInfo, strategy: LocatorStrategy, value: str) -> bool:
    if strategy in (LocatorStrategy.TEXT, LocatorStrategy.LABEL, LocatorStrategy.ROLE_NAME):
        return value.lower() in _normalize(element.name + " " + element.label + " " + element.value)
    return False