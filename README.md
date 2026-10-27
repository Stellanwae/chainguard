# ChainGuard: explainable supply-chain resilience agent

**Track:** The Agent That Can Explain Why · **Open Agent Hackathon 2026**

> *"If one of our suppliers became unavailable, what would actually break, who would be affected, and what should we protect first?"*

ChainGuard is an AI agent that investigates a company's own sales data to find its **most dangerous hidden supply dependency**. Then it explains **why**, with every number traceable to the exact Zetaris query that produced it, and lets you **simulate** that supplier becoming unavailable using historical data.

It does not guess and it does not hardcode. The agent decides what to check, queries Zetaris, reads the result, decides what to check next, and an Evaluator rejects any conclusion containing a number it cannot trace to evidence.

---

## The problem

Businesses usually watch their *biggest* suppliers. The dangerous ones are often the ones that look small: a supplier with a tiny share of revenue whose products sit in thousands of customer baskets, across every store. Nobody escalates a 0.3% supplier until it disappears.

**Users:** supply-chain, procurement and risk managers who need an evidence-backed answer they can defend, not a dashboard of unexplained scores.

## What ChainGuard found (real run, store sales channel)

- **Manufacturer 207** is the top hidden dependency. It is only **0.32% of store sales value**, but its products appear on **9,294 of 240,000 receipts (3.87%)**, reach **8,501 customers**, span **37 product classes**, and sell in **all 6 stores**.
- Its risk comes from **breadth, not size**: it is the sole known maker of **0** product classes, and only **1** customer relied on it for half or more of their store spend.
- **Data-quality findings, surfaced by the agent:** about **11.4 million** in sales has **no manufacturer recorded**; some items have a blank category or class; manufacturer names are inconsistent per ID (so ChainGuard identifies manufacturers by ID).
- An early version flagged a "100% sole-source" class. It turned out to be a **blank category with one item**. ChainGuard now treats blank classes as data gaps, which is exactly the kind of error the Evaluator and schema checks exist to catch.

*Synthetic data note:* the data is the TPC-DS retail benchmark dataset bundled with Zetaris, presented here as a sample company's data. Store sales cover 1998-01-02 to 2003-01-02.

---

## How it works

```
User question
   │
   ▼
Investigator (NVIDIA Nemotron via Meterless Nightshift)
   │  decides the next step → calls one Zetaris tool → reads result → decides again
   │        ▲                                                │
   │        └──────────── World Model engine ◄───────────────┘  (every result ingested
   │                      shared state + provenance               as entities/relationships)
   ▼
Draft conclusion (every claim cites evidence E1, E2, ...)
   │
   ▼
Evaluator (separate Nemotron call) + deterministic number check
   │  rejects → Investigator must investigate further or revise (up to 2 revisions)
   ▼
Conclusion  →  "Why?" (Explainer)  →  "Simulate supplier failure" (Simulator)
```

**Agents**
| Agent | Job |
|---|---|
| Investigator | Plans the investigation step by step and chooses tools; no fixed script |
| Evaluator | Checks every number and citation against the evidence, rejects unsupported claims, predictions and capacity claims |
| Explainer | Answers "Why?" from the stored evidence and World Model relationships |
| Simulator | Runs the failure simulation tools and summarises historical exposure |

**Zetaris tools** (each returns the SQL it ran plus the rows)
`inspect_schema`, `observed_period`, `rank_dependency_candidates` (receipt reach, customers, revenue, sole-source classes, classes led, max class share), `manufacturer_class_profile`, `customer_dependence`, `geographic_exposure`, `diversification_candidates`, `simulate_unavailability`, `run_readonly_sql` (guarded, read-only).

**Language rules built into the agents:** "historical exposure", "potential impact based on observed data", never a forecast. Alternative manufacturers are "diversification candidates to investigate". The data says nothing about their capacity, and ChainGuard never claims it does.

---

## Sponsor technology

