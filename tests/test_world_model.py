"""Tests for the Meterless World Model engine as used by ChainGuard.
Run: python3 -m pytest -q tests"""
from app.engines.world_model import WorldModel, stable_id

RANK = {"summary": "ranked", "queries": [{"sql": "SELECT ..."}],
        "rows": [{"manufacturer_id": 207, "manufacturer": "oughtcallyese", "revenue": 16911467.2,
                  "receipt_reach": 0.0387, "customers": 8222}],
        "data_quality": {"unknown_manufacturer_revenue": 11412201.66}}
PROFILE = {"summary": "profile", "queries": [],
           "rows": [{"category": "Men", "class": "pants", "known_class": "true", "items": 6,
                     "revenue": 2499303.46, "class_total": 122105343.05, "share_of_class": 0.0205,
                     "manufacturers_in_class": 313, "rank_in_class": 1}]}
SIM = {"summary": "sim", "queries": [], "rows": [{"exposed_customers": 8501, "exposed_stores": 6}]}


def test_stable_ids_agree_across_agents():
    assert stable_id("manufacturer", 207) == stable_id("manufacturer", "207")
    assert stable_id("manufacturer", 207) != stable_id("manufacturer", 208)


def test_ingest_builds_entities_and_relationships(tmp_path):
    w = WorldModel(tmp_path / "obs.jsonl")
    w.ingest("rank_dependency_candidates", {}, RANK, "E1", "run-a")
    w.ingest("manufacturer_class_profile", {"manufacturer_id": 207}, PROFILE, "E2", "run-a")
    m = w.get_entity(stable_id("manufacturer", 207))
    assert m["attributes"]["receipt_reach"]["value"] == 0.0387
    assert m["attributes"]["receipt_reach"]["evidence_id"] == "E1"  # provenance
    assert any(r["type"] == "sells_in" for r in m["relationships"])
    assert w.health()["orphan_relationships"] == 0
    assert stable_id("data_gap", "unknown_manufacturer") in w.entities()


def test_ingest_is_idempotent(tmp_path):
    w = WorldModel(tmp_path / "obs.jsonl")
    w.ingest("rank_dependency_candidates", {}, RANK, "E1", "run-a")
    w.ingest("rank_dependency_candidates", {}, RANK, "E1", "run-a")
    assert w.health()["observations"] == 1


def test_state_persists_across_runs_and_latest_wins(tmp_path):
    path = tmp_path / "obs.jsonl"
    w1 = WorldModel(path)
    w1.ingest("rank_dependency_candidates", {}, RANK, "E1", "run-a")
    w2 = WorldModel(path)  # a later run, a fresh process
    w2.ingest("simulate_unavailability", {"manufacturer_id": 207}, SIM, "E4", "run-b")
    m = w2.get_entity(stable_id("manufacturer", 207))
    assert m["attributes"]["customers"]["run_id"] == "run-a"
    assert m["attributes"]["exposure_exposed_customers"]["run_id"] == "run-b"
    assert m["mention_count"] == 2 and w2.health()["runs"] == 2


def test_context_builder_prioritises_focus_and_respects_budget(tmp_path):
    w = WorldModel(tmp_path / "obs.jsonl")
    w.ingest("rank_dependency_candidates", {}, RANK, "E1", "run-a")
    w.ingest("manufacturer_class_profile", {"manufacturer_id": 207}, PROFILE, "E2", "run-a")
    focus = stable_id("product_class", "Men", "pants")
    ctx = w.build_context("why", focus_ids=[focus], budget_chars=5000)
    assert ctx["entities"][0]["id"] == focus and ctx["entities"][0]["selection_reason"] == "focus"
    assert len(w.build_context("why", budget_chars=50)["entities"]) == 0
