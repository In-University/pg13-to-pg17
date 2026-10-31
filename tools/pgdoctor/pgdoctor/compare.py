"""Old-vs-new plan comparison: node matching, divergence, version-specific patterns."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from .findings import Finding, fmt_ms, fmt_num
from .plan import JOIN_TYPES, SCAN_TYPES, Node, Plan
from .rules_plan import subplan_info

# node label fragment -> GUC that disables it (diagnostic switch)
GUC_FOR_NODE = [
    ("Memoize", "enable_memoize"),
    ("Incremental Sort", "enable_incremental_sort"),
    ("Nested Loop", "enable_nestloop"),
    ("Hash Join", "enable_hashjoin"),
    ("Hash Right Anti Join", "enable_hashjoin"),
    ("Merge Join", "enable_mergejoin"),
    ("Materialize", "enable_material"),
    ("Gather Merge", "enable_gathermerge"),
    ("Parallel Append", "enable_parallel_append"),
    ("Parallel Hash", "enable_parallel_hash"),
    ("HashAggregate", "enable_hashagg"),
    ("Bitmap Heap Scan", "enable_bitmapscan"),
    ("Index Only Scan", "enable_indexonlyscan"),
    ("Index Scan", "enable_indexscan"),
    ("Seq Scan", "enable_seqscan"),
]
VERSION_NEW_NODES = {
    "Memoize": "PG14+ (enable_memoize)",
    "Right Anti Join": "PG16+ (Right Anti Join)",
    "Right Semi Join": "PG18+ (Right Semi Join)",
    "Parallel Hash Right Join": "PG16+ (Parallel Hash Right/Full Join)",
    "Parallel Hash Full Join": "PG16+ (Parallel Hash Right/Full Join)",
    "Incremental Sort": "PG13+ (enable_incremental_sort; mở rộng cho DISTINCT/window ở PG16)",
}


def _ctx(node: Node) -> str:
    p = node
    while p is not None:
        if p.subplan_name:
            return p.subplan_name
        p = p.parent
    return "main"


def rels(node: Node) -> FrozenSet[str]:
    s = set()
    for n in node.walk():
        if n.node_type in SCAN_TYPES and n.target and n.node_type != "Bitmap Index Scan":
            s.add(n.target + (f"/{n.alias}" if n.alias else ""))
        if n.subplan_name and n is not node:
            # do not descend into other InitPlans/SubPlans – handled by walk anyway; keep simple
            pass
    return frozenset(s)


def node_key(n: Node) -> Tuple:
    ctx = _ctx(n)
    if n.node_type in JOIN_TYPES:
        return ("JOIN", ctx, rels(n))
    if n.node_type in SCAN_TYPES and n.node_type != "Bitmap Index Scan":
        return ("SCAN", ctx, n.target, n.alias)
    if n.node_type == "Aggregate":
        return ("AGG", ctx, rels(n), n.get("Partial Mode", "Simple"))
    return (n.node_type, ctx, rels(n))


@dataclass
class CompareResult:
    old_to_new: Dict[int, int] = field(default_factory=dict)
    new_to_old: Dict[int, int] = field(default_factory=dict)
    divergence_old: Optional[int] = None
    divergence_new: Optional[int] = None
    findings: List[Finding] = field(default_factory=list)


def match_nodes(old: Plan, new: Plan) -> Tuple[Dict[int, int], Dict[int, int]]:
    idx_old: Dict[Tuple, List[Node]] = {}
    for n in old.nodes:
        idx_old.setdefault(node_key(n), []).append(n)
    o2n, n2o = {}, {}
    for n in new.nodes:
        bucket = idx_old.get(node_key(n))
        if not bucket:
            continue
        # prefer same node type, then same depth
        bucket.sort(key=lambda o: (o.node_type != n.node_type, abs(o.depth - n.depth)))
        o = bucket.pop(0)
        o2n[o.id], n2o[n.id] = n.id, o.id
    return o2n, n2o


def _first_divergence(a: Node, b: Node) -> Optional[Tuple[Node, Node]]:
    if (a.label, a.target) != (b.label, b.target):
        return a, b
    ac = [c for c in a.children]
    bc = [c for c in b.children]
    for x, y in zip(ac, bc):
        d = _first_divergence(x, y)
        if d:
            return d
    if len(ac) != len(bc):
        return a, b
    return None


def compare_plans(old: Plan, new: Plan, qname: str) -> CompareResult:
    res = CompareResult()
    res.old_to_new, res.new_to_old = match_nodes(old, new)
    F = res.findings
    both_analyzed = old.analyzed and new.analyzed

    def add(rule, sev, msg, new_nodes=(), old_nodes=(), ev=None, score=0.0, data=None):
        F.append(Finding(rule=rule, severity=sev, scope="compare", subject=qname, message=msg,
                         evidence=list(ev or []), side="both", node_ids=[n.id for n in new_nodes],
                         old_node_ids=[n.id for n in old_nodes], data=data or {}, score=score))

    # ----------------------------------------------------------- REGRESSION
    if old.execution_time and new.execution_time:
        ratio = new.execution_time / max(old.execution_time, 0.001)
        sev = None
        if ratio >= 1.5:
            sev = "low"
        if ratio >= 3:
            sev = "medium"
        if ratio >= 10:
            sev = "high"
        if ratio >= 30:
            sev = "critical"
        if sev:
            add("CMP-REGRESSION", sev,
                f"Bản mới chậm hơn {ratio:.1f}× ({fmt_ms(old.execution_time)} → {fmt_ms(new.execution_time)})",
                score=ratio, data={"ratio": ratio})
        elif ratio <= 0.67:
            add("CMP-REGRESSION", "info",
                f"Bản mới nhanh hơn {1/ratio:.1f}× ({fmt_ms(old.execution_time)} → {fmt_ms(new.execution_time)})",
                data={"ratio": ratio})

    # ------------------------------------------------------------ DIVERGENCE
    d = _first_divergence(old.root, new.root)
    if d:
        a, b = d
        res.divergence_old, res.divergence_new = a.id, b.id
        old_joins = {rels(n) for n in old.nodes if n.node_type in JOIN_TYPES}
        new_joins = {rels(n) for n in new.nodes if n.node_type in JOIN_TYPES}
        only_old = [sorted(s) for s in old_joins - new_joins]
        only_new = [sorted(s) for s in new_joins - old_joins]
        ev = [f"Bản cũ:  {a.describe()} (ước lượng {fmt_num(a.plan_rows)} dòng)",
              f"Bản mới: {b.describe()} (ước lượng {fmt_num(b.plan_rows)} dòng)"]
        if only_old or only_new:
            ev.append("Thứ tự join khác nhau – tập bảng được join trung gian chỉ có ở bản cũ: "
                      + "; ".join("{" + ", ".join(x) + "}" for x in only_old[:4]))
            ev.append("… chỉ có ở bản mới: " + "; ".join("{" + ", ".join(x) + "}" for x in only_new[:4]))
        add("CMP-DIVERGE", "medium", f"Plan phân kỳ tại: '{a.describe()}' → '{b.describe()}'",
            [b], [a], ev, score=1)
    # ------------------------------------------------------ JOIN / SCAN method
    for nid, oid in res.new_to_old.items():
        n, o = new.node(nid), old.node(oid)
        if n.node_type in JOIN_TYPES and o.node_type in JOIN_TYPES and n.label != o.label:
            sev, ev = "medium", []
            share_new = (n.total_time_incl or 0) / new.runtime_ms if (new.analyzed and new.runtime_ms) else 0
            if both_analyzed:
                tn, to = n.total_time_incl or 0, o.total_time_incl or 0
                ev.append(f"Thời gian (gộp): {fmt_ms(to)} → {fmt_ms(tn)}")
                if tn >= 2 * max(to, 1) and share_new >= 0.2:
                    sev = "high"
                if tn >= 10 * max(to, 1) and share_new >= 0.5:
                    sev = "critical"
            ev.append(f"Ước lượng đầu ra: {fmt_num(o.plan_rows)} → {fmt_num(n.plan_rows)}")
            oc_o, oc_n = o.outer_child(), n.outer_child()
            if oc_o and oc_n:
                ev.append(f"Ước lượng outer: {fmt_num(oc_o.plan_rows)} ({oc_o.describe()}) → {fmt_num(oc_n.plan_rows)} ({oc_n.describe()})")
            add("CMP-JOIN-METHOD", sev,
                f"{o.label} → {n.label} cho join {{{', '.join(sorted(rels(n)))}}}",
                [n], [o], ev, score=share_new * 10)
        elif n.node_type in SCAN_TYPES and n.label != o.label and o.node_type in SCAN_TYPES:
            ev = [f"Ước lượng: {fmt_num(o.plan_rows)} → {fmt_num(n.plan_rows)}"]
            sev = "low"
            if both_analyzed:
                tn, to = n.total_time_incl or 0, o.total_time_incl or 0
                ev.append(f"Thời gian (gộp): {fmt_ms(to)} → {fmt_ms(tn)}")
                if tn >= 3 * max(to, 1) and new.runtime_ms and tn / new.runtime_ms >= 0.2:
                    sev = "medium"
            add("CMP-SCAN-METHOD", sev, f"{n.target}: {o.describe()} → {n.describe()}", [n], [o], ev)

    # ---------------------------------------------------------- EST SHIFT
    shifts = []
    skip_types = {"Hash", "Bitmap Index Scan", "ModifyTable", "Sort", "Incremental Sort", "Materialize", "Memoize",
                  "Result", "Limit", "Gather", "Gather Merge", "Unique", "LockRows", "ProjectSet"}
    for nid, oid in res.new_to_old.items():
        n, o = new.node(nid), old.node(oid)
        if n.node_type in skip_types or o.node_type in skip_types:
            continue
        if n.node_type in SCAN_TYPES and (n.node_type != o.node_type or _parameterized(n) != _parameterized(o)
                                          or _parameterized(n)):
            continue  # per-loop estimates of parameterized scans are not comparable
        eo, en = max(o.plan_rows, 1), max(n.plan_rows, 1)
        ratio = max(eo, en) / min(eo, en)
        if ratio < 10 or max(eo, en) < 100:
            continue
        shifts.append((ratio, n, o))
    shift_ids = {n.id: (en_ > eo_) for _, n, o in shifts for en_, eo_ in [(n.plan_rows, o.plan_rows)]}

    def _has_shifted_desc(n: Node, up: bool) -> bool:
        if n.node_type == "CTE Scan" and n.cte_name:
            for x in new.nodes:
                if x.subplan_name == f"CTE {n.cte_name}" and (shift_ids.get(x.id) is up or _has_shifted_desc(x, up)):
                    return True
        for c in n.children:
            if shift_ids.get(c.id) is up or _has_shifted_desc(c, up):
                return True
        return False
    origins = [(r, n, o) for r, n, o in shifts if not _has_shifted_desc(n, shift_ids[n.id])]
    shifts = origins
    shifts.sort(key=lambda t: -t[0])
    for ratio, n, o in shifts[:6]:
        has_cte = any(x.node_type == "CTE Scan" for x in n.walk())
        ev = [f"Ước lượng bản cũ: {fmt_num(o.plan_rows)} dòng/loop; bản mới: {fmt_num(n.plan_rows)} dòng/loop"]
        sev = "medium"
        closer = None
        act = n.actual_rows if n.analyzed else (o.actual_rows if o.analyzed else None)
        if act is not None:
            a1 = max(act, 1)
            err_o, err_n = max(a1, max(o.plan_rows, 1)) / min(a1, max(o.plan_rows, 1)), max(a1, max(n.plan_rows, 1)) / min(a1, max(n.plan_rows, 1))
            closer = "old" if err_o < err_n else "new"
            ev.append(f"Thực tế: {fmt_num(act)} dòng/loop → bản {'CŨ' if closer == 'old' else 'MỚI'} gần đúng hơn "
                      f"(sai số cũ {err_o:.0f}×, mới {err_n:.0f}×)")
            if closer == "old" and err_n >= 100:
                sev = "high"
            if closer == "new":
                sev = "low"
        if o.plan_rows == 200 and o.node_type in ("Aggregate", "Unique"):
            ev.append("Bản cũ ước lượng đúng 200 = DEFAULT_NUM_DISTINCT (không có stats cho cột).")
        if has_cte:
            ev.append("Node này đọc từ CTE: PG17 lấy stats cột CTE từ bảng gốc (f7816aec2) – stats của toàn bảng, "
                      "không tính điều kiện lọc bên trong CTE.")
        what = n.describe() if n.node_type not in JOIN_TYPES else f"{n.label} {{{', '.join(sorted(rels(n)))}}}"
        add("CMP-EST-SHIFT", sev, f"{what}: ước lượng đổi {ratio:.0f}× giữa hai bản (cũ {fmt_num(o.plan_rows)} → mới {fmt_num(n.plan_rows)})",
            [n], [o], ev, score=math.log10(ratio), data={"closer": closer, "cte": has_cte})

    # ------------------------------------------------------ SUBPLAN HASH LOST
    old_sp = [(n, subplan_info(old, n)) for n in old.nodes if n.parent_rel == "SubPlan" and n.subplan_name]
    new_sp = [(n, subplan_info(new, n)) for n in new.nodes if n.parent_rel == "SubPlan" and n.subplan_name]
    for nn, ni in new_sp:
        if ni["hashed"] or not ni["any_type"]:
            continue
        cand = [(on, oi) for on, oi in old_sp if oi["hashed"] and (on.target == nn.target or on.subplan_name == nn.subplan_name)]
        if not cand:
            continue
        on, oi = cand[0]
        ev = [f"Bản cũ: {on.subplan_name} hashed – ước lượng {fmt_num(on.plan_rows)} dòng (~{_fmt_bytes(oi['hash_bytes'])})"
              + (f", thực tế {fmt_num(on.actual_rows_total)}" if on.analyzed else ""),
              f"Bản mới: {nn.subplan_name} KHÔNG hashed – ước lượng {fmt_num(nn.plan_rows)} dòng (~{_fmt_bytes(ni['hash_bytes'])})",
              f"Bản cũ  → {oi['expr'][:180]}", f"Bản mới → {ni['expr'][:180]}"]
        if ni["limit_bytes"]:
            ev.append(f"Ngưỡng hash bản mới ≈ {_fmt_bytes(ni['limit_bytes'])} (work_mem × hash_mem_multiplier từ EXPLAIN SETTINGS)")
        else:
            ev.append("Ngưỡng hash = work_mem × hash_mem_multiplier; EXPLAIN SETTINGS không liệt kê ⇒ đang ở mặc định (4MB × 2.0 = 8MB)")
        add("CMP-SUBPLAN-HASH-LOST", "critical",
            f"{nn.subplan_name}: bản cũ dùng hashed SubPlan, bản mới duyệt danh sách cho từng dòng "
            f"(ước lượng {fmt_num(on.plan_rows)} → {fmt_num(nn.plan_rows)} dòng)",
            [nn, nn.parent] if nn.parent else [nn], [on], ev, score=100)

    # ------------------------------------------------------ CTE PATHKEYS (PG17)
    for n in new.nodes:
        if n.node_type == "Merge Join":
            cte_kids = [c for c in n.children if c.node_type == "CTE Scan"]
            if cte_kids:
                oid = res.new_to_old.get(n.id)
                if oid is None or old.node(oid).node_type != "Merge Join":
                    add("CMP-CTE-PATHKEYS", "low",
                        f"Bản mới dùng Merge Join đọc trực tiếp '{cte_kids[0].describe()}' không cần Sort (PG17 truyền pathkeys của CTE)",
                        [n], [old.node(oid)] if oid is not None else [])

    # ------------------------------------------------------------- NEW NODES
    old_labels = {x.label for x in old.nodes}
    new_only = []
    for x in new.nodes:
        for frag, note in VERSION_NEW_NODES.items():
            if frag in x.label and not any(frag in l for l in old_labels):
                new_only.append((x, note))
    seen = set()
    for x, note in new_only:
        if x.label in seen:
            continue
        seen.add(x.label)
        guc = next((g for frag, g in GUC_FOR_NODE if frag in x.label), None)
        sev = "medium" if x.node_type == "Memoize" else "low"
        add("CMP-NEW-NODE", sev, f"'{x.label}' chỉ có ở bản mới – {note}", [x], [],
            [f"Công tắc chẩn đoán: SET LOCAL {guc} = off;" if guc else "Không có GUC riêng."])

    # --------------------------------------------------------------- NOSTATS
    ev = []
    hits = []
    for nid, oid in res.new_to_old.items():
        n, o = new.node(nid), old.node(oid)
        if n.node_type == "Seq Scan" and o.node_type == "Seq Scan" and not n.get("Filter") and not o.get("Filter"):
            if o.plan_rows >= 1000 and abs(n.plan_rows - o.plan_rows) / o.plan_rows >= 0.05:
                ev.append(f"{n.relation}: ước lượng scan toàn bảng {fmt_num(o.plan_rows)} (cũ) vs {fmt_num(n.plan_rows)} (mới)")
                hits.append(n)
        if n.node_type in ("Aggregate",) and n.plan_rows == 200 and o.plan_rows != 200:
            ev.append(f"{n.describe()}: bản mới ước lượng 200 nhóm (mặc định) trong khi bản cũ {fmt_num(o.plan_rows)}")
            hits.append(n)
    collapse_new = [x for x in new.nodes if x.plan_rows <= 1 and x.filters_count() >= 3]
    collapse_old_t = {x.target for x in old.nodes if x.plan_rows <= 1 and x.filters_count() >= 3}
    cte_corr = []
    for x in collapse_new:
        if x.target in collapse_old_t:
            continue
        o = old.node(res.new_to_old[x.id]) if x.id in res.new_to_old else None
        line = f"{x.describe()}: bản mới ước lượng {fmt_num(x.plan_rows)} dòng" + (f", bản cũ {fmt_num(o.plan_rows)}" if o else "")
        if new.analyzed and x.actual_rows is not None:
            line += f", thực tế {fmt_num(x.actual_rows)}"
        if x.node_type == "CTE Scan":
            cte_corr.append((x, o, line))      # filter on CTE columns: PG17 uses base-table stats (not a missing-stats sign)
        else:
            ev.append(line)
            hits.append(x)
    if cte_corr:
        sev = "high"
        if new.analyzed and any((x.actual_rows or 0) >= 1000 for x, _, _ in cte_corr):
            sev = "critical"
        add("CMP-CTE-CORRELATION", sev,
            f"Ước lượng của {len(cte_corr)} CTE Scan có điều kiện lọc sụp còn ~1 dòng ở bản mới (stats bảng gốc + điều kiện tương quan)",
            [x for x, _, _ in cte_corr], [o for _, o, _ in cte_corr if o is not None],
            [l for _, _, l in cte_corr] + [f"Filter: {cte_corr[0][0].get('Filter')}",
                                           "Không phải do thiếu ANALYZE: PG17 gắn stats của bảng gốc cho cột CTE (f7816aec2)."],
            score=50)
    if hits:
        add("CMP-NOSTATS", "high" if len(hits) >= 2 else "medium",
            "Bản mới có dấu hiệu thiếu/khác thống kê (điển hình sau pg_upgrade chưa ANALYZE)", hits, [], ev, score=len(hits))

    # -------------------------------------------------------------- SETTINGS
    so, sn = old.settings, new.settings
    if so or sn:
        diffs = [(k, so.get(k, "(mặc định)"), sn.get(k, "(mặc định)")) for k in sorted(set(so) | set(sn)) if so.get(k) != sn.get(k)]
        if diffs:
            add("CMP-SETTINGS", "medium" if any(k.startswith("enable_") or k in ("work_mem", "random_page_cost") for k, _, _ in diffs) else "low",
                f"{len(diffs)} tham số khác nhau trong EXPLAIN (SETTINGS)", [], [],
                [f"{k}: {a} → {b}" for k, a, b in diffs])

    # ----------------------------------------------------------------- SPILL
    if both_analyzed:
        for n in new.nodes:
            spill_n = _spills(n)
            if not spill_n:
                continue
            oid = res.new_to_old.get(n.id)
            if oid is not None and not _spills(old.node(oid)):
                add("CMP-SPILL", "medium", f"{n.describe()} tràn đĩa ở bản mới nhưng không ở bản cũ", [n], [old.node(oid)], [spill_n])

    # ------------------------------------------------------------------- JIT
    jo, jn = old.jit_total_ms, new.jit_total_ms
    if jn >= 500 and jn >= 2 * max(jo, 1):
        add("CMP-JIT", "medium", f"JIT: {fmt_ms(jo)} → {fmt_ms(jn)}", [], [],
            [f"Bản cũ options: {old.jit.get('Options')}", f"Bản mới options: {new.jit.get('Options')}"])

    # ------------------------------------------------------------ GROUP KEY
    for nid, oid in res.new_to_old.items():
        n, o = new.node(nid), old.node(oid)
        for key in ("Group Key", "Sort Key"):
            kn, ko = n.get(key), o.get(key)
            if isinstance(kn, list) and isinstance(ko, list) and len(kn) > 1 and kn != ko and sorted(kn) == sorted(ko):
                add("CMP-GROUPKEY-ORDER", "low", f"{n.describe()}: {key} đổi thứ tự", [n], [o],
                    [f"Cũ: {', '.join(ko)}", f"Mới: {', '.join(kn)}"])
    return res


def _fmt_bytes(b) -> str:
    if b is None:
        return "?"
    return f"{b / 1024 ** 2:.1f}MB" if b >= 1024 ** 2 else f"{b / 1024:.0f}kB"


def _parameterized(n: Node) -> bool:
    """Scan executed once per outer row (inner side of a Nested Loop)."""
    c, p = n, n.parent
    while p is not None:
        if p.node_type == "Nested Loop" and c is p.inner_child():
            return True
        if p.node_type not in ("Memoize", "Materialize", "Bitmap Heap Scan", "Result", "Append"):
            return False
        c, p = p, p.parent
    return False


def _spills(n: Node) -> Optional[str]:
    if n.node_type == "Hash" and (n.get("Hash Batches") or 1) > 1:
        return f"Hash Batches {n.get('Hash Batches')}"
    if n.node_type in ("Sort", "Incremental Sort") and n.get("Sort Space Type") == "Disk":
        return f"Sort {n.get('Sort Method')} Disk {fmt_num(n.get('Sort Space Used'))} kB"
    if n.node_type == "Aggregate" and ((n.get("Disk Usage") or 0) > 0):
        return f"HashAgg Disk {fmt_num(n.get('Disk Usage'))} kB"
    return None
