"""ChainGuard investigation tools. Each tool runs real SQL on Zetaris and returns
the SQL it ran plus the rows it got back, so every claim can be traced.

Data model (inspected live in Zetaris, TPCDS_DB):
  item.i_manufact_id / i_manufact  -> the manufacturer (our "supplier" role)
  store_sales.ss_item_sk           -> item sold
  store_sales.ss_ext_sales_price   -> sales value of the line
  store_sales.ss_customer_sk / ss_store_sk / ss_addr_sk / ss_ticket_number
Tables used for geography (customer_address, store) and time (date_dim) are
checked at run time by inspect_schema before any tool relies on them.

Scope: the store sales channel. Catalog and web sales exist in the data but are
not included yet; tools say so in their output.
"""
import re

from . import config
from .zetaris import ZetarisError, num, run_sql

DB = config.DATABASE
SCOPE_NOTE = "Store sales channel only (catalog and web sales not included)."

# Columns each tool depends on. inspect_schema() checks these against Zetaris.
REQUIRED = {
    "item": ["i_item_sk", "i_manufact_id", "i_manufact", "i_category", "i_class"],
    "store_sales": ["ss_item_sk", "ss_customer_sk", "ss_store_sk", "ss_addr_sk",
                    "ss_ticket_number", "ss_ext_sales_price", "ss_sold_date_sk"],
    "customer_address": ["ca_address_sk", "ca_state"],
    "store": ["s_store_sk", "s_store_name", "s_state"],
    "date_dim": ["d_date_sk", "d_date"],
}

_schema = {}  # table -> list of columns, filled by inspect_schema


def _t(table):
    return f"{DB}.{table}"


def _rows(result, max_rows=25):
    return [{k: num(v) for k, v in r.items()} for r in result["rows"][:max_rows]]


def _evidence(summary, results, rows, extra=None):
    out = {
        "summary": summary,
        "queries": [{"sql": sql, "ms": res["ms"], "cached": res["cached"], "ran_at": res["ran_at"],
                     "row_count": len(res["rows"])} for sql, res in results],
        "rows": rows,
        "scope": SCOPE_NOTE,
    }
    if extra:
        out.update(extra)
    return out


def _need(*tables):
    if not _schema:
        inspect_schema()
    missing = {t: [c for c in REQUIRED[t] if c not in _schema.get(t, [])] for t in tables}
    missing = {t: cols for t, cols in missing.items() if cols}
    if missing:
        raise ZetarisError(f"Required columns not found in Zetaris: {missing}")


def _mid(manufacturer_id):
    try:
        return int(manufacturer_id)
    except (TypeError, ValueError):
        raise ZetarisError(f"manufacturer_id must be a number, got {manufacturer_id!r}")


# --------------------------------------------------------------------- tools

def inspect_schema():
    """List tables in the database and the columns of the tables ChainGuard uses."""
    results = []
    sql = f"SHOW LIGHTNING TABLES {DB}"
    tables_res = run_sql(sql)
    results.append((sql, tables_res))
    tables = [r.get("name") for r in tables_res["rows"]]
    found = {}
    for table in REQUIRED:
        if table not in tables:
            found[table] = None
            continue
        q = f"SELECT * FROM {_t(table)} LIMIT 1"
        res = run_sql(q, limit=1)
        results.append((q, res))
        _schema[table] = res["columns"]
        found[table] = {
            "columns": res["columns"],
            "required_present": [c for c in REQUIRED[table] if c in res["columns"]],
            "required_missing": [c for c in REQUIRED[table] if c not in res["columns"]],
        }
    summary = (f"{DB} has {len(tables)} tables. Checked the columns of: "
               + ", ".join(t for t, v in found.items() if v) + ".")
    return _evidence(summary, results, [], {"tables": tables, "checked": found})


def observed_period():
    """First and last sale date in the store sales data (the history we can speak about)."""
    _need("store_sales", "date_dim")
    sql = (f"SELECT MIN(d.d_date) AS first_sale_date, MAX(d.d_date) AS last_sale_date, "
           f"COUNT(*) AS sales_lines, SUM(s.ss_ext_sales_price) AS total_sales_value "
           f"FROM {_t('store_sales')} s JOIN {_t('date_dim')} d ON s.ss_sold_date_sk = d.d_date_sk")
    res = run_sql(sql)
    rows = _rows(res)
    r = rows[0] if rows else {}
    return _evidence(f"Store sales run from {r.get('first_sale_date')} to {r.get('last_sale_date')}.",
                     [(sql, res)], rows)


