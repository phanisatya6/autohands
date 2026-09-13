from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from ..core import errors
from ..core.models import ActionResult, ElementInfo
from .surface import Surface

SENSITIVE_ATTR = ("password", "secret", "token", "pass", "pin", "cvv")


def _is_sensitive_input(el: Any) -> bool:
    type_attr = (el.get_attribute("type") or "").lower()
    name_attr = (el.get_attribute("name") or "").lower()
    id_attr = (el.get_attribute("id") or "").lower()
    if type_attr == "password":
        return True
    haystack = " ".join([type_attr, name_attr, id_attr])
    return any(term in haystack for term in SENSITIVE_ATTR)


def _accessible_name(el: Any) -> str:
    aria = el.get_attribute("aria-label") or ""
    if aria:
        return aria
    label_el = el.evaluate_handle("""e => {
        const id = e.getAttribute('id');
        if (id) {
            const label = document.querySelector(`label[for="${id}"]`);
            if (label) return label.textContent;
        }
        let p = e.closest('label');
        if (p) return p.textContent;
        return null;
    }""")
    try:
        label_text = label_el.as_element().inner_text() if label_el and label_el.as_element() else ""
    except Exception:
        label_text = ""
    if label_text:
        return label_text.strip()
    placeholder = el.get_attribute("placeholder") or ""
    if placeholder:
        return placeholder
    text = (el.inner_text() or "").strip()
    return text.split("\n")[0] if text else ""


def _control_type(el: Any) -> str:
    tag = (el.evaluate("e => e.tagName") or "").lower()
    if tag == "input":
        return el.get_attribute("type") or "text"
    return tag


def _css_path(el: Any) -> str:
    parts: list[str] = []
    try:
        for node in el.evaluate_handle(
            """e => {
            const out = [];
            let cur = e;
            while (cur && cur.nodeType === 1 && out.length < 12) {
                let sel = cur.tagName.toLowerCase();
                const id = cur.getAttribute('id');
                if (id) { out.unshift("#" + id); break; }
                if (cur.getAttribute('data-testid')) { sel += `[data-testid="${cur.getAttribute('data-testid')}"]`; }
                else if (cur.getAttribute('name')) { sel += `[name="${cur.getAttribute('name')}"]`; }
                out.unshift(sel);
                cur = cur.parentElement;
            }
            return out;
        }"""
        ).json_value():
            parts.append(node)
    except Exception:
        pass
    return " ".join(parts) or f"ref:{_stable_ref(el)}"


def _stable_ref(el: Any) -> str:
    payload = {
        "tag": el.evaluate("e => e.tagName"),
        "id": el.get_attribute("id"),
        "name": el.get_attribute("name"),
        "text": (el.inner_text() or "")[:80],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10]


