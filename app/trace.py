"""Observable investigation log: one JSON line per event in logs/<run_id>.jsonl.
Records what each agent did, which tool/SQL ran, and which model answered.
It records actions and evidence, not hidden model reasoning."""
import json
import threading
import time
import uuid

from . import config

LOG_DIR = config.ROOT / "logs"


class Trace:
    def __init__(self, run_id=None):
        self.run_id = run_id or time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.events = []
        self._lock = threading.Lock()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"{self.run_id}.jsonl"

    def log(self, agent, action, **fields):
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run_id": self.run_id,
                 "step": len(self.events) + 1, "agent": agent, "action": action, **fields}
        with self._lock:
            self.events.append(event)
            with self.path.open("a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        print(f"[{event['step']:02d}] {agent:<10} {action:<22} "
              f"{fields.get('tool') or fields.get('model') or ''} {fields.get('note', '')}"[:200], flush=True)
        return event
