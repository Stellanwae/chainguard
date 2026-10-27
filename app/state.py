"""Investigation state: question, evidence (E1, E2, ...), conclusion.
Persisted to data/investigations/<run_id>.json so "Why?" and "Simulate" work on a
finished investigation, even after a restart.

NOTE: this is the slot where a Meterless Engine (World Model / H-MEM) will hold
the shared investigation state once its spec is integrated. Until then this is
plain JSON storage and is NOT presented as a Meterless Engine.
"""
import json
import time

from . import config

DIR = config.ROOT / "data" / "investigations"


def new_investigation(run_id, question):
    return {"run_id": run_id, "question": question, "created": time.time(),
            "evidence": {}, "conclusion": None, "approved": None, "open_issues": []}


def add_evidence(inv, tool, args, purpose, result):
    eid = f"E{len(inv['evidence']) + 1}"
    inv["evidence"][eid] = {"tool": tool, "args": args, "purpose": purpose, "result": result}
    return eid


def save(inv):
    if not inv.get("run_id"):
        return
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{inv['run_id']}.json").write_text(json.dumps(inv, default=str, indent=1))


def load(run_id):
    return json.loads((DIR / f"{run_id}.json").read_text())


def latest():
    files = sorted(DIR.glob("run-*.json"), key=lambda p: p.stat().st_mtime) if DIR.exists() else []
    return json.loads(files[-1].read_text()) if files else None
