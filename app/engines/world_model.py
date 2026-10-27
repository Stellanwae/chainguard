"""Meterless World Model Agent Engine, implemented for ChainGuard.

Follows the Meterless "World Model Agent Engine" reference architecture (v1.0):
  - Hybrid aggregate: a typed entity/relationship graph + an append-only
    observation log (one observation per Zetaris tool result).            (sec 4.1)
  - Entity records with identity, semantics (attributes), aggregates
    (mention counts, first/last seen), provenance and agent attribution
    (agentId / runId / evidence id).                                     (sec 4.2)
  - Stable content-hash IDs, so every agent and every run agree on what
    "manufacturer 207" or "class Men/pants" is without coordination.     (sec 3)
  - Append-only log; canonical entities are RECONSTRUCTED from the log
    (latest-wins per attribute, union of provenance), never mutated
    independently by an agent.                                           (sec 3, 11.2)
  - Idempotent ingest: re-ingesting the same observation changes nothing. (sec 3, 6)
  - Context builder returning a structured payload + trace metadata,
    used by the Investigator, Explainer and Simulator agents.            (sec 10.2)
  - Headless control plane: query endpoints + health.                    (sec 12)
  - Wired-vs-helper manifest written to logs/ so judges can see what is
    live and what is not implemented.                                    (sec 10.3)

Role in ChainGuard: the shared, persistent world state that the Investigator,
Evaluator, Explainer ("Why?") and Simulator all read, so every agent reasons
over the same manufacturers, product classes, states and relationships, and
every belief traces back to the Zetaris query (evidence id) that produced it.
State survives across runs in data/world/observations.jsonl.
"""
import hashlib
import json
import threading
import time

from .. import config

SCHEMA_VERSION = 1
DIR = config.ROOT / "data" / "world"
LOG_PATH = DIR / "observations.jsonl"
MANIFEST_PATH = config.ROOT / "logs" / "world_model_manifest.json"


def stable_id(kind, *parts):
    """Content-hash ID: same entity -> same ID in every agent and every run."""
    key = "|".join([kind] + [str(p).strip().lower() for p in parts])
    return f"{kind}_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


