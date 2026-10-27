"""ChainGuard agent loop.

Investigator (Nemotron via Nightshift) decides which Zetaris tool to call next,
reads the result, and decides again, until it can conclude. Every tool result
is stored as evidence E1, E2, ... An Evaluator (a separate Nemotron call) plus a
deterministic number check review the draft; if issues are found the
Investigator must investigate further or revise. Nothing is hardcoded: all
numbers come from Zetaris query results.
"""
import json
import re

from . import llm, state, tools
from .engines.world_model import get_world, stable_id, write_manifest
from .trace import Trace
from .zetaris import ZetarisError

MAX_TOOL_CALLS = 9
MAX_REVISIONS = 2

TOOL_LIST = "\n".join(f"- {name}: {desc}" for name, (_, desc) in tools.TOOLS.items())

INVESTIGATOR_PROMPT = f"""You are ChainGuard, a supply-chain resilience investigator for a retail business.
Its data lives in Zetaris (database {tools.DB}). In this data the "supplier" role is played by the
MANUFACTURER of each item (item.i_manufact_id / i_manufact). Sales link to items.

You investigate by calling tools, one at a time. Available tools:
{TOOL_LIST}

Reply with ONE JSON object and nothing else, in one of these two forms:
{{"action": "<tool name>", "args": {{...}}, "purpose": "<one short sentence: what you are checking and why>"}}
{{"action": "final", "critical_manufacturer_id": <id>, "critical_manufacturer": "<name>",
  "conclusion": "<2-3 sentences>",
  "why": ["<claim with numbers, citing evidence like (E3)>", ...],
  "recommendations": ["<what to investigate or diversify first>", ...],
  "limitations": ["<what the data does not show>", ...]}}

Rules:
- Start by inspecting the schema. Do not assume tables or columns beyond what tools report.
- Use at least two different dependency metrics before choosing a manufacturer; check customers and
  alternatives for your candidate before concluding.
- Every number you state must appear in a tool result. Cite the evidence id for each claim.
- Talk about "historical exposure" and "potential impact based on observed data", never predictions.
- Other manufacturers in the same class are "diversification candidates to investigate"; never claim
  they have capacity to replace anyone.
- Results cover the store sales channel only; say so in limitations.
- If a tool reports a data_quality finding (e.g. sales with no manufacturer recorded), mention it in
  limitations with its numbers.
- A "hidden" dependency is one that matters more than its revenue suggests: e.g. high receipt_reach
  (share of customer receipts containing its products) or many customers, with a small revenue share,
  or being the ONLY known maker of a product class (sole_source_classes). Compare more than one ranking.
- Product classes with a blank category or class are data gaps, not real classes: never present a
  blank class as a sole-source dependency. Identify manufacturers by manufacturer_id (names are
  inconsistent in this data).
- You have at most {MAX_TOOL_CALLS} tool calls.
"""

EVALUATOR_PROMPT = """You are the ChainGuard Evaluator. You check a draft conclusion against the evidence.
Approve only if ALL hold:
1. Every number in the draft appears in the evidence (allow rounding).
2. Each claim cites an evidence id that actually supports it.
3. The chosen manufacturer is justified by at least two different metrics from the evidence.
4. No predictions of the future and no claims that alternatives have spare capacity.
5. Limitations mention the store-sales-only scope.
Reply with ONE JSON object only:
{"approved": true|false, "issues": ["<specific problem and what evidence is missing>", ...]}
"""


def _strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json(text):
    text = _strip_think(text)
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in model reply")


