"""Per-node metrics, findings ("insights") and cross-plan node matching for EXPLAIN plans.

assets/core.js is a line-by-line JavaScript port of this module (the HTML app analyses pasted plans in the browser);
tests/test_planviz.py runs both on the sample plans and checks they agree.  Change both together.

Conventions (same as PostgreSQL's EXPLAIN):
  * "Plan Rows" and "Actual Rows" are per loop; buffers are totals over all loops; "Actual Total Time" is per loop.
  * Rows Removed by Filter / Join Filter / Index Recheck are per-loop averages.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, List, Optional

BUF_KEYS = ["Shared Hit Blocks", "Shared Read Blocks", "Shared Dirtied Blocks", "Shared Written Blocks",
            "Local Hit Blocks", "Local Read Blocks", "Local Dirtied Blocks", "Local Written Blocks",
            "Temp Read Blocks", "Temp Written Blocks"]
JOIN_TYPES = ("Nested Loop", "Hash Join", "Merge Join")
SCAN_TYPES = ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan", "CTE Scan",
              "Subquery Scan", "Function Scan", "Values Scan", "Tid Scan", "Tid Range Scan", "Foreign Scan",
              "WorkTable Scan", "Sample Scan", "Table Function Scan", "Named Tuplestore Scan", "Custom Scan")
UNION_TYPES = ("Append", "Merge Append", "Recursive Union", "BitmapAnd", "BitmapOr")
RESCAN_COSTLY = ("Seq Scan", "CTE Scan", "Materialize", "Subquery Scan", "Function Scan", "Values Scan", "Sort",
                 "Aggregate", "Hash Join", "Merge Join", "Nested Loop", "WorkTable Scan", "Bitmap Heap Scan",
                 "Append", "Result", "Gather", "Unique", "SetOp", "WindowAgg")
COND_KEYS = ("Hash Cond", "Merge Cond", "Join Filter", "Index Cond", "Recheck Cond", "Filter", "Group Key",
             "TID Cond", "One-Time Filter")
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
BLOCK = 8192

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def load_rules() -> Dict[str, Dict[str, str]]:
    with open(os.path.join(_ASSETS, "rules.json"), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------- formatting (mirrored in core.js)
def fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "–"
    if ms < 1:
        return f"{ms:.3f} ms"
    if ms < 10:
        return f"{ms:.2f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f} s" if s < 10 else f"{s:.0f} s"
    m, s = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "–"
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            x = v / div
            return (f"{x:.1f}" if abs(x) < 100 else f"{x:.0f}") + suf
    if a == int(a):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def fmt_bytes(b: Optional[float]) -> str:
    if b is None:
        return "–"
    for div, suf in ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "kB")):
        if b >= div:
            x = b / div
            return (f"{x:.1f}" if x < 100 else f"{x:.0f}") + " " + suf
    return f"{b:.0f} B"


def fmt_factor(lq: Optional[float]) -> str:
    """lq = log10(actual/estimate) -> '↑85×' (underestimate) / '↓12×' (overestimate) / '≈1×'."""
    if lq is None:
        return "–"
    f = 10 ** abs(lq)
    if f < 1.5:
        return "≈1×"
    txt = fmt_num(round(f, 1) if f < 10 else round(f))
    return ("↑" if lq > 0 else "↓") + txt + "×"


def parse_mem_kb(v: Any) -> Optional[float]:
    """'4MB' / '64kB' / '1GB' / '4096' (kB) -> kB."""
    if v is None:
        return None
    m = re.match(r"^\s*([\d.]+)\s*(B|kB|MB|GB|TB)?\s*$", str(v))
    if not m:
        return None
    return float(m.group(1)) * {"B": 1 / 1024, "kB": 1, "MB": 1024, "GB": 1024 ** 2, "TB": 1024 ** 3}[m.group(2) or "kB"]


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------- node labels
def node_label(r: Dict[str, Any]) -> str:
    """Same wording as text EXPLAIN."""
    nt = r.get("Node Type", "?")
    if nt == "Aggregate":
        base = {"Plain": "Aggregate", "Sorted": "GroupAggregate", "Hashed": "HashAggregate",
                "Mixed": "MixedAggregate"}.get(r.get("Strategy", "Plain"), "Aggregate")
        pm = r.get("Partial Mode", "Simple")
        return base if pm == "Simple" else f"{pm} {base}"
    if nt == "SetOp":
        return "HashSetOp" if r.get("Strategy") == "Hashed" else "SetOp"
    if nt in JOIN_TYPES:
        jt = r.get("Join Type", "Inner")
        if nt == "Nested Loop":
            lbl = "Nested Loop" if jt == "Inner" else f"Nested Loop {jt} Join"
        else:
            base = nt.replace(" Join", "")
            lbl = f"{base} Join" if jt == "Inner" else f"{base} {jt} Join"
        return ("Parallel " + lbl) if r.get("Parallel Aware") else lbl
    if nt == "ModifyTable":
        return r.get("Operation", "Modify")
    return f"Parallel {nt}" if r.get("Parallel Aware") else nt


def node_object(r: Dict[str, Any]) -> str:
    """'products p', 'cte current_brand_code cbc', 'idx products_status_idx' - what the node reads."""
    rel, alias, cte = r.get("Relation Name"), r.get("Alias"), r.get("CTE Name")
    if cte:
        return cte + (f" {alias}" if alias and alias != cte else "")
    if rel:
        name = f"{r['Schema']}.{rel}" if r.get("Schema") and r.get("Schema") != "public" else rel
        return name + (f" {alias}" if alias and alias != rel else "")
    if r.get("Function Name"):
        return r["Function Name"] + "()" + (f" {alias}" if alias and alias != r["Function Name"] else "")
    if alias and r.get("Node Type") in ("Subquery Scan", "Values Scan", "WorkTable Scan", "Table Function Scan"):
        return alias
    if r.get("Index Name") and r.get("Node Type") == "Bitmap Index Scan":
        return r["Index Name"]
    return ""


def _target(r: Dict[str, Any]) -> str:
    if r.get("CTE Name"):
        return f"cte:{r['CTE Name']}/{r.get('Alias') or ''}"
    if r.get("Relation Name"):
        return f"{r['Relation Name']}/{r.get('Alias') or ''}"
    if r.get("Function Name"):
        return f"fn:{r['Function Name']}/{r.get('Alias') or ''}"
    if r.get("Node Type") == "Bitmap Index Scan":
        return f"idx:{r.get('Index Name') or ''}"
    if r.get("Node Type") in ("Subquery Scan", "Values Scan", "WorkTable Scan") and r.get("Alias"):
        return f"q:{r['Alias']}"
    return ""


# ---------------------------------------------------------------------------- analysis
def analyze(obj: Dict[str, Any], label: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = []

    def build(raw: Dict[str, Any], parent: Optional[int], depth: int) -> int:
        r = {k: v for k, v in raw.items() if k != "Plans"}
        nid = len(nodes)
        rel = r.get("Parent Relationship") or ""
        n = {"id": nid, "parent": parent, "depth": depth, "children": [], "raw": r,
             "type": r.get("Node Type", "?"), "label": node_label(r), "object": node_object(r),
             "index": r.get("Index Name") or "", "rel": rel, "sub": r.get("Subplan Name") or "",
             "est": _num(r.get("Plan Rows")), "width": int(_num(r.get("Plan Width"))),
             "cost": _num(r.get("Total Cost")), "startup_cost": _num(r.get("Startup Cost")),
             "analyzed": "Actual Loops" in r}
        nodes.append(n)
        for ch in raw.get("Plans") or []:
            n["children"].append(build(ch, nid, depth + 1))
        return nid

    build(obj["Plan"], None, 0)
    root = nodes[0]
    analyzed = root["analyzed"]
    has_timing = analyzed and any("Actual Total Time" in n["raw"] for n in nodes if n["analyzed"])

    # -- rows, loops, q-error ----------------------------------------------------
    for n in nodes:
        r = n["raw"]
        n["loops"] = _num(r.get("Actual Loops")) if n["analyzed"] else None
        n["never"] = bool(n["analyzed"] and n["loops"] == 0)
        n["rows"] = _num(r.get("Actual Rows")) if n["analyzed"] else None
        lp = max(n["loops"] or 0, 1) if n["analyzed"] else 1
        n["est_total"] = n["est"] * lp
        n["act_total"] = (n["rows"] or 0) * (n["loops"] or 0) if n["analyzed"] else None
        if n["analyzed"] and not n["never"] and n["type"] != "ModifyTable":
            n["lq"] = math.log10(max(n["rows"], 1.0) / max(n["est"], 1.0))
        else:
            n["lq"] = None
        removed = sum(_num(r.get(k)) for k in ("Rows Removed by Filter", "Rows Removed by Join Filter",
                                                "Rows Removed by Index Recheck"))
        n["removed"] = removed * (n["loops"] or 0) if n["analyzed"] else None

    def ancestor_gather(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        p = n["parent"]
        while p is not None:
            if nodes[p]["type"] in ("Gather", "Gather Merge"):
                return nodes[p]
            p = nodes[p]["parent"]
        return None

    # -- inclusive time (ms, summed over loops; parallel workers counted as wall-clock) ------------------
    for n in nodes:
        t = n["raw"].get("Actual Total Time")
        if not n["analyzed"] or t is None:
            n["incl"] = None
            continue
        if n["never"]:
            n["incl"] = 0.0
            continue
        loops = n["loops"]
        g = ancestor_gather(n)
        div = 1.0
        if g is not None:
            workers = _num(g["raw"].get("Workers Launched", g["raw"].get("Workers Planned")))
            div = max(1.0, min(loops, workers + 1))
        n["incl"] = float(t) * loops / div

    def is_cte_init(c: Dict[str, Any]) -> bool:
        return c["rel"] == "InitPlan" and c["sub"].startswith("CTE ")

    # CTE plans run inside the CTE Scan that first pulls their rows, so their time / buffers are charged to the
    # CTE Scan nodes (greedily, in plan order) and not to the node the "CTE x" InitPlan hangs from.
    cte_root = {c["sub"][4:]: c for c in nodes if is_cte_init(c)}
    # Other InitPlans hang from the top node of their query level but run lazily inside the first node that
    # evaluates their output ("(InitPlan 3).col1" on PG17, "$0" before): charge them to that node.
    charged: Dict[int, int] = {}
    for x in nodes:
        if x["rel"] != "InitPlan" or is_cte_init(x) or x["parent"] is None:
            continue
        toks = _init_tokens(x["sub"])
        u = _first_user(nodes, nodes[x["parent"]], x, toks) if toks else None
        if u is not None and u["id"] != x["parent"]:
            charged[x["id"]] = u["id"]
    time_parent = {n["id"]: n["parent"] for n in nodes}
    time_parent.update(charged)
    for key in ["incl"] + BUF_KEYS:
        excl_key = "excl" if key == "incl" else "x:" + key
        for n in nodes:
            v = n.get(key) if key == "incl" else (n["raw"].get(key) if key in n["raw"] else None)
            if v is None:
                n[excl_key] = None
                continue
            sub = 0.0
            for cid in n["children"]:
                c = nodes[cid]
                if is_cte_init(c) or c["id"] in charged:
                    continue
                cv = c.get(key) if key == "incl" else c["raw"].get(key)
                sub += float(cv or 0)
            n[excl_key] = float(v) - sub
        for xid, uid in charged.items():
            x, u = nodes[xid], nodes[uid]
            xv = x.get(key) if key == "incl" else x["raw"].get(key)
            if u[excl_key] is not None:
                u[excl_key] -= float(xv or 0)
        for name, c in cte_root.items():
            cv = c.get(key) if key == "incl" else c["raw"].get(key)
            remaining = float(cv or 0)
            for s in nodes:
                if remaining <= 0:
                    break
                if s["type"] == "CTE Scan" and s["raw"].get("CTE Name") == name and s[excl_key] is not None:
                    take = min(remaining, max(s[excl_key], 0.0))
                    s[excl_key] -= take
                    remaining -= take
                    if key == "incl" and take > 0 and time_parent[c["id"]] == c["parent"]:
                        time_parent[c["id"]] = s["id"]
        for n in nodes:
            if n[excl_key] is not None:
                n[excl_key] = max(n[excl_key], 0.0)

    for n in nodes:
        n["buf"] = {k: _num(n["raw"].get(k)) for k in BUF_KEYS if k in n["raw"]}
        n["buf_excl"] = {k: n.pop("x:" + k) for k in BUF_KEYS if n.get("x:" + k) is not None}
        for k in BUF_KEYS:
            n.pop("x:" + k, None)

    exec_ms = obj.get("Execution Time")
    runtime = float(exec_ms) if exec_ms is not None else root.get("incl")
    for n in nodes:
        n["pct"] = (n["excl"] / runtime * 100) if (n.get("excl") is not None and runtime) else None

    # -- local estimation error: how much of this node's q-error is created here, not inherited ------------
    def lq0(c: Dict[str, Any]) -> float:
        return c["lq"] if c["lq"] is not None else 0.0

    for n in nodes:
        n["base_lq"] = None
        n["local_lq"] = None
        if n["lq"] is None:
            continue
        data = [nodes[c] for c in n["children"] if nodes[c]["rel"] not in ("InitPlan", "SubPlan")]
        t = n["type"]
        if t == "CTE Scan":
            src = cte_root.get(n["raw"].get("CTE Name") or "")
            base = lq0(src) if src is not None else 0.0
        elif not data or (t == "Aggregate" and n["raw"].get("Strategy", "Plain") == "Plain"):
            base = 0.0  # leaves, and plain aggregates (always one row per loop, whatever the input)
        elif t in JOIN_TYPES and len(data) >= 2:
            outer = next((c for c in data if c["rel"] == "Outer"), data[0])
            inner = next((c for c in data if c["rel"] == "Inner"), data[1])
            jt = n["raw"].get("Join Type", "Inner")
            if jt in ("Semi", "Anti"):
                base = lq0(outer)
            elif jt in ("Right Semi", "Right Anti"):
                base = lq0(inner)
            else:
                base = lq0(outer) + lq0(inner)
        elif t in UNION_TYPES:
            act = sum(c["rows"] or 0 for c in data if c["lq"] is not None)
            est = sum(c["est"] for c in data if c["lq"] is not None)
            base = math.log10(max(act, 1.0) / max(est, 1.0))
        else:
            base = lq0(data[0])
        n["base_lq"] = base
        n["local_lq"] = n["lq"] - base

    # -- scope (CTE / sub-plan the node belongs to) and hot path ---------------------------------------
    for n in nodes:
        scope, p = "main", n
        while p is not None:
            if p["rel"] in ("InitPlan", "SubPlan") and p["sub"]:
                scope = ("cte:" + p["sub"][4:]) if p["sub"].startswith("CTE ") else "sub"
                break
            p = nodes[p["parent"]] if p["parent"] is not None else None
        n["scope"] = scope
        n["hot"] = False
    for n in nodes:
        n["time_parent"] = time_parent[n["id"]]
    if has_timing:
        cur, seen = root, set()
        while cur is not None and cur["id"] not in seen:
            cur["hot"] = True
            seen.add(cur["id"])
            kids = [k for k in nodes if k["time_parent"] == cur["id"] and k.get("incl") is not None]
            if not kids:
                break
            nxt = max(kids, key=lambda c: c["incl"])
            if runtime and nxt["incl"] < runtime * 0.05:
                break
            cur = nxt

    summary = _summary(obj, nodes, runtime, analyzed, has_timing, meta or {})
    insights = _insights(obj, nodes, summary, cte_root)
    for n in nodes:
        n["insights"] = [i for i, ins in enumerate(insights) if ins.get("node") == n["id"]]
    return {"label": label, "summary": summary, "nodes": nodes, "insights": insights}


def _summary(obj: Dict[str, Any], nodes: List[Dict[str, Any]], runtime: Optional[float], analyzed: bool,
             has_timing: bool, meta: Dict[str, Any]) -> Dict[str, Any]:
    root = nodes[0]
    jit = obj.get("JIT") or {}
    jt = (jit.get("Timing") or {}).get("Total")
    if isinstance(jt, dict):
        jt = jt.get("Total")
    triggers = obj.get("Triggers") or []
    lqs = [n["lq"] for n in nodes if n["lq"] is not None]
    worst = max((n for n in nodes if n["lq"] is not None), key=lambda n: abs(n["lq"]), default=None)
    temp = sum(_num(root["raw"].get(k)) for k in ("Temp Read Blocks", "Temp Written Blocks"))
    return {
        "analyzed": analyzed, "has_timing": has_timing, "nodes": len(nodes),
        "execution_ms": obj.get("Execution Time"), "planning_ms": obj.get("Planning Time"), "runtime_ms": runtime,
        "jit_ms": float(jt) if jt is not None else None,
        "trigger_ms": sum(_num(t.get("Time")) for t in triggers) if triggers else None,
        "triggers": [{"name": t.get("Trigger Name"), "relation": t.get("Relation"), "ms": _num(t.get("Time")),
                      "calls": _num(t.get("Calls"))} for t in triggers],
        "settings": obj.get("Settings") or {}, "query_id": obj.get("Query Identifier"),
        "max_abs_lq": max((abs(x) for x in lqs), default=None), "worst_node": worst["id"] if worst else None,
        "shared_hit": _num(root["raw"].get("Shared Hit Blocks")) if "Shared Hit Blocks" in root["raw"] else None,
        "shared_read": _num(root["raw"].get("Shared Read Blocks")) if "Shared Read Blocks" in root["raw"] else None,
        "temp_blocks": temp if any(k in root["raw"] for k in ("Temp Read Blocks", "Temp Written Blocks")) else None,
        "cost": root["cost"], "major": _infer_major(obj, nodes, meta),
    }


_PG17_PARAM = re.compile(r"\((?:hashed )?(?:SubPlan|InitPlan) \d+\)\.col\d+")


def _infer_major(obj: Dict[str, Any], nodes: List[Dict[str, Any]], meta: Dict[str, Any]) -> Optional[int]:
    vn = meta.get("server_version_num")
    if vn:
        try:
            return int(vn) // 10000
        except (TypeError, ValueError):
            pass
    for n in nodes:
        for v in n["raw"].values():
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, str) and _PG17_PARAM.search(x):
                    return 17
    return 14 if obj.get("Query Identifier") is not None else None


def _init_tokens(sub: str) -> List[str]:
    """How expressions refer to an InitPlan's output: '(InitPlan 3)' on PG17+, '$0' ('InitPlan 1 (returns $0)') before."""
    toks = []
    m = re.match(r"InitPlan (\d+)", sub or "")
    if m:
        toks.append(f"(InitPlan {m.group(1)})")
    toks += re.findall(r"\$\d+", sub or "")
    return toks