### Zetaris: the data layer
- All facts come from Zetaris over its REST API (`POST /sql-editor/sqls/run`) in `app/zetaris.py`.
- Data: the `TPCDS_DB` Lightning database (24 tables; ChainGuard uses `item`, `store_sales`, `store`, `customer_address`, `date_dim`).
- The agent **inspects the schema first** (`inspect_schema`) and every tool checks its required columns exist before running.
- Query results are cached by exact SQL text; cached results are marked as cached in the trace with the time they originally ran on Zetaris.
- Zetaris ran in Docker inside a GitHub Codespace (the build laptop has 8 GB RAM, below Zetaris's 16 GB requirement).

### Meterless: Nightshift + World Model Agent Engine
- **Nightshift is the only AI provider.** `app/llm.py` uses the OpenAI-compatible Runtime API at `METERLESS_BASE_URL` with the Runtime token, `meterless-app: chainguard` and `meterless-scope: user` headers. The Nightshift Runtime ran on the builder's laptop and was reached from the codespace through a `gh codespace ssh -R 8088:127.0.0.1:8088` tunnel.
- **Meterless World Model Agent Engine**, in `app/engines/world_model.py`, has a real job: it is the shared, persistent world state that the Investigator, Explainer and Simulator all read.
  - Every Zetaris tool result is ingested as typed entities (manufacturers, product classes, states, data gaps, time window) and relationships (`sells_in`, `sold_through_store_state`, `diversification_candidate_for`).
  - Stable content-hash IDs, an **append-only observation log** (`data/world/observations.jsonl`), canonical entities **reconstructed** from the log (latest-wins with provenance), and **idempotent ingest**.
  - A **context builder** returns structured context with selection reasons: prior-run context goes to the Investigator, focused context to the "Why?" Explainer.
  - Control plane: `GET /api/world/health`, `GET /api/world/manufacturer/{id}`, `python run.py world 207`.
  - A wired-vs-not-implemented manifest is written to `logs/world_model_manifest.json`.
  - **Test:** `tests/test_world_model.py` covers stable IDs, ingest into entities and relationships with provenance, idempotency, persistence across runs, latest-wins reconstruction, and the context builder's priorities and budget. Run `python3 -m pytest -q tests`.

### NVIDIA: Nemotron does the reasoning
- Every Investigator, Evaluator, Explainer and Simulator call is answered by **`nvidia/nemotron-3.5-lightning-30b-a3b`** through Nightshift. The NVIDIA key lives only in Nightshift's Providers tab.
- The model is pinned in `.env.local` (`METERLESS_MODEL`), and **every model call's answering model is recorded in the logs** (`logs/<run_id>.jsonl`, field `model`).

### What was unique
- The agent's job is to **prove** a dependency, not just rank it: an independent Evaluator plus a deterministic number check reject any figure not found in Zetaris evidence.
- The World Model turns one-off query results into a shared, provenance-stamped picture that persists across runs.
- ChainGuard reports the **data's own gaps** (missing manufacturers, blank classes, inconsistent names) as findings instead of hiding them.

### AI coding assistance
The code was written with AI coding assistance (Claude), as the rules allow. Cursor was not used.

---

## Setup

**Requirements:** Python 3.11+, a running Zetaris instance (local Docker per the Participant Setup Guide, or Docker in a GitHub Codespace), and Meterless Nightshift with a Nemotron lane.

```bash
git clone <this repo> && cd chainguard
pip install -r requirements.txt
cp .env.example .env.local      # then fill in your own values (never commit .env.local)
```

`.env.local`:
```
ZETARIS_REST_URL=http://localhost:8888/api/v1.0
ZETARIS_ORG_ID=1
ZETARIS_API_KEY=<Zetaris API token from the Zetaris UI → API Tokens>
METERLESS_BASE_URL=http://127.0.0.1:8088/v1
METERLESS_RUNTIME_TOKEN=<Nightshift Console Runtime token>
METERLESS_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
```

If Zetaris runs in a codespace and Nightshift on your own computer, open a tunnel from your computer and keep it open:
```
gh codespace ssh -- -R 8088:127.0.0.1:8088 -N
```

## Usage

```bash
python run.py check                          # tests Zetaris and Nightshift connections
python -m pytest -q tests                    # World Model engine + evidence-check tests
python run.py ask "Find the most dangerous hidden dependency in our supply chain."
python run.py tool simulate_unavailability '{"manufacturer_id": 207}'
python run.py world 207                      # what the World Model knows, with provenance
python run.py serve                          # web app on http://localhost:8000
```

The web app shows each agent step live (with the SQL each step ran), the conclusion with clickable evidence, a **Why?** button and a **Simulate supplier failure** button.

**Logs:** every run writes `logs/<run_id>.jsonl`: one line per agent action, tool call, Zetaris query, model (with name) and Evaluator verdict. Investigations are saved to `data/investigations/<run_id>.json`.

**Example questions:**
1. `Find the most dangerous hidden dependency in our supply chain.`
2. `Which manufacturer would hurt the most customers if it became unavailable?`
3. `Are there product classes that depend on a single manufacturer?`

## Limitations
- Store sales channel only (catalog and web sales are in the data but not yet included).
- Exposure is historical, based on observed data. It is not a forecast.
- Diversification candidates do not imply capacity to replace a supplier.
- Each investigation takes several minutes because queries scan the data in cloud storage.
- No offline sample mode yet: ChainGuard needs a live Zetaris and Nightshift.

## Future enhancements
- **Real data, real stakes:** point ChainGuard at a company's own sales and supplier systems through Zetaris, or at essential-medicine supply for Kenyan county hospitals, where one hidden single supplier can mean an insulin stock-out. The agent, Evaluator and World Model stay the same; only the Zetaris sources change.
- Include catalog and web sales; add inventory and warehouse exposure.
- Draft escalation notes to the affected store managers (human-approved, never auto-sent).
- H-MEM memory to track how a supplier's exposure changes over time.
- Scheduled monitoring with alerts when receipt reach crosses a threshold.

## Pre-existing components
None. All code was written during the build window. Open-source libraries: `openai`, `fastapi`, `uvicorn`, `pydantic`, `pytest`.