class WorldModel:
    def __init__(self, log_path=LOG_PATH):
        self.log_path = log_path
        self._lock = threading.Lock()
        self.observations = []
        self._seen = set()
        if self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                if line.strip():
                    obs = json.loads(line)
                    self.observations.append(obs)
                    self._seen.add(obs["obs_id"])

    # ------------------------------------------------------------ write path
    def ingest(self, tool, args, result, evidence_id, run_id, agent_id="Investigator"):
        """Turn one Zetaris tool result into entity/relationship facts and append
        it to the log. Idempotent: the same observation is only stored once."""
        facts = list(_extract(tool, args or {}, result or {}))
        obs_id = stable_id("obs", run_id, evidence_id, tool, json.dumps(args, sort_keys=True))
        if obs_id in self._seen:
            return obs_id, 0
        obs = {"obs_id": obs_id, "schema": SCHEMA_VERSION, "ts": time.time(),
               "run_id": run_id, "agent_id": agent_id, "evidence_id": evidence_id,
               "tool": tool, "args": args, "summary": result.get("summary"),
               "queries": [q.get("sql") for q in result.get("queries", [])], "facts": facts}
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(obs, default=str) + "\n")
            self.observations.append(obs)
            self._seen.add(obs_id)
        return obs_id, len(facts)

    # ------------------------------------------------------ canonical views
    def entities(self):
        """Canonical entities reconstructed from the append log."""
        out = {}
        for obs in self.observations:
            prov = {"run_id": obs["run_id"], "evidence_id": obs["evidence_id"],
                    "tool": obs["tool"], "agent_id": obs["agent_id"], "ts": obs["ts"]}
            for fact in obs["facts"]:
                if fact["kind"] != "entity":
                    continue
                e = out.setdefault(fact["id"], {
                    "id": fact["id"], "type": fact["type"], "name": fact["name"],
                    "attributes": {}, "mention_count": 0, "first_seen": obs["ts"],
                    "last_seen": obs["ts"], "provenance": []})
                e["mention_count"] += 1
                e["last_seen"] = obs["ts"]
                for k, v in fact.get("attributes", {}).items():
                    if v is not None:
                        e["attributes"][k] = {"value": v, **prov}  # latest wins, with provenance
                if prov not in e["provenance"]:
                    e["provenance"].append(prov)
        return out

    def relationships(self):
        out = {}
        for obs in self.observations:
            for fact in obs["facts"]:
                if fact["kind"] != "edge":
                    continue
                key = (fact["from"], fact["type"], fact["to"])
                out[key] = {**fact, "run_id": obs["run_id"], "evidence_id": obs["evidence_id"]}
        return list(out.values())

    # ---------------------------------------------------- control plane
    def get_entity(self, entity_id):
        e = self.entities().get(entity_id)
        if not e:
            return None
        e = dict(e)
        e["relationships"] = [r for r in self.relationships()
                              if r["from"] == entity_id or r["to"] == entity_id]
        return e

    def health(self):
        ents = self.entities()
        rels = self.relationships()
        ids = set(ents)
        orphans = [r for r in rels if r["from"] not in ids or r["to"] not in ids]
        by_type = {}
        for e in ents.values():
            by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        return {"schema_version": SCHEMA_VERSION, "observations": len(self.observations),
                "runs": len({o["run_id"] for o in self.observations}),
                "entities": len(ents), "entities_by_type": by_type,
                "relationships": len(rels), "orphan_relationships": len(orphans)}

    def build_context(self, intent, focus_ids=None, budget_chars=2500, exclude_run=None):
        """Structured context for an agent: most relevant entities first (focus,
        then mention count, then recency), each with provenance; truncated to budget."""
        ents = [e for e in self.entities().values()
                if not exclude_run or any(p["run_id"] != exclude_run for p in e["provenance"])]
        focus_ids = set(focus_ids or [])

        def rank(e):
            return (e["id"] in focus_ids, e["mention_count"], e["last_seen"])

        ents.sort(key=rank, reverse=True)
        items, used = [], 0
        for e in ents:
            item = {"id": e["id"], "type": e["type"], "name": e["name"],
                    "attributes": {k: v["value"] for k, v in e["attributes"].items()},
                    "evidence": sorted({f"{p['run_id']}:{p['evidence_id']}" for p in e["provenance"]})[-3:],
                    "selection_reason": "focus" if e["id"] in focus_ids else "most-mentioned"}
            size = len(json.dumps(item, default=str))
            if used + size > budget_chars:
                break
            items.append(item)
            used += size
        return {"intent": intent, "strategy": "focus>mentions>recency", "entities": items,
                "health": self.health()}