KNOWN_CLASS = "(COALESCE(TRIM(cat), '') <> '' AND COALESCE(TRIM(cls), '') <> '')"


def rank_dependency_candidates(order_by="receipt_reach", limit=10):
    """Rank manufacturers by how much the business depends on them.

    Metrics per manufacturer (store sales):
      revenue, revenue_share (of all store sales value),
      receipts / receipt_reach: how many customer receipts (and what share of all
        receipts) contain at least one of its products,
      customers: distinct customers who bought its products,
      sole_source_classes: product classes where it is the ONLY known manufacturer,
      classes_led: product classes where it is the No.1 manufacturer by sales,
      max_class_share: its highest share of any single product class's sales,
      classes_present: how many product classes it sells in.
    Product classes with a blank category or class name, and sales whose item has
    no manufacturer recorded, are excluded from the class metrics and reported as
    data-quality findings.
    order_by: receipt_reach | customers | revenue | sole_source_classes | classes_led | max_class_share
    """
    _need("item", "store_sales")
    allowed = ["receipt_reach", "customers", "revenue", "sole_source_classes", "classes_led", "max_class_share"]
    order = str(order_by) if str(order_by) in allowed else "receipt_reach"
    limit = max(1, min(int(limit or 10), 25))
    sql = f"""
    WITH mc AS (
      SELECT i.i_category AS cat, i.i_class AS cls, i.i_manufact_id AS m, MAX(i.i_manufact) AS name,
             SUM(s.ss_ext_sales_price) AS r
      FROM {_t('store_sales')} s JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
      GROUP BY i.i_category, i.i_class, i.i_manufact_id
    ), t AS (
      SELECT cat, cls, m, name, r, {KNOWN_CLASS} AS known_class,
             SUM(r) OVER (PARTITION BY cat, cls) AS class_total,
             COUNT(m) OVER (PARTITION BY cat, cls) AS known_makers_in_class,
             RANK() OVER (PARTITION BY cat, cls ORDER BY r DESC) AS rk
      FROM mc
    ), agg AS (
      SELECT m, MAX(name) AS name, SUM(r) AS revenue,
             MAX(CASE WHEN known_class THEN r / class_total END) AS max_class_share,
             SUM(CASE WHEN m IS NOT NULL AND known_class AND known_makers_in_class = 1 THEN 1 ELSE 0 END)
               AS sole_source_classes,
             SUM(CASE WHEN known_class AND rk = 1 THEN 1 ELSE 0 END) AS classes_led,
             SUM(CASE WHEN known_class THEN 1 ELSE 0 END) AS classes_present
      FROM t GROUP BY m
    ), reach AS (
      SELECT i.i_manufact_id AS m, COUNT(DISTINCT s.ss_ticket_number) AS receipts,
             COUNT(DISTINCT s.ss_customer_sk) AS customers
      FROM {_t('store_sales')} s JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
      GROUP BY i.i_manufact_id
    ), total AS (
      SELECT SUM(r) AS grand, SUM(CASE WHEN known_class THEN 0 ELSE r END) AS unclassified FROM t
    ), tt AS (SELECT COUNT(DISTINCT ss_ticket_number) AS all_receipts FROM {_t('store_sales')})
    SELECT agg.m AS manufacturer_id, agg.name AS manufacturer, agg.revenue,
           agg.revenue / total.grand AS revenue_share,
           reach.receipts, reach.receipts / tt.all_receipts AS receipt_reach, tt.all_receipts,
           reach.customers, agg.sole_source_classes, agg.classes_led, agg.max_class_share,
           agg.classes_present, total.unclassified / total.grand AS unclassified_sales_share
    FROM agg JOIN reach ON agg.m <=> reach.m CROSS JOIN total CROSS JOIN tt
    ORDER BY CASE WHEN agg.m IS NULL THEN 0 ELSE 1 END, {order} DESC, revenue DESC
    LIMIT {limit + 1}
    """
    res = run_sql(sql)
    rows = _rows(res, limit + 1)
    unknown = [r for r in rows if r.get("manufacturer_id") is None]
    rows = [r for r in rows if r.get("manufacturer_id") is not None][:limit]
    dq = {"note": "Manufacturer names are not consistent per id in this data; use manufacturer_id."}
    if rows:
        dq["unclassified_sales_share"] = rows[0].get("unclassified_sales_share")
        dq["unclassified_note"] = "Share of sales whose item has a blank category or class (excluded from class metrics)."
    if unknown:
        u = unknown[0]
        dq.update({"unknown_manufacturer_revenue": u.get("revenue"),
                   "unknown_manufacturer_revenue_share": u.get("revenue_share"),
                   "unknown_manufacturer_receipts": u.get("receipts"),
                   "unknown_manufacturer_note": "Sales whose item has no manufacturer id (excluded from ranking)."})
    for r in rows:
        r.pop("unclassified_sales_share", None)
    top = rows[0] if rows else {}
    return _evidence(
        f"Top {len(rows)} manufacturers ordered by {order}. No.1: manufacturer id {top.get('manufacturer_id')}.",
        [(sql, res)], rows, {"data_quality": dq})


