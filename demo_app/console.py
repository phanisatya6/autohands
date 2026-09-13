from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, jsonify, request


def create_app(root: Path) -> Flask:
    root = Path(root)
    app = Flask(__name__)

    def tickets() -> list[dict]:
        out = []
        if root.exists():
            for path in sorted(root.glob("*.json")):
                if path.name.endswith(".decision.json"):
                    continue
                ticket = json.loads(path.read_text(encoding="utf-8"))
                ticket["id"] = path.stem
                ticket["decision"] = None
                decision_path = root / f"{path.stem}.decision.json"
                if decision_path.exists():
                    ticket["decision"] = json.loads(decision_path.read_text(encoding="utf-8"))
                out.append(ticket)
        return out

    @app.get("/")
    def index():
        return jsonify({"tickets": tickets(count=False)})

    @app.get("/tickets")
    def list_tickets():
        data = tickets()
        for ticket in data:
            ticket.pop("context_screenshot", None)
        return jsonify({"tickets": data})

    @app.get("/tickets/<ticket_id>")
    def detail(ticket_id: str):
        for ticket in tickets():
            if ticket["id"] == ticket_id:
                return jsonify(ticket)
        return jsonify({"error": "not found"}), 404

    @app.post("/tickets/<ticket_id>/decide")
    def decide(ticket_id: str):
        decision = request.get_json(silent=True) or {}
        choice = str(decision.get("decision", "")).strip().lower()
        if choice not in ("allow", "abort", "retry"):
            return jsonify({"error": "decision must be allow|abort|retry"}), 400
        payload = {"decision": choice, "note": decision.get("note", ""), "at": __import__("time").time()}
        (root / f"{ticket_id}.decision.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        return jsonify(payload)

    return app


def start(root: Path, port: int = 5100) -> Flask:
    app = create_app(root)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    return app


if __name__ == "__main__":
    start(Path("escalations"))