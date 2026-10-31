"""Plan model + loader for EXPLAIN output (FORMAT JSON, or text via textplan)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

# Node types whose rescans are expensive when they sit on the inner side of a Nested Loop
NON_INDEX_INNER_TYPES = {
    "Seq Scan", "CTE Scan", "Materialize", "Subquery Scan", "Function Scan",
    "Values Scan", "Sort", "Aggregate", "Hash Join", "Merge Join", "Nested Loop",
    "WorkTable Scan", "Append", "Result", "Bitmap Heap Scan", "Gather",
}
JOIN_TYPES = {"Nested Loop", "Hash Join", "Merge Join"}
SCAN_TYPES = {
    "Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan",
    "CTE Scan", "Subquery Scan", "Function Scan", "Values Scan", "Tid Scan",
    "Tid Range Scan", "Foreign Scan", "WorkTable Scan", "Sample Scan",
}


@dataclass
class Node:
    raw: Dict[str, Any]
    children: List["Node"] = field(default_factory=list)
    parent: Optional["Node"] = None
    id: int = 0
    depth: int = 0

    # -- basic attributes -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def node_type(self) -> str:
        return self.raw.get("Node Type", "?")

    @property
    def relation(self) -> Optional[str]:
        return self.raw.get("Relation Name")

    @property
    def alias(self) -> Optional[str]:
        return self.raw.get("Alias")

    @property
    def cte_name(self) -> Optional[str]:
        return self.raw.get("CTE Name")

    @property
    def index_name(self) -> Optional[str]:
        return self.raw.get("Index Name")

    @property
    def parent_rel(self) -> str:
        return self.raw.get("Parent Relationship", "")

    @property
    def subplan_name(self) -> Optional[str]:
        return self.raw.get("Subplan Name")

    @property
    def label(self) -> str:
        """Human label equal to what text EXPLAIN prints (independent of input format)."""
        nt = self.node_type
        if nt == "Aggregate":
            base = {"Plain": "Aggregate", "Sorted": "GroupAggregate", "Hashed": "HashAggregate",
                    "Mixed": "MixedAggregate"}.get(self.raw.get("Strategy", "Plain"), "Aggregate")
            pm = self.raw.get("Partial Mode", "Simple")
            return base if pm == "Simple" else f"{pm} {base}"
        if nt == "SetOp":
            return ("HashSetOp" if self.raw.get("Strategy") == "Hashed" else "SetOp")
        if nt in JOIN_TYPES:
            jt = self.raw.get("Join Type", "Inner")
            if nt == "Nested Loop":
                return "Nested Loop" if jt == "Inner" else f"Nested Loop {jt} Join"
            base = nt.replace(" Join", "")
            return f"{base} Join" if jt == "Inner" else f"{base} {jt} Join"
        if nt == "ModifyTable":
            return self.raw.get("Operation", "Modify")
        if self.raw.get("Parallel Aware"):
            return f"Parallel {nt}"
        return nt

    @property
    def target(self) -> str:
        """Relation / CTE / function this node reads, for matching across versions."""
        if self.cte_name:
            return f"cte:{self.cte_name}"
        if self.relation:
            return self.relation
        if self.raw.get("Function Name"):
            return f"fn:{self.raw['Function Name']}"
        if self.node_type == "Subquery Scan" and self.alias:
            return f"subq:{self.alias}"
        return ""

    def describe(self) -> str:
        parts = [self.label]
        if self.index_name and "Index" in self.node_type:
            parts.append(f"using {self.index_name}")
        if self.relation:
            parts.append(f"on {self.relation}" + (f" {self.alias}" if self.alias and self.alias != self.relation else ""))
        elif self.cte_name:
            parts.append(f"on {self.cte_name}" + (f" {self.alias}" if self.alias and self.alias != self.cte_name else ""))
        elif self.node_type in ("Subquery Scan", "Function Scan") and self.alias:
            parts.append(f"on {self.alias}")
        return " ".join(parts)

    # -- estimates / actuals ------------------------------------------------
    @property
    def analyzed(self) -> bool:
        return "Actual Loops" in self.raw

    @property
    def never_executed(self) -> bool:
        return self.analyzed and (self.raw.get("Actual Loops") or 0) == 0

    @property
    def plan_rows(self) -> float:
        return float(self.raw.get("Plan Rows", 0) or 0)

    @property
    def loops(self) -> float:
        return float(self.raw.get("Actual Loops", 1) or 0)

    @property
    def actual_rows(self) -> Optional[float]:
        """Rows per loop (as printed by EXPLAIN)."""
        if not self.analyzed:
            return None
        return float(self.raw.get("Actual Rows", 0) or 0)

    @property
    def actual_rows_total(self) -> Optional[float]:
        if not self.analyzed:
            return None
        return (self.actual_rows or 0) * max(self.loops, 0)

    @property
    def est_rows_total(self) -> float:
        loops = self.loops if self.analyzed and self.loops > 0 else 1
        return self.plan_rows * loops

    @property
    def qerror(self) -> Optional[float]:
        """Signed q-error: >1 => underestimate (actual > est), <-1 => overestimate. None if n/a."""
        if not self.analyzed or self.never_executed:
            return None
        est = max(self.plan_rows, 1.0)
        act = max(self.actual_rows or 0.0, 1.0)
        if act >= est:
            return act / est
        return -(est / act)

    @property
    def qerror_abs(self) -> float:
        q = self.qerror
        return abs(q) if q is not None else 1.0

    @property
    def total_time_incl(self) -> Optional[float]:
        """Inclusive time (ms) summed over loops (approximation for parallel nodes)."""
        if not self.analyzed:
            return None
        t = float(self.raw.get("Actual Total Time", 0) or 0)
        loops = self.loops
        workers = self.raw.get("Workers Launched") or 0
        if self.raw.get("Parallel Aware") or (self.parent_in_parallel() and loops > 1):
            # per-loop time of parallel workers runs concurrently: use wall-clock-ish value
            return t
        return t * max(loops, 1) if workers == 0 else t

    def parent_in_parallel(self) -> bool:
        p = self.parent
        while p is not None:
            if p.node_type in ("Gather", "Gather Merge"):
                return True
            p = p.parent
        return False

    exclusive_time: Optional[float] = None  # filled by Plan

    # -- convenience ----------------------------------------------------------
    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def inner_child(self) -> Optional["Node"]:
        for c in self.children:
            if c.parent_rel == "Inner":
                return c
        plain = [c for c in self.children if c.parent_rel not in ("InitPlan", "SubPlan")]
        return plain[1] if len(plain) > 1 else None

    def outer_child(self) -> Optional["Node"]:
        for c in self.children:
            if c.parent_rel == "Outer":
                return c
        plain = [c for c in self.children if c.parent_rel not in ("InitPlan", "SubPlan")]
        return plain[0] if plain else None

    def filters_count(self) -> int:
        n = 0
        for k in ("Filter", "Index Cond", "Recheck Cond", "Join Filter", "Hash Cond", "Merge Cond"):
            v = self.raw.get(k)
            if v:
                n += 1 + len(re.findall(r"\bAND\b", v))
        return n


@dataclass
class Plan:
    root: Node
    meta: Dict[str, Any]
    source: str = ""
    fmt: str = "json"
    nodes: List[Node] = field(default_factory=list)

    @property
    def analyzed(self) -> bool:
        return self.root.analyzed

    @property
    def execution_time(self) -> Optional[float]:
        v = self.meta.get("Execution Time")
        return float(v) if v is not None else None

    @property
    def planning_time(self) -> Optional[float]:
        v = self.meta.get("Planning Time")
        return float(v) if v is not None else None

    @property
    def jit(self) -> Dict[str, Any]:
        return self.meta.get("JIT") or {}

    @property
    def jit_total_ms(self) -> float:
        timing = self.jit.get("Timing") or {}
        tot = timing.get("Total")
        if isinstance(tot, dict):
            return float(tot.get("Total", 0) or 0) if "Total" in tot else 0.0
        try:
            return float(tot or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def settings(self) -> Dict[str, str]:
        return self.meta.get("Settings") or {}

    @property
    def triggers(self) -> List[Dict[str, Any]]:
        return self.meta.get("Triggers") or []

    @property
    def runtime_ms(self) -> Optional[float]:
        if self.execution_time is not None:
            return self.execution_time
        return self.root.total_time_incl

    def node(self, nid: int) -> Node:
        return self.nodes[nid]


# ---------------------------------------------------------------------------
def _build(raw: Dict[str, Any], parent: Optional[Node], depth: int, acc: List[Node]) -> Node:
    node = Node(raw={k: v for k, v in raw.items() if k != "Plans"}, parent=parent, depth=depth)
    node.id = len(acc)
    acc.append(node)
    for child in raw.get("Plans", []) or []:
        node.children.append(_build(child, node, depth + 1, acc))
    return node


def _compute_exclusive(plan: Plan) -> None:
    for n in plan.nodes:
        incl = n.total_time_incl
        if incl is None:
            continue
        child_sum = 0.0
        for c in n.children:
            ct = c.total_time_incl
            # InitPlan CTEs are executed lazily by their CTE Scan; still subtract when they hang here.
            if ct is not None:
                child_sum += ct
        n.exclusive_time = max(incl - child_sum, 0.0)


def plan_from_obj(obj: Any, source: str = "", fmt: str = "json") -> Plan:
    if isinstance(obj, list):
        if not obj:
            raise ValueError("empty plan list")
        obj = obj[0]
    if not isinstance(obj, dict):
        raise ValueError("unexpected EXPLAIN JSON structure")
    if "Plan" not in obj:
        if "Node Type" in obj:
            obj = {"Plan": obj}
        else:
            raise ValueError("JSON has no 'Plan' key - was it produced with EXPLAIN (FORMAT JSON)?")
    nodes: List[Node] = []
    root = _build(obj["Plan"], None, 0, nodes)
    meta = {k: v for k, v in obj.items() if k != "Plan"}
    plan = Plan(root=root, meta=meta, source=source, fmt=fmt, nodes=nodes)
    _compute_exclusive(plan)
    return plan


def _strip_psql_decoration(text: str) -> str:
    """Remove psql table decoration ('QUERY PLAN' header, '+' continuation marks, row count)."""
    lines = text.splitlines()
    out = []
    for ln in lines:
        s = ln.rstrip()
        if re.match(r"^\s*(SET|BEGIN|ROLLBACK|COMMIT|START TRANSACTION|RESET|DISCARD ALL)\s*;?\s*$", s) or                 re.match(r"^Time:\s*[\d.]+\s*ms", s.strip()):
            continue  # psql command tags / 	iming output around the EXPLAIN
        if re.match(r"^\s*QUERY PLAN\s*$", s) or re.match(r"^\s*-{5,}\s*$", s) or re.match(r"^\(\d+ rows?\)$", s.strip()):
            continue
        if s.endswith("+"):
            s = s[:-1].rstrip()
        out.append(s)
    return "\n".join(out)


def load_plan(path: str) -> Plan:
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    return parse_plan_text(text, source=path)


def parse_plan_text(text: str, source: str = "") -> Plan:
    stripped = text.strip()
    # auto_explain log lines: "... plan:\n{ json }"  -> try to locate JSON
    candidate = _strip_psql_decoration(stripped)
    for blob in (stripped, candidate):
        start = min([i for i in (blob.find("["), blob.find("{")) if i >= 0], default=-1)
        if start >= 0:
            try:
                obj = json.loads(blob[start:])
                return plan_from_obj(obj, source=source, fmt="json")
            except json.JSONDecodeError:
                end = max(blob.rfind("]"), blob.rfind("}"))
                if end > start:
                    try:
                        obj = json.loads(blob[start:end + 1])
                        return plan_from_obj(obj, source=source, fmt="json")
                    except json.JSONDecodeError:
                        pass
    from .textplan import parse_text_plan  # local import to avoid cycle
    obj = parse_text_plan(candidate)
    return plan_from_obj(obj, source=source, fmt="text")