def _mentions(raw: Dict[str, Any], toks: List[str]) -> bool:
    text = json.dumps({k: v for k, v in raw.items() if k not in ("Subplan Name", "Plans")}, ensure_ascii=False)
    for t in toks:
        if t.startswith("$"):
            if re.search(re.escape(t) + r"(?!\d)", text):
                return True
        elif t in text:
            return True
    return False


def _first_user(nodes: List[Dict[str, Any]], top: Dict[str, Any], skip: Dict[str, Any],
                toks: List[str]) -> Optional[Dict[str, Any]]:
    """First node (plan order) under 'top', outside 'skip', whose expressions use one of toks."""
    stack = [top]
    while stack:
        m = stack.pop()
        if m is skip:
            continue
        if _mentions(m["raw"], toks):
            return m
        stack.extend(nodes[c] for c in reversed(m["children"]))
    return None


def _conds(r: Dict[str, Any]) -> str:
    out = []
    for k in COND_KEYS:
        v = r.get(k)
        if v:
            out.append(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
    return "; ".join(out)


def _hash_budget_kb(settings: Dict[str, Any], major: Optional[int]) -> Dict[str, Any]:
    wm = parse_mem_kb(settings.get("work_mem")) or 4096.0
    hm_set = settings.get("hash_mem_multiplier")
    if hm_set is not None:
        hm, hm_src = float(hm_set), "Settings"
    elif major is not None and major <= 14:
        hm, hm_src = 1.0, "mặc định PG13/14"
    else:
        hm, hm_src = 2.0, "mặc định PG15+"
    return {"work_mem_kb": wm, "hash_mem_multiplier": hm, "budget_kb": wm * hm, "hm_source": hm_src,
            "work_mem_source": "Settings" if settings.get("work_mem") else "mặc định"}


def _insights(obj: Dict[str, Any], nodes: List[Dict[str, Any]], s: Dict[str, Any],
              cte_root: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    R = s["runtime_ms"]

    def add(rule: str, sev: str, nid: Optional[int], **p: Any) -> None:
        out.append({"rule": rule, "sev": sev, "node": nid, "p": p})

    def share(v: Optional[float]) -> float:
        return (v / R) if (v is not None and R) else 0.0

    def name(n: Dict[str, Any]) -> str:
        return (n["label"] + (" " + n["object"] if n["object"] else "")).strip()

    if not s["analyzed"]:
        add("NO_ANALYZE", "info", None)
    elif not s["has_timing"]:
        add("NO_TIMING", "info", None)

    # hot spots
    hot = sorted((n for n in nodes if n.get("excl") is not None), key=lambda n: -n["excl"])
    for n in hot[:3]:
        sh = share(n["excl"])
        if sh >= 0.25:
            add("HOTSPOT", "high" if sh >= 0.5 else "medium", n["id"], node=name(n), pct=round(sh * 100),
                excl=fmt_ms(n["excl"]), runtime=fmt_ms(R))

    # sub-plans re-executed per outer row
    budget = _hash_budget_kb(s["settings"], s["major"])
    flagged_sub = set()
    for n in nodes:
        if n["rel"] != "SubPlan" or not n["analyzed"] or (n["loops"] or 0) < 10:
            continue
        parent = nodes[n["parent"]] if n["parent"] is not None else None
        ptxt = json.dumps(parent["raw"], ensure_ascii=False) if parent else ""
        num = re.sub(r"\D", "", n["sub"])
        if re.search(r"hashed (?:\()?SubPlan " + num + r"\b", ptxt):
            continue
        sh = share(n.get("incl"))
        sev = "critical" if sh >= 0.5 else ("high" if sh >= 0.2 else ("medium" if sh >= 0.05 or not R else "low"))
        any_all = bool(re.search(r"\b(?:ANY|ALL) \(", ptxt) or "NOT (ANY" in ptxt)
        need_kb = n["est"] * (((n["width"] + 7) // 8) * 8 + 24) / 1024
        add("SUBPLAN_RESCAN", sev, n["id"], sub=n["sub"], loops=fmt_num(n["loops"]), rows=fmt_num(n["rows"]),
            read=fmt_num(n["act_total"]), pct=round(sh * 100), notin=any_all, est=fmt_num(n["est"]), width=n["width"],
            need=fmt_bytes(need_kb * 1024), budget=fmt_bytes(budget["budget_kb"] * 1024),
            work_mem=fmt_bytes(budget["work_mem_kb"] * 1024), hm=f"{budget['hash_mem_multiplier']:g}",
            hm_source=budget["hm_source"], fits=need_kb <= budget["budget_kb"],
            need_work_mem=fmt_bytes(math.ceil(need_kb / budget["hash_mem_multiplier"]) * 1024))
        flagged_sub.add(n["id"])

    # other expensive re-scans (inner side of a Nested Loop, ...)
    def under_flagged(n: Dict[str, Any]) -> bool:
        p = n
        while p is not None:
            if p["id"] in flagged_sub:
                return True
            p = nodes[p["parent"]] if p["parent"] is not None else None
        return False

    for n in nodes:
        if n["type"] not in RESCAN_COSTLY or not n["analyzed"] or (n["loops"] or 0) < 1000 or n["rel"] == "SubPlan":
            continue
        if under_flagged(n):
            continue
        sh = share(n.get("incl"))
        if sh < 0.1 and (n["act_total"] or 0) < 1e6:
            continue
        add("RESCAN", "high" if sh >= 0.5 else "medium", n["id"], node=name(n), loops=fmt_num(n["loops"]),
            rows=fmt_num(n["rows"]), pct=round(sh * 100))

    # where the misestimates are born (only where the estimate itself is off ≥10×: a node that cancels an error
    # inherited from its input is not a problem, and that input error is reported where it was born)
    origins = [n for n in nodes if n["local_lq"] is not None and abs(n["local_lq"]) >= 1 and abs(n["lq"]) >= 1]
    origins.sort(key=lambda n: -abs(n["local_lq"]))
    for n in origins[:6]:
        a, sh = abs(n["local_lq"]), share(n.get("incl"))
        sev = "high" if (a >= 3 or (a >= 2 and sh >= 0.2)) else ("medium" if (a >= 2 or sh >= 0.2) else "low")
        cte = n["type"] == "CTE Scan"
        add("MISEST", sev, n["id"], node=name(n), dir="thiếu" if n["local_lq"] > 0 else "thừa",
            local=fmt_factor(n["local_lq"]), total=fmt_factor(n["lq"]), base=fmt_factor(n["base_lq"]),
            est=fmt_num(n["est"]), rows=fmt_num(n["rows"]), cond=_conds(n["raw"]) or "–", cte=cte)

    # memory spills
    for n in nodes:
        r = n["raw"]
        sh = share(n.get("incl"))
        small = bool(R) and sh < 0.05
        if r.get("Sort Space Type") == "Disk":
            kb = _num(r.get("Sort Space Used"))
            add("SORT_SPILL", "low" if small else "medium", n["id"], node=name(n), disk=fmt_bytes(kb * 1024),
                need=fmt_bytes(kb * 1024 * 2))
        hb, ob = _num(r.get("Hash Batches")), _num(r.get("Original Hash Batches"))
        if n["type"] == "Hash" and hb > 1:
            peak = _num(r.get("Peak Memory Usage"))
            add("HASH_SPILL", "low" if small else "medium", n["id"], batches=fmt_num(hb), orig=fmt_num(ob or hb),
                grew=bool(ob and ob < hb), peak=fmt_bytes(peak * 1024), need=fmt_bytes(peak * hb * 1024))
        if n["type"] == "Aggregate" and (_num(r.get("HashAgg Batches")) > 1 or _num(r.get("Disk Usage")) > 0):
            add("HASHAGG_SPILL", "low" if small else "medium", n["id"], batches=fmt_num(_num(r.get("HashAgg Batches"))),
                disk=fmt_bytes(_num(r.get("Disk Usage")) * 1024))
    spilled = {i["node"] for i in out if i["rule"] in ("SORT_SPILL", "HASH_SPILL", "HASHAGG_SPILL")}
    for n in nodes:
        tw = (n["buf_excl"] or {}).get("Temp Written Blocks", 0)
        if tw >= 12800 and n["id"] not in spilled:
            add("TEMP_IO", "medium" if tw >= 131072 else "low", n["id"], node=name(n), size=fmt_bytes(tw * BLOCK))

    # wasted reads
    for n in nodes:
        r = n["raw"]
        if not n["analyzed"] or n["type"] not in SCAN_TYPES:
            continue
        removed = n["removed"] or 0
        act = n["act_total"] or 0
        sh = share(n.get("incl"))
        if removed >= 100000 and removed >= 9 * act and (sh >= 0.05 or not R):
            add("FILTER_WASTE", "medium" if sh >= 0.2 else "low", n["id"], node=name(n), removed=fmt_num(removed),
                kept=fmt_num(act), pct=round(removed / max(removed + act, 1) * 100), seq=n["type"] == "Seq Scan",
                cte=n["type"] == "CTE Scan", idx=n["type"] not in ("Seq Scan", "CTE Scan"),
                cond=_conds(r) or "–")
        if _num(r.get("Lossy Heap Blocks")) > 0 and _num(r.get("Rows Removed by Index Recheck")) * (n["loops"] or 0) >= 10000:
            add("LOSSY_BITMAP", "low", n["id"], lossy=fmt_num(_num(r.get("Lossy Heap Blocks"))),
                recheck=fmt_num(_num(r.get("Rows Removed by Index Recheck")) * (n["loops"] or 0)))
        hf = _num(r.get("Heap Fetches")) * (n["loops"] or 0)
        if n["type"] == "Index Only Scan" and hf >= 10000 and hf >= 0.5 * max(act, 1):
            add("HEAP_FETCHES", "low", n["id"], fetches=fmt_num(hf), rows=fmt_num(act))

    for n in nodes:
        r = n["raw"]
        if n["type"] in ("Gather", "Gather Merge") and "Workers Launched" in r \
                and _num(r.get("Workers Launched")) < _num(r.get("Workers Planned")):
            add("WORKERS", "low", n["id"], planned=fmt_num(_num(r.get("Workers Planned"))),
                launched=fmt_num(_num(r.get("Workers Launched"))))
        if n["type"] == "Memoize" and _num(r.get("Cache Evictions")) > 0:
            hits, misses = _num(r.get("Cache Hits")), _num(r.get("Cache Misses"))
            if hits < misses:
                add("MEMOIZE", "low", n["id"], hits=fmt_num(hits), misses=fmt_num(misses),
                    evictions=fmt_num(_num(r.get("Cache Evictions"))))

    E = s["execution_ms"]
    if s["jit_ms"] is not None and E and s["jit_ms"] >= 0.1 * E and s["jit_ms"] >= 100:
        add("JIT", "medium", None, jit=fmt_ms(s["jit_ms"]), pct=round(s["jit_ms"] / E * 100))
    if s["trigger_ms"] and E and s["trigger_ms"] >= 0.1 * E:
        top = max(s["triggers"], key=lambda t: t["ms"])
        add("TRIGGERS", "high" if s["trigger_ms"] >= 0.5 * E else "medium", None, ms=fmt_ms(s["trigger_ms"]),
            pct=round(s["trigger_ms"] / E * 100), top=top["name"] or "?", calls=fmt_num(top["calls"]))
    P = s["planning_ms"]
    if P is not None and P >= 50 and (E is None or P > E):
        add("PLANNING", "low", None, planning=fmt_ms(P), exec=fmt_ms(E))
    never = sum(1 for n in nodes if n["never"])
    if never:
        add("NEVER_EXECUTED", "info", None, count=never)

    out.sort(key=lambda i: (SEV_ORDER[i["sev"]], i["node"] if i["node"] is not None else -1))
    return out


# ---------------------------------------------------------------------------- matching two plans
def _signature(nodes: List[Dict[str, Any]], n: Dict[str, Any]) -> str:
    leaves: List[str] = []

    def walk(m: Dict[str, Any], top: bool) -> None:
        if not top and m["rel"] in ("InitPlan", "SubPlan"):
            return
        t = _target(m["raw"])
        if t:
            leaves.append(t)
        for c in m["children"]:
            walk(nodes[c], False)

    walk(n, True)
    return n["scope"] + "|" + ",".join(sorted(leaves))


def _category(n: Dict[str, Any]) -> str:
    if n["type"] in SCAN_TYPES:
        return "scan"
    if n["type"] in JOIN_TYPES:
        return "join"
    return n["type"]


def match(a: Dict[str, Any], b: Dict[str, Any]) -> List[List[int]]:
    """Pairs [node id in a, node id in b] of nodes doing the same job in two plans of one statement."""
    na, nb = a["nodes"], b["nodes"]
    sa = [_signature(na, n) for n in na]
    sb = [_signature(nb, n) for n in nb]
    pairs: Dict[int, int] = {0: 0}
    used = {0}
    for keyf in (lambda n, s: s + "#" + n["type"], lambda n, s: s + "#" + _category(n)):
        buckets: Dict[str, List[int]] = {}
        for n in nb:
            if n["id"] not in used:
                buckets.setdefault(keyf(n, sb[n["id"]]), []).append(n["id"])
        for n in na:
            if n["id"] in pairs:
                continue
            lst = buckets.get(keyf(n, sa[n["id"]]))
            while lst and lst[0] in used:
                lst.pop(0)
            if lst:
                j = lst.pop(0)
                pairs[n["id"]] = j
                used.add(j)
    return [[i, pairs[i]] for i in sorted(pairs)]


def render_insight(ins: Dict[str, Any], rules: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Fill the rules.json templates: '{name}' -> value; '{?flag}text{/flag}' kept only when flag is truthy."""
    tpl = rules.get(ins["rule"], {})
    p = ins["p"]

    def fill(t: str) -> str:
        t = re.sub(r"\{\?(\w+)\}(.*?)\{/\1\}", lambda m: m.group(2) if p.get(m.group(1)) else "", t, flags=re.S)
        t = re.sub(r"\{!(\w+)\}(.*?)\{/\1\}", lambda m: "" if p.get(m.group(1)) else m.group(2), t, flags=re.S)
        return re.sub(r"\{(\w+)\}", lambda m: str(p.get(m.group(1), m.group(0))), t)

    return {k: fill(tpl.get(k, "")) for k in ("title", "detail", "fix")}