def _compact(result, max_chars=3500):
    slim = {k: v for k, v in result.items() if k != "queries"}
    text = json.dumps(slim, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "...(truncated)"


# ------------------------------------------------- deterministic number check

_NUM = re.compile(r"(?<![A-Za-z#])-?\d[\d,]*\.?\d*%?")


def _evidence_numbers(evidence):
    found = []

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            found.append(float(x))
        elif isinstance(x, str):
            for tok in _NUM.findall(x):
                try:
                    found.append(float(tok.replace(",", "").rstrip("%")))
                except ValueError:
                    pass
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for e in evidence.values():
        walk(e["result"])
    return found


def unsupported_numbers(draft, evidence):
    """Numbers in the draft that cannot be matched to any evidence value (allowing
    rounding, percentages of shares, and millions/thousands formatting)."""
    known = _evidence_numbers(evidence)
    text = json.dumps({k: v for k, v in draft.items() if k != "action"}, ensure_ascii=False)
    text = text.replace("\u202f", " ").replace("\u00a0", " ")
    text = re.sub(r"\(?E\d+\)?", "", text)
    text = re.sub(r"(\d) %", r"\1%", text)
    text = re.sub(r"\btop \d+\b", "", text, flags=re.I)
    bad = []
    for tok in _NUM.findall(text):
        raw = tok.replace(",", "")
        is_pct = raw.endswith("%")
        try:
            val = float(raw.rstrip("%"))
        except ValueError:
            continue
        if abs(val) <= 10 and not is_pct:
            continue  # small counts/ordinals
        candidates = [val]
        if is_pct:
            candidates.append(val / 100)
        candidates += [val * 1e6, val * 1e3]  # "14.8 million", "90.9 thousand"
        ok = any(abs(c - k) <= max(abs(k) * 0.01, 0.006) for c in candidates for k in known)
        if not ok:
            bad.append(tok)
    return sorted(set(bad))


# ------------------------------------------------------------------ the loop

def _run_tool(trace, inv, name, args, purpose):
    trace.log("Investigator", "decide", tool=name, args=args, note=purpose)
    try:
        result = tools.call_tool(name, args)
    except (ZetarisError, TypeError, ValueError) as error:
        trace.log("Zetaris", "tool_error", tool=name, error=str(error))
        return None, f"TOOL ERROR from {name}: {error}"
    eid = state.add_evidence(inv, name, args, purpose, result)
    trace.log("Zetaris", "tool_result", tool=name, evidence_id=eid,
              note=result.get("summary"), queries=result.get("queries"))
    obs_id, n_facts = get_world().ingest(name, args, result, eid, inv.get("run_id") or trace.run_id)
    trace.log("WorldModel", "ingest", obs_id=obs_id, facts=n_facts, evidence_id=eid,
              note=f"{n_facts} facts from {eid}")
    return eid, f"{eid} = result of {name}({json.dumps(args)}):\n{_compact(result)}"


def evaluate(trace, question, inv, draft):
    issues = []
    bad = unsupported_numbers(draft, inv["evidence"])
    if bad:
        issues.append("These numbers are not found in any evidence: " + ", ".join(bad[:12]))
    cited = set(re.findall(r"E\d+", json.dumps(draft))) or set(inv["evidence"])
    evidence_text = "\n".join(f"{eid} ({e['tool']}): {_compact(e['result'], 1200)}"
                              for eid, e in inv["evidence"].items() if eid in cited)
    try:
        reply = llm.chat([
            {"role": "system", "content": EVALUATOR_PROMPT},
            {"role": "user", "content": f"Question: {question}\n\nEvidence cited by the draft:\n{evidence_text}\n\n"
                                        f"Draft:\n{json.dumps(draft)}"},
        ])
        verdict = parse_json(reply["content"])
    except ValueError:
        reply = {"model": reply.get("model") if isinstance(reply, dict) else None}
        verdict = {"approved": False, "issues": ["Evaluator reply was not valid JSON."]}
    except RuntimeError as error:
        reply = {"model": None}
        verdict = {"approved": False, "issues": [f"Evaluator could not run: {error}"]}
    issues += [str(i) for i in verdict.get("issues", []) if i]
    approved = bool(verdict.get("approved")) and not bad
    trace.log("Evaluator", "review", model=reply["model"], approved=approved, issues=issues,
              note="approved" if approved else f"{len(issues)} issue(s)")
    return approved, issues


def _trim(messages, keep_full=4, short=700):
    """Older tool observations are shortened (they stay in the evidence store in full)."""
    obs = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"].startswith("E")]
    old = set(obs[:-keep_full])
    out = []
    for i, m in enumerate(messages):
        if i in old and len(m["content"]) > short:
            m = {"role": m["role"], "content": m["content"][:short] + " ...(shortened; full result kept as evidence)"}
        out.append(m)
    return out


def investigate(question, trace=None, on_event=None):
    trace = trace or Trace()
    inv = state.new_investigation(trace.run_id, question)
    trace.log("User", "question", note=question)
    messages = [{"role": "system", "content": INVESTIGATOR_PROMPT},
                {"role": "user", "content": question}]
    world = get_world()
    prior = world.build_context(question, exclude_run=trace.run_id, budget_chars=2000)
    if prior["entities"]:
        trace.log("WorldModel", "context", note=f"{len(prior['entities'])} entities from earlier runs",
                  health=prior["health"])
        messages.append({"role": "user", "content":
            "WORLD MODEL context from earlier ChainGuard runs (use it to decide where to look; do NOT cite "
            "these numbers - re-check them with tools in this run):\n" + json.dumps(prior, default=str)[:2500]})
    tool_calls, revisions, bad_json = 0, 0, 0
    draft = None
    while True:
        reply = llm.chat(_trim(messages))
        trace.log("Investigator", "model_reply", model=reply["model"], ms=reply["ms"])
        messages.append({"role": "assistant", "content": reply["content"]})
        try:
            step = parse_json(reply["content"])
        except ValueError:
            bad_json += 1
            if bad_json > 2:
                raise RuntimeError("Model did not return valid JSON three times.")
            messages.append({"role": "user", "content": "Reply with ONE valid JSON object only."})
            continue

        action = step.get("action")
        if action == "final":
            draft = step
            trace.log("Investigator", "draft_conclusion", note=step.get("critical_manufacturer"))
            approved, issues = evaluate(trace, question, inv, draft)
            if approved or revisions >= MAX_REVISIONS:
                inv["approved"] = approved
                inv["open_issues"] = [] if approved else issues
                break
            revisions += 1
            messages.append({"role": "user", "content":
                "The Evaluator rejected the draft. Fix these issues, calling tools if evidence is missing, "
                "then give a new final JSON:\n- " + "\n- ".join(issues)})
            continue

        if tool_calls >= MAX_TOOL_CALLS:
            messages.append({"role": "user", "content": "Tool budget used up. Give your final JSON now."})
            continue
        tool_calls += 1
        _, observation = _run_tool(trace, inv, action, step.get("args") or {}, step.get("purpose", ""))
        messages.append({"role": "user", "content": observation + "\n\nDecide the next step (JSON only)."})

    inv["conclusion"] = draft
    state.save(inv)
    write_manifest(world)
    trace.log("ChainGuard", "done", note=f"approved={inv['approved']} evidence={len(inv['evidence'])}")
    return inv


def explain_why(run_id, follow_up="Why is this manufacturer the most critical dependency?"):
    """Answer 'Why?' from the stored investigation evidence, with citations."""
    inv = state.load(run_id)
    trace = Trace(run_id + "-why")
    mid = (inv.get("conclusion") or {}).get("critical_manufacturer_id")
    world_ctx = get_world().build_context(follow_up, focus_ids=[stable_id("manufacturer", mid)], budget_chars=1500)
    trace.log("WorldModel", "context", note=f"focus manufacturer {mid}")
    evidence_text = "\n".join(f"{eid} ({e['tool']}, purpose: {e['purpose']}): {_compact(e['result'], 1800)}"
                              for eid, e in inv["evidence"].items())
    reply = llm.chat([
        {"role": "system", "content":
            "You explain a finished ChainGuard investigation. Use ONLY the evidence given. Cite evidence ids. "
            "Explain step by step which data showed what. No predictions, no capacity claims. "
            'Reply as JSON: {"answer": "<plain-language explanation>", "evidence_used": ["E1", ...]}'},
        {"role": "user", "content": f"Conclusion: {json.dumps(inv.get('conclusion'))}\n\n"
                                    f"Evidence:\n{evidence_text}\n\n"
                                    f"World model (relationships and provenance):\n{json.dumps(world_ctx, default=str)[:1800]}\n\n"
                                    f"Question: {follow_up}"},
    ])
    trace.log("Explainer", "why", model=reply["model"])
    try:
        out = parse_json(reply["content"])
    except ValueError:
        out = {"answer": _strip_think(reply["content"]), "evidence_used": []}
    out["unsupported_numbers"] = unsupported_numbers(out, inv["evidence"])
    out["model"] = reply["model"]
    return out


def simulate(run_id, manufacturer_id):
    """Simulate a manufacturer becoming unavailable, using historical data only."""
    inv = state.load(run_id) if run_id else state.new_investigation(None, f"simulate {manufacturer_id}")
    trace = Trace((inv["run_id"] or "sim") + f"-sim-{manufacturer_id}")
    for name in ("simulate_unavailability", "customer_dependence", "diversification_candidates"):
        _run_tool(trace, inv, name, {"manufacturer_id": manufacturer_id}, "failure simulation")
    sim_ids = list(inv["evidence"])[-3:]
    evidence_text = "\n".join(f"{eid} ({inv['evidence'][eid]['tool']}): {_compact(inv['evidence'][eid]['result'], 2500)}"
                              for eid in sim_ids)
    reply = llm.chat([
        {"role": "system", "content":
            "Summarise a supplier-failure simulation for a business manager, using ONLY the evidence. "
            "Say 'historical exposure' / 'potential impact based on observed data'. Alternatives are only "
            "candidates to investigate. Cite evidence ids. Reply as JSON: "
            '{"headline": "...", "impact": ["..."], "first_actions": ["..."], "caveats": ["..."]}'},
        {"role": "user", "content": f"Manufacturer id {manufacturer_id}.\nEvidence:\n{evidence_text}"},
    ])
    trace.log("Simulator", "summary", model=reply["model"])
    try:
        out = parse_json(reply["content"])
    except ValueError:
        out = {"headline": _strip_think(reply["content"])}
    out["unsupported_numbers"] = unsupported_numbers(out, {k: inv["evidence"][k] for k in sim_ids})
    out["evidence"] = {k: inv["evidence"][k] for k in sim_ids}
    out["model"] = reply["model"]
    if inv.get("run_id"):
        state.save(inv)
    return out