def manufacturer_class_profile(manufacturer_id):
    """Product classes the manufacturer sells in, its share of each class, and how many
    other known manufacturers sell in that class. Blank classes are flagged known_class=false."""
    _need("item", "store_sales")
    m = _mid(manufacturer_id)
    sql = f"""
    WITH mc AS (
      SELECT i.i_category AS cat, i.i_class AS cls, i.i_manufact_id AS m,
             SUM(s.ss_ext_sales_price) AS r, COUNT(DISTINCT i.i_item_sk) AS items
      FROM {_t('store_sales')} s JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
      GROUP BY i.i_category, i.i_class, i.i_manufact_id
    ), t AS (
      SELECT cat, cls, m, r, items, {KNOWN_CLASS} AS known_class,
             SUM(r) OVER (PARTITION BY cat, cls) AS class_total,
             COUNT(m) OVER (PARTITION BY cat, cls) AS manufacturers_in_class,
             RANK() OVER (PARTITION BY cat, cls ORDER BY r DESC) AS rank_in_class
      FROM mc
    )
    SELECT cat AS category, cls AS class, known_class, items, r AS revenue, class_total,
           r / class_total AS share_of_class, manufacturers_in_class, rank_in_class
    FROM t WHERE m = {m}
    ORDER BY CASE WHEN known_class THEN 0 ELSE 1 END, share_of_class DESC
    LIMIT 45
    """
    res = run_sql(sql)
    rows = _rows(res, 45)
    known = [r for r in rows if r.get("known_class") in (True, "true", 1)]
    led = sum(1 for r in known if r.get("rank_in_class") == 1)
    sole = sum(1 for r in known if r.get("manufacturers_in_class") == 1)
    return _evidence(f"Manufacturer {m} sells in {len(known)} named product classes "
                     f"(+{len(rows) - len(known)} with blank category/class); No.1 in {led}, sole known maker in {sole}.",
                     [(sql, res)], rows)


def customer_dependence(manufacturer_id):
    """How many customers bought from this manufacturer, and how many depend on it for a
    large part of their store spend (>=25%, >=50%)."""
    _need("item", "store_sales")
    m = _mid(manufacturer_id)
    sql = f"""
    WITH cust AS (
      SELECT s.ss_customer_sk AS c, SUM(s.ss_ext_sales_price) AS total_spend,
             SUM(CASE WHEN i.i_manufact_id = {m} THEN s.ss_ext_sales_price ELSE 0 END) AS m_spend
      FROM {_t('store_sales')} s JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
      WHERE s.ss_customer_sk IS NOT NULL
      GROUP BY s.ss_customer_sk
    )
    SELECT COUNT(*) AS customers_total,
           SUM(CASE WHEN m_spend > 0 THEN 1 ELSE 0 END) AS customers_who_bought,
           SUM(CASE WHEN m_spend > 0 AND m_spend / total_spend >= 0.25 THEN 1 ELSE 0 END) AS customers_25pct_plus,
           SUM(CASE WHEN m_spend > 0 AND m_spend / total_spend >= 0.50 THEN 1 ELSE 0 END) AS customers_50pct_plus,
           AVG(CASE WHEN m_spend > 0 THEN m_spend / total_spend END) AS avg_share_among_buyers
    FROM cust WHERE total_spend > 0
    """
    res = run_sql(sql)
    rows = _rows(res)
    r = rows[0] if rows else {}
    return _evidence(f"{r.get('customers_who_bought')} customers bought from manufacturer {m}; "
                     f"{r.get('customers_50pct_plus')} relied on it for half or more of their store spend.",
                     [(sql, res)], rows, {"thresholds_used": [0.25, 0.5, 25, 50]})


