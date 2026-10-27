"""ChainGuard entry points.

  python run.py check                 # test Zetaris + Nightshift connections
  python run.py tool <name> [json]    # run one Zetaris tool, e.g. tool simulate_unavailability '{"manufacturer_id": 207}'
  python run.py ask "question"        # full agent investigation in the terminal
  python run.py world [manufacturer]  # World Model engine health / one manufacturer
  python run.py serve                 # web app on port 8000
"""
import json
import sys

from app import config  # noqa: F401


def check():
    from app import llm
    from app.zetaris import run_sql
    try:
        r = run_sql("SELECT 1 AS one", use_cache=False)
        print("PASS Zetaris:", r["rows"])
    except Exception as e:
        print("FAIL Zetaris:", e)
    try:
        print("Nightshift models:", llm.list_models())
        reply = llm.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=400)
        print("PASS Nightshift:", reply["content"].strip()[:80], "| model:", reply["model"])
    except Exception as e:
        print("FAIL Nightshift:", e)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "check":
        check()
    elif cmd == "tool":
        from app import tools
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(tools.call_tool(sys.argv[2], args), indent=1, default=str)[:6000])
    elif cmd == "ask":
        from app import agent
        inv = agent.investigate(" ".join(sys.argv[2:]) or "Find the most dangerous hidden dependency in our supply chain.")
        print(json.dumps(inv["conclusion"], indent=1))
        print("approved:", inv["approved"], "| run:", inv["run_id"])
        for issue in inv.get("open_issues") or []:
            print("  evaluator issue:", issue)
    elif cmd == "world":
        from app.engines.world_model import get_world, stable_id
        w = get_world()
        print(json.dumps(w.health(), indent=1))
        if len(sys.argv) > 2:
            print(json.dumps(w.get_entity(stable_id("manufacturer", sys.argv[2])), indent=1, default=str)[:6000])
    elif cmd == "serve":
        import uvicorn
        uvicorn.run("app.server:app", host="0.0.0.0", port=8000)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
