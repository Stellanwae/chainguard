"""zt.py - send one SQL statement to Zetaris over its REST API and print the result."""
import json, os, sys, uuid, urllib.request, urllib.error
from pathlib import Path

def load_env():
    p = Path(__file__).with_name(".env.local")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def run_sql(sql, limit=200):
    url = os.environ.get("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0").rstrip("/") + "/sql-editor/sqls/run"
    body = json.dumps({"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + os.environ["ZETARIS_API_KEY"],
        "X-Org-ID": os.environ.get("ZETARIS_ORG_ID", "1"),
        "X-Request-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read(1000).decode(errors='replace')}")

if __name__ == "__main__":
    load_env()
    resp = run_sql(sys.argv[1])
    if isinstance(resp, dict):
        meta = {k: v for k, v in resp.items() if k != "data"}
        print("META:", json.dumps(meta, default=str)[:1500])
        data = resp.get("data")
    else:
        data = resp
    rows = data if isinstance(data, list) else [data]
    print(f"ROWS: {len(rows)}")
    for row in rows[:80]:
        print(json.dumps(row, default=str))