def geographic_exposure(manufacturer_id):
    """Where this manufacturer's sales happen: by store state and by the customer address
    state recorded on the sale."""
    _need("item", "store_sales", "store", "customer_address")
    m = _mid(manufacturer_id)
    sql_store = f"""
    SELECT st.s_state AS store_state, COUNT(DISTINCT st.s_store_sk) AS stores,
           SUM(s.ss_ext_sales_price) AS revenue
    FROM {_t('store_sales')} s
    JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
    JOIN {_t('store')} st ON s.ss_store_sk = st.s_store_sk
    WHERE i.i_manufact_id = {m}
    GROUP BY st.s_state ORDER BY revenue DESC LIMIT 15
    """
    sql_cust = f"""
    SELECT ca.ca_state AS customer_state, COUNT(DISTINCT s.ss_customer_sk) AS customers,
           SUM(s.ss_ext_sales_price) AS revenue
    FROM {_t('store_sales')} s
    JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
    JOIN {_t('customer_address')} ca ON s.ss_addr_sk = ca.ca_address_sk
    WHERE i.i_manufact_id = {m}
    GROUP BY ca.ca_state ORDER BY revenue DESC LIMIT 15
    """
    r1 = run_sql(sql_store)
    r2 = run_sql(sql_cust)
    return _evidence(f"Manufacturer {m} sells through stores in {len(r1['rows'])} state(s); "
                     f"top customer states listed.",
                     [(sql_store, r1), (sql_cust, r2)], [],
                     {"by_store_state": _rows(r1, 15), "by_customer_state": _rows(r2, 15)})


def diversification_candidates(manufacturer_id):
    """Other manufacturers that sell in the same product classes, with their historical
    sales. These are candidates to investigate only: the data says nothing about their
    capacity to replace the manufacturer."""
    _need("item", "store_sales")
    m = _mid(manufacturer_id)
    sql = f"""
    WITH classes AS (SELECT DISTINCT i_category, i_class FROM {_t('item')} WHERE i_manufact_id = {m}),
    other AS (
      SELECT i.i_category AS cat, i.i_class AS cls, i.i_manufact_id AS alt_id, i.i_manufact AS alt_name,
             SUM(s.ss_ext_sales_price) AS revenue
      FROM {_t('store_sales')} s
      JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
      JOIN classes c ON i.i_category = c.i_category AND i.i_class = c.i_class
      WHERE i.i_manufact_id <> {m}
      GROUP BY i.i_category, i.i_class, i.i_manufact_id, i.i_manufact
    ), ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY cat, cls ORDER BY revenue DESC) AS rn,
             COUNT(*) OVER (PARTITION BY cat, cls) AS other_manufacturers
      FROM other
    )
    SELECT cat AS category, cls AS class, other_manufacturers, alt_id AS candidate_id,
           alt_name AS candidate, revenue AS candidate_revenue
    FROM ranked WHERE rn <= 2
    ORDER BY other_manufacturers ASC, category, class, rn
    LIMIT 30
    """
    res = run_sql(sql)
    return _evidence(f"Other manufacturers selling in the same classes as {m} (top 2 per class). "
                     "Candidates only: capacity to replace is NOT shown by the data.",
                     [(sql, res)], _rows(res, 30))


