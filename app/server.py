"""Web app: POST /api/investigate starts an investigation in the background;
the page polls /api/runs/<id> to show each step live."""
import json
import threading
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, config, state
from .engines.world_model import get_world, stable_id
from .trace import Trace

app = FastAPI(title="ChainGuard")
RUNS = {}  # run_id -> {"trace": Trace, "status": str, "result": dict|None, "error": str|None}


class Question(BaseModel):
    question: str = "Find the most dangerous hidden dependency in our supply chain."


class Simulation(BaseModel):
    run_id: str | None = None
    manufacturer_id: int


class Why(BaseModel):
    run_id: str
    question: str = "Why is this manufacturer the most critical dependency?"


@app.get("/")
def index():
    return FileResponse(config.ROOT / "static" / "index.html")


@app.post("/api/investigate")
def start(q: Question):
    trace = Trace()
    RUNS[trace.run_id] = {"trace": trace, "status": "running", "result": None, "error": None}

    def work():
        try:
            RUNS[trace.run_id]["result"] = agent.investigate(q.question, trace=trace)
            RUNS[trace.run_id]["status"] = "done"
        except Exception as error:  # show the real error in the UI
            traceback.print_exc()
            RUNS[trace.run_id]["error"] = str(error)
            RUNS[trace.run_id]["status"] = "error"

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": trace.run_id}


@app.get("/api/latest")
def latest():
    inv = state.latest()
    if not inv:
        raise HTTPException(404, "no saved investigation yet")
    return {"run_id": inv["run_id"]}


@app.get("/api/runs/{run_id}")
def status(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        try:
            result = state.load(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, "unknown run")
        events = []
        log = config.ROOT / "logs" / f"{run_id}.jsonl"
        if log.exists():
            events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        return {"status": "done", "events": events, "result": result, "error": None}
    return {"status": run["status"], "events": run["trace"].events, "result": run["result"],
            "error": run["error"]}


@app.post("/api/why")
def why(body: Why):
    return agent.explain_why(body.run_id, body.question)


@app.post("/api/simulate")
def simulate(body: Simulation):
    return agent.simulate(body.run_id, body.manufacturer_id)


@app.get("/api/world/health")
def world_health():
    return get_world().health()


@app.get("/api/world/manufacturer/{manufacturer_id}")
def world_manufacturer(manufacturer_id: int):
    e = get_world().get_entity(stable_id("manufacturer", manufacturer_id))
    if not e:
        raise HTTPException(404, "not in the world model yet")
    return e