def _extract(tool, args, result):
    """Map each Zetaris tool result onto world entities and relationships."""
    rows = result.get("rows") or []

    def maker(mid, name=None, **attrs):
        return {"kind": "entity", "type": "manufacturer", "id": stable_id("manufacturer", mid),
                "name": f"manufacturer {mid}" + (f" ({name})" if name else ""),
                "attributes": {"manufacturer_id": mid, "recorded_name": name, **attrs}}

    if tool == "rank_dependency_candidates":
        for r in rows:
            mid = r.get("manufacturer_id")
            yield maker(mid, r.get("manufacturer"), **{k: r.get(k) for k in (
                "revenue", "revenue_share", "receipts", "receipt_reach", "customers",
                "sole_source_classes", "classes_led", "max_class_share", "classes_present")})
        dq = result.get("data_quality") or {}
        if dq.get("unknown_manufacturer_revenue") is not None:
            yield {"kind": "entity", "type": "data_gap", "id": stable_id("data_gap", "unknown_manufacturer"),
                   "name": "sales with no manufacturer recorded",
                   "attributes": {k: v for k, v in dq.items() if k.startswith("unknown_")}}
        if dq.get("unclassified_sales_share") is not None:
            yield {"kind": "entity", "type": "data_gap", "id": stable_id("data_gap", "unclassified"),
                   "name": "sales with blank product category or class",
                   "attributes": {"unclassified_sales_share": dq["unclassified_sales_share"]}}
    elif tool == "manufacturer_class_profile":
        mid = args.get("manufacturer_id")
        yield maker(mid)
        for r in rows:
            known = r.get("known_class") in (True, "true", 1)
            label = f"{r.get('category') or '(blank)'} / {r.get('class') or '(blank)'}"
            cid = stable_id("product_class", r.get("category"), r.get("class"))
            yield {"kind": "entity", "type": "product_class" if known else "data_gap", "id": cid,
                   "name": label, "attributes": {"class_total_revenue": r.get("class_total"),
                                                 "known_manufacturers": r.get("manufacturers_in_class")}}
            yield {"kind": "edge", "type": "sells_in", "from": stable_id("manufacturer", mid), "to": cid,
                   "properties": {"share_of_class": r.get("share_of_class"), "rank_in_class": r.get("rank_in_class"),
                                  "items": r.get("items"), "revenue": r.get("revenue")}}
    elif tool == "customer_dependence" and rows:
        yield maker(args.get("manufacturer_id"), **{k: rows[0].get(k) for k in (
            "customers_who_bought", "customers_25pct_plus", "customers_50pct_plus", "avg_share_among_buyers")})
    elif tool == "simulate_unavailability" and rows:
        r = rows[0]
        yield maker(args.get("manufacturer_id"), **{f"exposure_{k}": v for k, v in r.items()})
    elif tool == "geographic_exposure":
        mid = args.get("manufacturer_id")
        yield maker(mid)
        for key, field, etype in (("by_store_state", "store_state", "store_state"),
                                  ("by_customer_state", "customer_state", "customer_state")):
            for r in result.get(key) or []:
                sid = stable_id(etype, r.get(field))
                yield {"kind": "entity", "type": etype, "id": sid, "name": str(r.get(field)), "attributes": {}}
                yield {"kind": "edge", "type": f"sold_through_{etype}", "from": stable_id("manufacturer", mid),
                       "to": sid, "properties": {"revenue": r.get("revenue"),
                                                 "customers": r.get("customers"), "stores": r.get("stores")}}
    elif tool == "diversification_candidates":
        mid = args.get("manufacturer_id")
        yield maker(mid)
        for r in rows:
            cid = stable_id("product_class", r.get("category"), r.get("class"))
            yield {"kind": "entity", "type": "product_class", "id": cid,
                   "name": f"{r.get('category')} / {r.get('class')}", "attributes": {}}
            yield maker(r.get("candidate_id"), r.get("candidate"))
            yield {"kind": "edge", "type": "diversification_candidate_for",
                   "from": stable_id("manufacturer", r.get("candidate_id")), "to": stable_id("manufacturer", mid),
                   "properties": {"class": cid, "candidate_revenue": r.get("candidate_revenue"),
                                  "note": "candidate only; capacity not shown by data"}}
    elif tool == "observed_period" and rows:
        yield {"kind": "entity", "type": "time_window", "id": stable_id("time_window", "store_sales"),
               "name": "observed store sales period", "attributes": dict(rows[0])}


def write_manifest(world):
    """Wired-vs-helper map (engine sec 10.3): what is live, what is not."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps({
        "engine": "Meterless World Model Agent Engine v1.0 (ChainGuard implementation)",
        "schema_version": SCHEMA_VERSION,
        "wired": {
            "ingest": "every Zetaris tool result (agent._run_tool)",
            "canonical_reconstruction": "entities()/relationships() rebuilt from observations.jsonl",
            "context_builder": "Investigator start (prior runs), Explainer ('Why?'), Simulator",
            "control_plane": "GET /api/world/health, GET /api/world/manufacturer/{id}, `python run.py world`",
        },
        "not_implemented": ["enrichment jobs", "supervisor/maintenance agent", "merge detection",
                            "bitemporal edges", "H-MEM memory tiers"],
        "health": world.health(),
    }, indent=1, default=str))


_world = None


def get_world():
    global _world
    if _world is None:
        _world = WorldModel()
    return _world