def simulate_unavailability(manufacturer_id):
    """Historical exposure if this manufacturer became unavailable: what share of past
    store sales, items, tickets, customers and stores involved its products."""
    _need("item", "store_sales")
    m = _mid(manufacturer_id)
    sql = f"""
    WITH all_s AS (
      SELECT s.ss_ticket_number AS t, s.ss_customer_sk AS c, s.ss_store_sk AS st, s.ss_item_sk AS it,
             s.ss_ext_sales_price AS rev, i.i_manufact_id AS m
      FROM {_t('store_sales')} s JOIN {_t('item')} i ON s.ss_item_sk = i.i_item_sk
    )
    SELECT
      SUM(CASE WHEN m = {m} THEN rev ELSE 0 END) AS exposed_revenue,
      SUM(rev) AS total_revenue,
      SUM(CASE WHEN m = {m} THEN rev ELSE 0 END) / SUM(rev) AS exposed_revenue_share,
      SUM(CASE WHEN m = {m} THEN 1 ELSE 0 END) AS exposed_sales_lines,
      COUNT(DISTINCT CASE WHEN m = {m} THEN it END) AS exposed_items,
      COUNT(DISTINCT CASE WHEN m = {m} THEN t END) AS exposed_tickets,
      COUNT(DISTINCT t) AS total_tickets,
      COUNT(DISTINCT CASE WHEN m = {m} THEN c END) AS exposed_customers,
      COUNT(DISTINCT CASE WHEN m = {m} THEN st END) AS exposed_stores,
      COUNT(DISTINCT st) AS total_stores
    FROM all_s
    """
    res = run_sql(sql)
    rows = _rows(res)
    r = rows[0] if rows else {}
    share = r.get("exposed_revenue_share")
    share_txt = f"{share * 100:.2f}%" if isinstance(share, (int, float)) else "n/a"
    return _evidence(
        f"Historical exposure for manufacturer {m}: {share_txt} of store sales value, "
        f"{r.get('exposed_customers')} customers, {r.get('exposed_tickets')} receipts, "
        f"{r.get('exposed_stores')} of {r.get('total_stores')} stores. Based on observed data, not a forecast.",
        [(sql, res)], rows)


_READONLY = re.compile(r"^\s*(select|with)\b", re.I)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|create|alter|cache|uncache|refresh|grant|truncate)\b", re.I)


def run_readonly_sql(sql):
    """Escape hatch for exploration: one read-only SELECT on the database, max 50 rows."""
    if not sql or not _READONLY.match(sql) or _FORBIDDEN.search(sql) or ";" in sql.strip().rstrip(";"):
        raise ZetarisError("Only a single read-only SELECT/WITH statement is allowed.")
    res = run_sql(sql.strip().rstrip(";"), limit=50)
    return _evidence(f"Custom query returned {len(res['rows'])} row(s).", [(sql, res)], _rows(res, 50))


TOOLS = {
    "inspect_schema": (inspect_schema, "List tables and check the columns ChainGuard relies on. Args: none."),
    "observed_period": (observed_period, "First/last sale date and total store sales. Args: none."),
    "rank_dependency_candidates": (rank_dependency_candidates,
        "Rank manufacturers by dependency metrics (receipt reach, customers, revenue, sole-source classes). "
        "Args: order_by (receipt_reach|customers|revenue|sole_source_classes|classes_led|max_class_share), limit (<=25)."),
    "manufacturer_class_profile": (manufacturer_class_profile,
        "Classes a manufacturer sells in and its share of each. Args: manufacturer_id."),
    "customer_dependence": (customer_dependence,
        "Customers relying on a manufacturer for >=25% / >=50% of spend. Args: manufacturer_id."),
    "geographic_exposure": (geographic_exposure,
        "Revenue by store state and customer state for a manufacturer. Args: manufacturer_id."),
    "diversification_candidates": (diversification_candidates,
        "Other manufacturers in the same classes (candidates only). Args: manufacturer_id."),
    "simulate_unavailability": (simulate_unavailability,
        "Historical exposure if a manufacturer disappeared. Args: manufacturer_id."),
    "run_readonly_sql": (run_readonly_sql,
        f"One read-only SELECT over {DB} tables (prefix table names with {DB}.). Args: sql."),
}


def call_tool(name, args):
    if name not in TOOLS:
        raise ZetarisError(f"Unknown tool {name!r}. Available: {', '.join(TOOLS)}")
    fn = TOOLS[name][0]
    return fn(**(args or {}))
