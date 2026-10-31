"""Parser for text-format EXPLAIN / EXPLAIN ANALYZE output (PG 9.6 .. 18).

Produces the same dict structure as EXPLAIN (FORMAT JSON) so the rest of the tool is
format-agnostic.  Coverage is pragmatic: node lines, the attributes the rules use,
Planning/Execution time, JIT, Triggers and Settings.  Unknown lines are kept in
"Extra" so nothing is silently lost.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

NODE_RE = re.compile(
    r"^(?P<indent>\s*)(?:->\s+)?(?P<body>.+?)\s+"
    r"\(cost=(?P<sc>[\d.]+)\.\.(?P<tc>[\d.]+)\s+rows=(?P<rows>[\d.]+)\s+width=(?P<w>\d+)\)"
    r"(?:\s+\((?:actual\s+)?(?:time=(?P<as>[\d.]+)\.\.(?P<at>[\d.]+)\s+)?rows=(?P<ar>[\d.]+)\s+loops=(?P<al>\d+)\)"
    r"|\s+\((?P<never>never executed)\))?\s*$"
)
SUBPLAN_RE = re.compile(r"^(?P<indent>\s*)(?P<kind>CTE|InitPlan|SubPlan)\s*(?P<name>.*?)\s*$")
JOIN_RE = re.compile(
    r"^(?P<par>Parallel\s+)?(?P<base>Hash|Merge)\s+(?:(?P<jt>Left Anti|Left Semi|Right Anti|Right Semi|Left|Right|Full|Semi|Anti)\s+)?Join$"
)
NL_RE = re.compile(r"^Nested Loop(?:\s+(?P<jt>Left Anti|Left Semi|Right Anti|Right Semi|Left|Right|Full|Semi|Anti)\s+Join)?$")
AGG_RE = re.compile(r"^(?:(?P<pm>Partial|Finalize)\s+)?(?P<kind>HashAggregate|GroupAggregate|MixedAggregate|Aggregate)$")
SIZE_RE = re.compile(r"(-?[\d.]+)\s*(kB|MB|GB|B)?")

AGG_STRATEGY = {"HashAggregate": "Hashed", "GroupAggregate": "Sorted", "MixedAggregate": "Mixed", "Aggregate": "Plain"}
MODIFY_OPS = ("Insert", "Update", "Delete", "Merge")


def _kb(val: str) -> Optional[float]:
    m = SIZE_RE.search(val)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "kB"
    return num * {"B": 1 / 1024, "kB": 1, "MB": 1024, "GB": 1024 * 1024}[unit]


def _parse_body(body: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    rel = alias = idx = None
    head = body
    m = re.match(r"^(?P<head>.*?)\s+using\s+(?P<idx>\S+)(?:\s+on\s+(?P<rest>.+))?$", body)
    if m and ("Index" in m.group("head")):
        head, idx = m.group("head"), m.group("idx")
        rest = m.group("rest")
    else:
        m2 = re.match(r"^(?P<head>.*?)\s+on\s+(?P<rest>.+)$", body)
        rest = None
        if m2:
            head, rest = m2.group("head"), m2.group("rest")
    if rest:
        toks = rest.split()
        rel = toks[0]
        alias = toks[1] if len(toks) > 1 else None
    head = head.strip()

    if head.startswith("Parallel ") and not JOIN_RE.match(head):
        d["Parallel Aware"] = True
        head = head[len("Parallel "):]

    if NL_RE.match(head):
        d["Node Type"] = "Nested Loop"
        d["Join Type"] = NL_RE.match(head).group("jt") or "Inner"
    elif JOIN_RE.match(head):
        jm = JOIN_RE.match(head)
        d["Node Type"] = f"{jm.group('base')} Join"
        d["Join Type"] = jm.group("jt") or "Inner"
        if jm.group("par"):
            d["Parallel Aware"] = True
    elif AGG_RE.match(head):
        am = AGG_RE.match(head)
        d["Node Type"] = "Aggregate"
        d["Strategy"] = AGG_STRATEGY[am.group("kind")]
        d["Partial Mode"] = am.group("pm") or "Simple"
    elif head in ("HashSetOp", "SetOp"):
        d["Node Type"] = "SetOp"
        d["Strategy"] = "Hashed" if head == "HashSetOp" else "Sorted"
    elif head in MODIFY_OPS:
        d["Node Type"] = "ModifyTable"
        d["Operation"] = head
    else:
        d["Node Type"] = head

    nt = d["Node Type"]
    if idx:
        d["Index Name"] = idx
    if rel:
        if nt == "CTE Scan":
            d["CTE Name"] = rel
        elif nt in ("Function Scan",):
            d["Function Name"] = rel.split("(")[0]
        elif nt in ("Subquery Scan", "Values Scan", "WorkTable Scan", "Named Tuplestore Scan", "Table Function Scan"):
            pass
        else:
            d["Relation Name"] = rel.split(".")[-1]
            if "." in rel:
                d["Schema"] = rel.split(".")[0]
        d["Alias"] = alias or rel.split(".")[-1]
    return d


def _split_kv(text: str) -> List[Tuple[str, str]]:
    """'Buckets: 1024  Batches: 1  Memory Usage: 9kB' -> [(Buckets,1024),(Batches,1),...]."""
    parts = re.split(r"\s{2,}", text.strip())
    out = []
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            out.append((k.strip(), v.strip()))
    return out


def _apply_attr(node: Dict[str, Any], key: str, val: str, line: str) -> None:
    nt = node.get("Node Type")
    if key == "Buffers":
        for scope, body in re.findall(r"(shared|local|temp)\s+([^,]+)", val):
            for k, n in re.findall(r"(hit|read|dirtied|written)=(\d+)", body):
                node[f"{scope.capitalize()} {k.capitalize()} Blocks"] = int(n)
        return
    if key == "I/O Timings":
        m = re.search(r"(?:shared\s+)?read=([\d.]+)", val)
        if m:
            node["I/O Read Time"] = float(m.group(1))
        m = re.search(r"(?:shared\s+)?write=([\d.]+)", val)
        if m:
            node["I/O Write Time"] = float(m.group(1))
        return
    if key == "Buckets":
        m = re.match(r"(\d+)(?:\s+\(originally\s+(\d+)\))?", val)
        if m:
            node["Hash Buckets"] = int(m.group(1))
            node["Original Hash Buckets"] = int(m.group(2) or m.group(1))
        return
    if key == "Batches":
        m = re.match(r"(\d+)(?:\s+\(originally\s+(\d+)\))?", val)
        if m:
            if nt == "Hash":
                node["Hash Batches"] = int(m.group(1))
                node["Original Hash Batches"] = int(m.group(2) or m.group(1))
            else:
                node["HashAgg Batches"] = int(m.group(1))
        return
    if key in ("Memory Usage", "Memory", "Peak Memory Usage"):
        kb = _kb(val)
        if nt == "Sort" or nt == "Incremental Sort":
            node["Sort Space Used"] = kb
            node["Sort Space Type"] = "Memory"
        else:
            node["Peak Memory Usage"] = kb
        return
    if key in ("Disk Usage", "Disk"):
        kb = _kb(val)
        if nt in ("Sort", "Incremental Sort"):
            node["Sort Space Used"] = kb
            node["Sort Space Type"] = "Disk"
        else:
            node["Disk Usage"] = kb
        return
    if key in ("Hits", "Misses", "Evictions", "Overflows"):
        node[f"Cache {key}"] = int(float(val))
        return
    if key in ("Rows Removed by Filter", "Rows Removed by Join Filter", "Rows Removed by Index Recheck",
               "Heap Fetches", "Workers Planned", "Workers Launched", "Planned Partitions",
               "Pre-sorted Groups", "Full-sort Groups", "Heap Blocks"):
        try:
            node[key] = float(val.split()[0]) if key.startswith("Rows Removed") else int(float(val.split()[0]))
        except ValueError:
            node[key] = val
        return
    if key in ("Sort Key", "Group Key", "Presorted Key", "Cache Key", "Hash Key"):
        node[key] = [s.strip() for s in _split_top_commas(val)]
        return
    node[key] = val


def _split_top_commas(s: str) -> List[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def parse_text_plan(text: str) -> Dict[str, Any]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    root: Optional[Dict[str, Any]] = None
    stack: List[Tuple[int, Dict[str, Any]]] = []  # (indent, node)
    meta: Dict[str, Any] = {}
    pending_rel: Optional[Tuple[int, str, str]] = None  # (indent, Parent Relationship, Subplan Name)
    section: Optional[str] = None  # 'JIT'
    jit: Dict[str, Any] = {}
    triggers: List[Dict[str, Any]] = []

    first_node = next((ln for ln in lines if NODE_RE.match(ln)), lines[0] if lines else "")
    base_indent = len(first_node) - len(first_node.lstrip())
    for ln in lines:
        stripped = ln.strip()
        ind_now = len(ln) - len(ln.lstrip())
        top = ind_now <= base_indent
        # ---- top-level summary lines ------------------------------------
        m = re.match(r"^Planning Time:\s*([\d.]+)\s*ms", stripped)
        if m and top:
            meta["Planning Time"] = float(m.group(1)); section = None; continue
        m = re.match(r"^Execution Time:\s*([\d.]+)\s*ms", stripped)
        if m and top:
            meta["Execution Time"] = float(m.group(1)); section = None; continue
        if stripped.startswith("Total runtime:"):
            meta["Execution Time"] = float(re.findall(r"[\d.]+", stripped)[0]); continue
        if stripped == "JIT:" and top:
            section = "JIT"; continue
        if section == "JIT" and not top:
            if stripped.startswith("Functions:"):
                jit["Functions"] = int(stripped.split(":")[1])
            elif stripped.startswith("Options:"):
                jit["Options"] = {k.strip(): v.strip() == "true" for k, v in
                                  (p.strip().rsplit(" ", 1) for p in stripped.split(":", 1)[1].split(","))}
            elif stripped.startswith("Timing:"):
                t = {}
                for name, val in re.findall(r"(Generation|Inlining|Optimization|Emission|Total)\s+([\d.]+)\s*ms", stripped):
                    t[name] = float(val)
                jit["Timing"] = t
            continue
        m = re.match(r"^Trigger\s+(?P<name>.+?):\s+time=(?P<t>[\d.]+)\s+calls=(?P<c>\d+)", stripped)
        if m and top:
            name = m.group("name")
            cm = re.match(r"for constraint (\S+)", name)
            triggers.append({"Trigger Name": name, "Constraint Name": cm.group(1) if cm else None,
                             "Time": float(m.group("t")), "Calls": int(m.group("c"))})
            continue
        if stripped.startswith("Settings:") and top:
            sett = {}
            for k, v in re.findall(r"(\w+)\s*=\s*'([^']*)'", stripped):
                sett[k] = v
            meta["Settings"] = sett
            continue
        section = None

        # ---- plan nodes ------------------------------------------------
        nm = NODE_RE.match(ln)
        if nm:
            indent = ind_now
            node = _parse_body(nm.group("body").strip())
            node["Startup Cost"] = float(nm.group("sc"))
            node["Total Cost"] = float(nm.group("tc"))
            node["Plan Rows"] = float(nm.group("rows"))
            node["Plan Width"] = int(nm.group("w"))
            if nm.group("al") is not None:
                if nm.group("as") is not None:
                    node["Actual Startup Time"] = float(nm.group("as"))
                    node["Actual Total Time"] = float(nm.group("at"))
                node["Actual Rows"] = float(nm.group("ar"))
                node["Actual Loops"] = int(nm.group("al"))
            elif nm.group("never"):
                node["Actual Rows"] = 0.0
                node["Actual Loops"] = 0
                node["Actual Total Time"] = 0.0
            node["Plans"] = []
            if root is None:
                root = node
                stack = [(indent, node)]
                continue
            # parent = last node on stack with indent < this indent
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else root
            if pending_rel and pending_rel[0] < indent:
                node["Parent Relationship"] = pending_rel[1]
                if pending_rel[2]:
                    node["Subplan Name"] = pending_rel[2]
                pending_rel = None
            else:
                sibs = [c for c in parent["Plans"] if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
                if parent.get("Node Type") in ("Nested Loop", "Hash Join", "Merge Join"):
                    node["Parent Relationship"] = "Outer" if not sibs else "Inner"
                elif parent.get("Node Type") in ("Append", "Merge Append"):
                    node["Parent Relationship"] = "Member"
                elif parent.get("Node Type") == "Subquery Scan":
                    node["Parent Relationship"] = "Subquery"
                else:
                    node["Parent Relationship"] = "Outer"
            parent["Plans"].append(node)
            stack.append((indent, node))
            continue

        sm = SUBPLAN_RE.match(ln)
        if sm and stack and not re.match(r"^\s*(CTE Scan)", ln) and ":" not in stripped.split(" ")[0]:
            kind, name = sm.group("kind"), sm.group("name")
            rel = "InitPlan" if kind in ("CTE", "InitPlan") else "SubPlan"
            sub_name = f"CTE {name}" if kind == "CTE" else f"{kind} {name}".strip()
            pending_rel = (len(sm.group("indent")), rel, sub_name)
            continue

        # ---- attribute lines: attach to deepest node with smaller indent ---
        if stack and ":" in stripped:
            ind = len(ln) - len(ln.lstrip())
            target = None
            for nind, nd in reversed(stack):
                if nind < ind:
                    target = nd
                    break
            target = target or stack[-1][1]
            kvs = _split_kv(stripped)
            first_key = stripped.split(":", 1)[0]
            if len(kvs) > 1 and first_key in ("Buckets", "Hits", "Batches", "Sort Method", "Planned Partitions",
                                               "Full-sort Groups", "Pre-sorted Groups"):
                for k, v in kvs:
                    _apply_attr(target, k, v, stripped)
                    if k == "Sort Method":
                        target["Sort Method"] = v
            else:
                k, v = stripped.split(":", 1)
                k, v = k.strip(), v.strip()
                if k == "Sort Method":
                    sm2 = re.match(r"(.+?)\s{2,}(Memory|Disk):\s*(.+)$", v)
                    if sm2:
                        target["Sort Method"] = sm2.group(1).strip()
                        target["Sort Space Type"] = sm2.group(2)
                        target["Sort Space Used"] = _kb(sm2.group(3))
                    else:
                        target["Sort Method"] = v
                elif k.startswith("Worker ") or k in ("Output",):
                    target.setdefault("Extra", []).append(stripped)
                else:
                    _apply_attr(target, k, v, stripped)
            continue
        if stack:
            stack[-1][1].setdefault("Extra", []).append(stripped)

    if root is None:
        raise ValueError("could not find any plan node line - is this an EXPLAIN output (with COSTS on)?")
    if jit:
        meta["JIT"] = jit
    if triggers:
        meta["Triggers"] = triggers
    out = {"Plan": root}
    out.update(meta)
    return out