class PlaywrightSurface(Surface):
    def __init__(self, page: Page) -> None:
        self.page = page
        self.refs: dict[str, Any] = {}
        self.ref_selectors: dict[str, str] = {}

    def list_elements(self) -> list[ElementInfo]:
        return self.snapshot()

    def navigate(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_load_state("networkidle", timeout=15000)

    def url(self) -> str:
        return self.page.url

    def title(self) -> str:
        return self.page.title()

    def snapshot(self) -> list[ElementInfo]:
        elements = self.page.evaluate(
            """() => {
            const out = [];
            const seen = new Set();
            const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && el.offsetParent !== null;
            };
            const query = 'button, a[href], input, textarea, select, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="option"], [role="text"], h1, h2, h3, td, th';
            document.querySelectorAll(query).forEach((el) => {
                if (seen.has(el)) return;
                if (!isVisible(el)) return;
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'body' || tag === 'html') return;
                const text = (el.innerText || (el.textContent || '')).trim().slice(0, 200);
                let accName = el.getAttribute('aria-label') || '';
                if (!accName) {
                    const tagL = tag;
                    if (tagL === 'input' || tagL === 'textarea' || tagL === 'select') {
                        accName = el.getAttribute('placeholder') || '';
                    }
                    if (!accName) {
                        const forId = el.getAttribute('id');
                        if (forId) {
                            const lbl = document.querySelector('label[for="' + forId + '"]');
                            if (lbl) accName = (lbl.textContent || '').trim();
                        }
                    }
                    if (!accName) accName = text;
                }
                const name = accName.slice(0, 120);
                if (text === '' && !['input','textarea','select','button','a'].includes(tag)) return;
                const r = el.getBoundingClientRect();
                const type = ('type' in el && el.type) ? el.type : (tag === 'input' ? 'text' : tag);
                const header = (row, cell) => {
                    const table = row && row.closest ? row.closest('table') : null;
                    if (!table) return null;
                    const thRow = table.querySelector('thead tr') || table.querySelector('tr');
                    if (!thRow) return null;
                    const headers = Array.from(thRow.querySelectorAll('th, td')).map(th => (th.textContent || '').trim());
                    const tds = Array.from(row.cells || []);
                    const idx = tds.indexOf(cell);
                    return headers[idx] || (idx >= 0 ? String(idx) : '');
                };
                let rowKey = '';
                if (tag === 'td') {
                    const row = el.parentElement;
                    const h = header(row, el);
                    if (h !== null) rowKey = h + ':' + text.slice(0, 40);
                }
                const payload = {
                    ref: el.___ref || (el.___ref = Math.random().toString(16).slice(2, 10)),
                    tag,
                    type,
                    id: el.getAttribute('id') || '',
                    cls: (el.getAttribute('class') || '').slice(0, 200),
                    nameAttr: el.getAttribute('name') || '',
                    role: el.getAttribute('role') || (tag === 'button' ? 'button' : (tag === 'a' ? 'link' : '')),
                    label: el.getAttribute('aria-label') || '',
                    name,
                    text,
                    rowKey,
                    x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                };
                seen.add(el);
                out.push(payload);
            });
            return out;
        }"""
        )
        infos: list[ElementInfo] = []
        for raw in elements:
            tag = raw["tag"]
            role = raw.get("role")
            name = raw.get("name") or raw["text"] or raw["label"]
            info = ElementInfo(
                ref=raw["ref"],
                role=role,
                tag=tag,
                control_type=str(raw.get("type") or tag),
                name=name[:120],
                label=raw.get("label") or "",
                value="",
                visible=True,
                x=raw["x"],
                y=raw["y"],
                w=raw["w"],
                h=raw["h"],
                id_attr=raw.get("id") or "",
                class_name=raw.get("cls") or "",
                table_cell_key=raw.get("rowKey") or "",
            )
            if raw.get("id"):
                self.ref_selectors[info.ref] = f"#{raw['id']}"
            elif raw.get("nameAttr"):
                self.ref_selectors[info.ref] = f"{tag}[name={raw['nameAttr']}]"
            else:
                self.ref_selectors[info.ref] = ""
            infos.append(info)
        return infos

    def _el_by_ref(self, element: ElementInfo) -> Any:
        if element.ref in self.refs:
            handle = self.refs[element.ref]
            if handle and handle.is_connected():
                return handle
        selector = self.ref_selectors.get(element.ref, "")
        if selector:
            handle = self.page.query_selector(selector)
            if handle is not None:
                self.refs[element.ref] = handle
                return handle
        handle = self._query_by_ref(element.ref)
        if handle is not None:
            self.refs[element.ref] = handle
            return handle
        handle = self._query_js(element)
        if handle is not None:
            self.refs[element.ref] = handle
            return handle
        raise errors.LocatorFailure(
            None,
            f"element ref {element.ref}",
            "a connected element",
            "no live element matched the recorded descriptor",
        )

    def _query_by_ref(self, ref: str) -> Any:
        return self.page.evaluate_handle(
            f"""(ref) => {{
                const q = 'button, a[href], input, textarea, select, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [role="tab"], td, th, h1, h2, h3';
                for (const el of document.querySelectorAll(q)) {{
                    if (el.___ref === ref) return el;
                }}
                return null;
            }}""",
            ref,
        )

    def _query_js(self, element: ElementInfo) -> Any:
        cx = element.x + element.w // 2
        cy = element.y + element.h // 2
        min_distance = 40
        args = json.dumps({"cx": cx, "cy": cy, "name": (element.name or ""), "min": min_distance})
        handle = self.page.evaluate_handle(
            f"""(spec) => {{
                const cx = spec.cx, cy = spec.cy, name = spec.name, min = spec.min;
                const isVisible = (el) => {{
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && el.offsetParent !== null;
                }};
                let best = null, bestD = min;
                const q = 'button, a[href], input, textarea, select, [role="button"], [role="link"], [role="textbox"], td, th, h1, h2, h3';
                for (const el of document.querySelectorAll(q)) {{
                    if (!isVisible(el)) continue;
                    const t = ((el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.textContent || '') || '').trim().slice(0, 200);
                    if (name && t.indexOf(name) === -1) continue;
                    const r = el.getBoundingClientRect();
                    const ecx = r.x + r.width / 2, ecy = r.y + r.height / 2;
                    const d = Math.hypot(ecx - cx, ecy - cy);
                    if (d < bestD) {{ bestD = d; best = el; }}
                }}
                return best;
            }}""",
            json.loads(args),
        )
        if handle is None:
            return None
        element_handle = handle.as_element()
        if element_handle is None:
            return None
        return element_handle

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass

    def submit_click(self, element: ElementInfo) -> ActionResult:
        el = self._el_by_ref(element)
        el.scroll_into_view_if_needed()
        el.click(force=True)
        self._settle()
        return ActionResult(message="clicked")

    def submit_fill(self, element: ElementInfo, text: str) -> ActionResult:
        el = self._el_by_ref(element)
        el.scroll_into_view_if_needed()
        el.click(force=True)
        el.fill(text)
        self._settle()
        return ActionResult(message="filled")

    def submit_select(self, element: ElementInfo, option: str) -> ActionResult:
        el = self._el_by_ref(element)
        el.select_option(label=option)
        self._settle()
        return ActionResult(message=f"selected {option}")

    def submit_press(self, key: str) -> ActionResult:
        self.page.keyboard.press(key)
        self._settle()
        return ActionResult(message=f"pressed {key}")

    def press_key(self, key: str) -> ActionResult:
        return self.submit_press(key)

    def read_text(self, element: ElementInfo, attribute: str = "text") -> str:
        el = self._el_by_ref(element)
        if attribute == "value":
            return el.input_value()
        if attribute == "href":
            return el.get_attribute("href") or ""
        return (el.inner_text() or "").strip()

    def screenshot(self, path: str) -> None:
        self.page.screenshot(path=path, full_page=True)

    def screenshot_bytes(self, width: int = 900) -> bytes:
        from io import BytesIO

        viewport = self.page.viewport_size or {"width": 1440, "height": 900}
        ratio = width / viewport["width"]
        target = {"width": width, "height": int(viewport["height"] * ratio)}
        return self.page.screenshot(full_page=False, clip={"x": 0, "y": 0, **target})

    def dom_dump(self) -> str:
        return self.page.content()

    def fingerprint(self) -> dict[str, Any]:
        try:
            digest = self.page.evaluate(
                """() => {
                const els = document.querySelectorAll('body, main, form, section');
                const text = Array.from(els).map(e => (e.innerText || '').slice(0, 400)).join('|');
                return {
                    hash: (function(){ let h=0; for (let i=0;i<text.length;i++){ h=((h<<5)-h)+text.charCodeAt(i); h|=0; } return (h>>>0).toString(16); })(),
                    url: location.href,
                    title: document.title,
                };
            }"""
            )
        except Exception as exc:
            digest = {"url": self.page.url, "hash": f"error:{exc}", "title": ""}
        digest["time"] = time.time()
        return digest

    def dump_fingerprint(self, path: str) -> None:
        import json as _json

        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(self.fingerprint(), fh, indent=2)

    def wait_for(self, text: str, timeout_ms: int = 15000) -> bool:
        try:
            self.page.wait_for_selector(f"text={text}", timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            return False

    def expect_text(self, text: str, timeout_ms: int = 15000) -> bool:
        try:
            self.page.wait_for_selector(f"text={text}", timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            return False

    def page_find_text(self, text: str) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    "(t) => (document.body ? document.body.innerText : '').includes(t)", text
                )
            )
        except Exception:
            return False

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass