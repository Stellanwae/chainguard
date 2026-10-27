"""Zetaris data layer: every fact ChainGuard uses comes through run_sql().

Calls the Zetaris REST API (POST /sql-editor/sqls/run). Results are cached on
disk by exact SQL text so a repeated question doesn't re-scan cloud storage;
cached results are clearly marked as cached in the trace, with the time the
query originally ran against Zetaris.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from . import config

CACHE_PATH = config.ROOT / "data" / "query_cache.json"
_cache = None


def _load_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache():
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_cache))


class ZetarisError(RuntimeError):
    pass


def run_sql(sql: str, limit: int = 200, use_cache: bool = True) -> dict:
    """Run one SQL statement on Zetaris.

    Returns {"columns": [...], "rows": [ {col: value}, ... ], "ms": int,
             "cached": bool, "ran_at": iso-time}.
    """
    sql = " ".join(sql.split())
    key = hashlib.sha256(f"{limit}|{sql}".encode()).hexdigest()
    cache = _load_cache()
    if use_cache and key in cache:
        hit = dict(cache[key])
        hit["cached"] = True
        return hit

    url = os.environ.get("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0").rstrip("/")
    token = os.environ.get("ZETARIS_API_KEY")
    if not token:
        raise ZetarisError("ZETARIS_API_KEY is not set in .env.local")
    body = json.dumps({
        "queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit,
    }).encode()
    request = urllib.request.Request(url + "/sql-editor/sqls/run", data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "X-Org-ID": os.environ.get("ZETARIS_ORG_ID", "1"),
        "X-Request-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    })
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise ZetarisError(f"Zetaris HTTP {error.code}: {error.read(600).decode(errors='replace')}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ZetarisError(f"Cannot reach Zetaris at {url}: {error}") from error

    columns = payload.get("headers") or []
    raw_rows = payload.get("data") or []
    rows = [dict(zip(columns, r)) if isinstance(r, list) else r for r in raw_rows]
    result = {
        "columns": columns,
        "rows": rows,
        "ms": int(payload.get("timeUsed") or (time.time() - started) * 1000),
        "cached": False,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if use_cache:
        cache[key] = {k: v for k, v in result.items() if k != "cached"}
        _save_cache()
    return result


def num(value):
    """Zetaris returns values as strings (e.g. '1.48E7'); convert safely."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f.is_integer() and abs(f) < 1e15 else round(f, 4)
