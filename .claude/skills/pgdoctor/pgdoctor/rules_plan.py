"""Single-plan rules (applied to each version independently)."""
from __future__ import annotations

import math
import re
from typing import List, Optional

from .findings import Finding, bump, fmt_ms, fmt_num, lower
from .plan import NON_INDEX_INNER_TYPES, Node, Plan

MISEST_MIN_ROWS = 1000      # ignore misestimates on tiny row counts
MAX_MISEST_FINDINGS = 6


def _share(plan: Plan, ms: Optional[float]) -> float:
    rt = plan.runtime_ms
    if not rt or ms is None:
        return 0.0
    return max(0.0, min(1.0, ms / rt))


def _pct(x: float) -> str:
    return f"{x*100:.0f}%"


def _cte_root(plan: Plan, cte_name: str) -> Optional[Node]:
    for n in plan.nodes:
        if n.subplan_name == f"CTE {cte_name}":
            return n
    return None


def _ctx(node: Node) -> str:
    p = node
    while p is not None:
        if p.subplan_name:
            return p.subplan_name
        p = p.parent
    return "main"


def analyze_plan(plan: Plan, side: str, qname: str) -> List[Finding]:
    out: List[Finding] = []
    A = plan.analyzed

    def add(rule, sev, msg, nodes=(), ev=None, score=0.0, data=None):
        out.append(Finding(rule=rule, severity=sev, scope="plan", subject=qname, message=msg,
                           evidence=list(ev or []), side=side, node_ids=[n.id for n in nodes],
                           data=data or {}, score=score))

    nl_rescan_inner_ids = set()

    # ---------------------------------------------------------------- MISEST
    if A:
        cands = []
        for n in plan.nodes:
            if n.never_executed or n.qerror is None or n.node_type == "ModifyTable":
                continue  # ModifyTable: Plan Rows = rows to modify, Actual Rows = rows returned (0 without RETURNING)
            if n.qerror_abs < 10:
                continue
            if max(n.actual_rows_total or 0, n.est_rows_total) < MISEST_MIN_ROWS and n.loops <= 1:
                continue
            if _merge_inner(n):
                continue  # Merge Join inner side re-reads rows (mark/restore): actual rows are inflated
            if n.qerror < 0 and _may_stop_early(n):
                continue  # Merge Join / Limit / semi-join stop reading early: actual < est is expected
            kids = [c for c in n.children if not c.never_executed and c.qerror is not None]
            if any(c.qerror_abs >= 10 and (c.qerror > 0) == (n.qerror > 0) for c in kids):
                continue  # not the origin
            if n.node_type == "CTE Scan" and n.cte_name:
                root = _cte_root(plan, n.cte_name)
                if root is not None and root.qerror is not None and root.qerror_abs >= 10:
                    continue
            cands.append(n)
        cands.sort(key=lambda n: -n.qerror_abs)
        for n in cands[:MAX_MISEST_FINDINGS]:
            q = n.qerror
            under = q > 0
            sev = "medium"
            if abs(q) >= 100:
                sev = "high"
            if abs(q) >= 1000:
                sev = "critical"
            if not under:
                sev = lower(sev)
            ev = [f"Ước lượng: {fmt_num(n.plan_rows)} dòng/loop; thực tế: {fmt_num(n.actual_rows)} dòng/loop × {fmt_num(n.loops)} loop",
                  f"Ngữ cảnh: {_ctx(n)}"]
            for k in ("Filter", "Index Cond", "Hash Cond", "Join Filter", "Group Key"):
                if n.get(k):
                    v = n.get(k)
                    ev.append(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
            add("PLAN-MISEST", sev,
                f"{n.describe()}: ước lượng {fmt_num(n.plan_rows)} dòng nhưng thực tế {fmt_num(n.actual_rows)} "
                f"({'thiếu' if under else 'thừa'} {abs(q):.0f}×)",
                [n], ev, score=math.log10(abs(q)), data={"qerror": q})
        if len(cands) > MAX_MISEST_FINDINGS:
            add("PLAN-MISEST", "info", f"Còn {len(cands) - MAX_MISEST_FINDINGS} node khác có sai lệch ≥10× (xem cây plan).",
                cands[MAX_MISEST_FINDINGS:], score=0)

    # ---------------------------------------------------------- NESTED LOOP
    for n in plan.nodes:
        if n.node_type != "Nested Loop":
            continue
        inner, outer = n.inner_child(), n.outer_child()
        if inner is None or outer is None:
            continue
        inner_eff = inner
        non_index = inner_eff.node_type in NON_INDEX_INNER_TYPES and not (
            inner_eff.node_type == "Bitmap Heap Scan" and any("Index" in c.node_type for c in inner_eff.children)
            and _is_parameterized(inner_eff))
        if inner_eff.node_type == "Memoize":
            non_index = False
        if A and not n.never_executed:
            loops = inner.loops
            removed = float(n.get("Rows Removed by Join Filter", 0) or 0) * max(n.loops, 1)
            inner_ms = inner.total_time_incl
            share = _share(plan, inner_ms)
            if non_index and loops >= 100:
                sev = "medium"
                if loops >= 1000 and share >= 0.2:
                    sev = "high"
                if loops >= 1000 and share >= 0.5:
                    sev = "critical"
                ev = [f"Outer: {outer.describe()} – ước lượng {fmt_num(outer.plan_rows)} dòng, thực tế {fmt_num(outer.actual_rows_total)}",
                      f"Inner: {inner.describe()} chạy {fmt_num(loops)} lần, mỗi lần ~{fmt_num(inner.actual_rows)} dòng",
                      f"Thời gian inner (gộp): {fmt_ms(inner_ms)} ({_pct(share)} tổng)"]
                if removed:
                    ev.append(f"Rows Removed by Join Filter: {fmt_num(removed)}")
                add("PLAN-NL-RESCAN", sev,
                    f"Nested Loop chạy lại '{inner.describe()}' {fmt_num(loops)} lần (inner không dùng index)",
                    [n, inner, outer], ev, score=share * 10 + math.log10(max(loops, 1)))
                nl_rescan_inner_ids.add(inner.id)
                continue
            if outer.plan_rows <= 10 and (outer.actual_rows_total or 0) >= 1000:
                sev = "high" if non_index or share >= 0.2 else "medium"
                add("PLAN-NL-BET", sev,
                    f"Nested Loop chọn vì outer ước lượng {fmt_num(outer.plan_rows)} dòng nhưng thực tế {fmt_num(outer.actual_rows_total)}",
                    [n, outer, inner],
                    [f"Outer: {outer.describe()}", f"Inner: {inner.describe()} – {fmt_num(inner.loops)} loop, {fmt_ms(inner.total_time_incl)}"],
                    score=share * 10)
        elif not A:
            if outer.plan_rows <= 2 and non_index:
                # cost of ONE rescan ~ biggest scan inside the inner subtree (not just its output rows)
                scanned = max([x.plan_rows for x in inner.walk()] + [inner.plan_rows])
                sev = "low"
                if scanned >= 10000 or inner.node_type in ("CTE Scan", "Materialize"):
                    sev = "medium"
                if scanned >= 100000:
                    sev = "high"
                add("PLAN-NL-BET", sev,
                    f"Nested Loop đặt cược outer ≈ {fmt_num(outer.plan_rows)} dòng; mỗi lần lặp chạy lại '{inner.describe()}' "
                    f"(quét tới ~{fmt_num(scanned)} dòng) không qua index – nếu outer thực tế lớn sẽ rất chậm",
                    [n, outer, inner],
                    [f"Outer: {outer.describe()} (ước lượng {fmt_num(outer.plan_rows)})",
                     f"Inner: {inner.describe()} – subtree quét tới ~{fmt_num(scanned)} dòng mỗi lần",
                     "Kiểm tra số dòng thật của outer: nếu là hàng nghìn trở lên, thời gian ≈ outer × chi phí inner"],
                    score=math.log10(max(scanned, 1)))

    # ---------------------------------------------------------- COLLAPSE
    for n in plan.nodes:
        if n.plan_rows > 1 or n.filters_count() < 3:
            continue
        if A:
            act = n.actual_rows or 0  # per loop: the estimate is per loop too
            if act >= 100:
                add("PLAN-COLLAPSE", "high" if act >= 10000 else "medium",
                    f"{n.describe()}: {n.filters_count()} điều kiện → ước lượng 1 dòng, thực tế {fmt_num(act)}",
                    [n], [f"Filter: {n.get('Filter') or n.get('Index Cond') or n.get('Recheck Cond')}"],
                    score=math.log10(max(act, 1)))
        else:
            add("PLAN-COLLAPSE", "low",
                f"{n.describe()}: {n.filters_count()} điều kiện → ước lượng 1 dòng (kiểm tra số dòng thật)",
                [n], [f"Filter: {n.get('Filter') or n.get('Index Cond') or n.get('Recheck Cond')}"])

    # ------------------------------------------------ DEFAULT N_DISTINCT
    for n in plan.nodes:
        if n.node_type not in ("Aggregate", "Unique", "SetOp") or n.plan_rows != 200:
            continue
        child = n.outer_child()
        if child is None or child.plan_rows <= 200:
            continue
        has_cte = any(x.node_type == "CTE Scan" for x in n.walk())
        msg = f"{n.describe()} ước lượng đúng 200 nhóm – giá trị mặc định khi không có stats"
        msg += " (đầu vào có CTE Scan: PG ≤16 không có stats cho cột CTE)" if has_cte else ""
        if A and n.actual_rows is not None and n.qerror_abs >= 5:
            msg += f"; thực tế {fmt_num(n.actual_rows_total)} nhóm"
            add("PLAN-DEFAULT-NDISTINCT", "medium", msg, [n], score=1)
        else:
            add("PLAN-DEFAULT-NDISTINCT", "info", msg, [n])

    # --------------------------------------------------------- MEMOIZE
    for n in plan.nodes:
        if n.node_type != "Memoize" or not A:
            continue
        hits, misses = n.get("Cache Hits", 0) or 0, n.get("Cache Misses", 0) or 0
        ev_, ov = n.get("Cache Evictions", 0) or 0, n.get("Cache Overflows", 0) or 0
        tot = hits + misses
        if tot == 0:
            continue
        ratio = hits / tot
        if (ratio < 0.5 and misses >= 1000) or (ev_ > 0 and misses >= 1000) or ov > 0:
            add("PLAN-MEMOIZE", "high" if ov > 0 or (ratio < 0.1 and misses >= 100000) else "medium",
                f"Memoize hit ratio {_pct(ratio)} (hits {fmt_num(hits)}, misses {fmt_num(misses)}, evictions {fmt_num(ev_)}, overflows {fmt_num(ov)})",
                [n], [f"Cache Key: {n.get('Cache Key')}", f"Ước lượng rows/loop: {fmt_num(n.plan_rows)}"], score=1 - ratio)

    # ----------------------------------------------------------- SPILL
    for n in plan.nodes:
        if not A:
            break
        ev = []
        sev = None
        if n.node_type == "Hash" and (n.get("Hash Batches") or 1) > 1:
            b, ob = n.get("Hash Batches"), n.get("Original Hash Batches") or n.get("Hash Batches")
            sev = "high" if b > ob else "medium"
            ev.append(f"Hash Batches {b} (ban đầu {ob}); bộ nhớ đỉnh {fmt_num(n.get('Peak Memory Usage'))} kB")
            if b > ob:
                ev.append("Số batch tăng trong lúc chạy → planner ước lượng thiếu kích thước bảng hash")
        if n.node_type in ("Sort", "Incremental Sort") and n.get("Sort Space Type") == "Disk":
            sev = "medium"
            ev.append(f"Sort Method: {n.get('Sort Method')} – Disk {fmt_num(n.get('Sort Space Used'))} kB")
        if n.node_type == "Aggregate" and ((n.get("Disk Usage") or 0) > 0 or (n.get("HashAgg Batches") or 1) > 1):
            sev = "medium"
            ev.append(f"HashAggregate batches {n.get('HashAgg Batches')} – Disk {fmt_num(n.get('Disk Usage'))} kB")
        if sev:
            share = _share(plan, n.total_time_incl)
            if share >= 0.3:
                sev = bump(sev)
            add("PLAN-SPILL", sev, f"{n.describe()} tràn ra đĩa", [n], ev, score=share, data={"spill": True})

    # --------------------------------------------------- SUBPLAN NOT HASHED
    for sp in plan.nodes:
        if sp.parent_rel != "SubPlan" or not sp.subplan_name:
            continue
        info = subplan_info(plan, sp)
        if info["hashed"] or not info["any_type"]:
            continue
        holder = sp.parent
        ev = [f"Biểu thức: {info['expr'][:200]}",
              f"SubPlan ước lượng {fmt_num(sp.plan_rows)} dòng × {sp.get('Plan Width')} byte ≈ {_mb(info['hash_bytes'])} khi hash"]
        if info["limit_bytes"]:
            ev.append(f"Ngưỡng hash ≈ work_mem × hash_mem_multiplier = {_mb(info['limit_bytes'])}")
        if A and not sp.never_executed:
            loops = sp.loops
            if loops < 100:
                continue
            share = _share(plan, sp.total_time_incl)
            sev = "critical" if share >= 0.5 and loops >= 1000 else ("high" if loops >= 1000 else "medium")
            ev.insert(0, f"{sp.subplan_name} chạy {fmt_num(loops)} lần, mỗi lần ~{fmt_num(sp.actual_rows)} dòng, tổng {fmt_ms(sp.total_time_incl)}")
            add("PLAN-SUBPLAN-NOHASH", sev,
                f"{sp.subplan_name} không được hash: duyệt lại {fmt_num(loops)} lần ({_pct(share)} thời gian)",
                [sp, holder] if holder else [sp], ev, score=share * 10 + math.log10(loops))
        else:
            outer = holder.plan_rows if holder else 0
            comparisons = outer * sp.plan_rows / 2
            if sp.plan_rows < 1000 or comparisons < 1e6:
                continue
            sev = "critical" if comparisons >= 1e9 else ("high" if comparisons >= 1e8 else "medium")
            ev.insert(0, f"Ước lượng ≈ {fmt_num(outer)} dòng ngoài × {fmt_num(sp.plan_rows)} / 2 ≈ {fmt_num(comparisons)} phép so sánh")
            add("PLAN-SUBPLAN-NOHASH", sev,
                f"{sp.subplan_name} (NOT IN/= ANY) không được hash – ước lượng ~{fmt_num(comparisons)} phép so sánh",
                [sp, holder] if holder else [sp], ev, score=math.log10(max(comparisons, 1)))

    # ------------------------------------------------------------- JIT
    jt = plan.jit_total_ms
    if jt and (jt >= 1000 or (plan.execution_time and jt >= 0.1 * plan.execution_time)):
        share = jt / plan.execution_time if plan.execution_time else 0
        opts = plan.jit.get("Options") or {}
        add("PLAN-JIT", "high" if share >= 0.3 else "medium",
            f"JIT tốn {fmt_ms(jt)} ({_pct(share)} thời gian thực thi), {plan.jit.get('Functions')} functions",
            [], [f"Options: {opts}", f"Timing: {plan.jit.get('Timing')}"], score=share)

    if not A and plan.jit and (plan.jit.get("Options") or {}).get("Optimization"):
        add("PLAN-JIT", "info", f"JIT sẽ được dùng với inlining/optimization ({plan.jit.get('Functions')} functions) – "
                                f"tổng cost {fmt_num(plan.root.get('Total Cost'))} vượt ngưỡng jit_*_above_cost", [])

    # ------------------------------------------------------- PARALLEL
    for n in plan.nodes:
        if n.node_type in ("Gather", "Gather Merge") and A:
            wp, wl = n.get("Workers Planned"), n.get("Workers Launched")
            if wp is not None and wl is not None and wl < wp:
                add("PLAN-PARALLEL", "medium" if wl == 0 else "low",
                    f"{n.describe()}: Workers Launched {wl} < Planned {wp}", [n])

    # ----------------------------------------------------- HEAP FETCH
    for n in plan.nodes:
        if n.node_type == "Index Only Scan" and A:
            hf = float(n.get("Heap Fetches", 0) or 0)  # EXPLAIN prints the total (not per loop)
            rows = n.actual_rows_total or 0
            if hf >= 10000 and rows and hf >= 0.5 * rows:
                add("PLAN-HEAPFETCH", "medium", f"{n.describe()}: Heap Fetches {fmt_num(hf)} / {fmt_num(rows)} dòng",
                    [n], score=hf / max(rows, 1))

    # -------------------------------------------------------- TRIGGERS
    if plan.triggers and plan.execution_time:
        ttot = sum(float(t.get("Time", 0) or 0) for t in plan.triggers)
        share = ttot / plan.execution_time
        if share >= 0.1:
            ev = [f"{t.get('Trigger Name')}: {fmt_ms(t.get('Time'))}, {fmt_num(t.get('Calls'))} calls" for t in plan.triggers]
            add("PLAN-TRIGGER", "high" if share >= 0.4 else "medium",
                f"Trigger/FK chiếm {fmt_ms(ttot)} ({_pct(share)} thời gian)", [], ev, score=share)

    # -------------------------------------------------------- PLANNING
    pt = plan.planning_time
    if pt and (pt >= 1000 or (plan.execution_time and pt >= plan.execution_time and pt >= 200)):
        add("PLAN-PLANNING", "medium" if pt >= 1000 else "low", f"Planning Time {fmt_ms(pt)}", [])

    # ---------------------------------------------------- FILTER WASTE
    for n in plan.nodes:
        if A and n.node_type == "Seq Scan" and n.get("Rows Removed by Filter"):
            removed = float(n.get("Rows Removed by Filter")) * max(n.loops, 1)
            kept = n.actual_rows_total or 0
            if removed >= 1_000_000 and removed >= 100 * max(kept, 1) and n.loops <= 1:
                add("PLAN-FILTER-WASTE", "low",
                    f"{n.describe()} loại bỏ {fmt_num(removed)} dòng để giữ {fmt_num(kept)}", [n],
                    [f"Filter: {n.get('Filter')}"])

    # ------------------------------------------------------ CTE RESCAN
    for n in plan.nodes:
        if A and n.node_type == "CTE Scan" and n.loops >= 100 and n.id not in nl_rescan_inner_ids:
            share = _share(plan, n.total_time_incl)
            add("PLAN-CTE-RESCAN", "high" if share >= 0.2 else "medium",
                f"{n.describe()} bị quét {fmt_num(n.loops)} lần ({fmt_ms(n.total_time_incl)})", [n], score=share)

    # ------------------------------------------------ CTE COST BREAKDOWN
    # "SELECT from current_brand_code is very slow": is it the CTE BODY (the multi-table join, computed once) or the
    # CTE SCAN re-reading the stored result again and again (SubPlan without hash, Nested Loop inner side)?
    # The first CTE Scan that pulls rows also runs the body, so: re-read time ~= sum(CTE Scan incl. time) - body time.
    rt = plan.runtime_ms
    if A and rt:
        for root in plan.nodes:
            sn = root.subplan_name or ""
            if not sn.startswith("CTE ") or root.never_executed:
                continue
            cname = sn[4:]
            scans = [x for x in plan.nodes if x.node_type == "CTE Scan" and x.cte_name == cname and not x.never_executed]
            body = root.total_time_incl or 0.0
            scan_ms = sum(x.total_time_incl or 0.0 for x in scans)
            if not scans or (body <= 0 and scan_ms <= 0):
                continue
            loops = sum(x.loops for x in scans)
            reread = max(scan_ms - body, 0.0)
            sb, sr = min(body / rt, 1.0), min(reread / rt, 1.0)
            if sb + sr < 0.2:
                continue
            hot = max(scans, key=lambda x: x.total_time_incl or 0.0)
            ev = [f"Thân CTE (chạy 1 lần): {fmt_ms(body)} ({_pct(sb)} tổng), {fmt_num(root.actual_rows_total)} dòng",
                  f"{len(scans)} CTE Scan, tổng {fmt_num(loops)} lần quét, thời gian gộp {fmt_ms(scan_ms)}",
                  f"Đọc lại kết quả đã lưu: ≈ {fmt_ms(reread)} ({_pct(sr)} tổng)",
                  f"CTE Scan nặng nhất: {hot.describe()} – {fmt_num(hot.loops)} loop × ~{fmt_num(hot.actual_rows)} dòng, "
                  f"ngữ cảnh {_ctx(hot)}"]
            if sr >= sb and loops > 1:
                sev = "high" if sr >= 0.5 else "medium"
                msg = (f"CTE {cname}: thân CTE chỉ tốn {fmt_ms(body)}, nhưng kết quả bị quét lại {fmt_num(loops)} lần "
                       f"(+{fmt_ms(reread)}, {_pct(sr)} tổng) → chậm do QUÉT LẠI, không phải do join bên trong CTE")
                ev.append("Sửa chỗ khiến CTE bị quét lại (SubPlan không hash / Nested Loop), không phải thêm index cho thân CTE")
            else:
                # a body that dominates a short statement is not a problem worth more than "low"
                sev = "medium" if sb >= 0.5 and body >= 10_000 else "low"
                if loops <= 1:
                    ev[2] = f"Đọc + lọc kết quả CTE (1 lần): ≈ {fmt_ms(reread)} ({_pct(sr)} tổng)"
                msg = f"CTE {cname}: thân CTE chiếm {fmt_ms(body)} ({_pct(sb)} tổng) → nếu chậm, tối ưu join/scan bên trong CTE"
                ev.append("So plan của thân CTE giữa hai bản (join order/method, index, ước lượng) – có thể chạy riêng SELECT của CTE")
            add("PLAN-CTE-COST", sev, msg, [root, hot], ev, score=max(sb, sr) * 10,
                data={"cte": cname, "body_ms": body, "reread_ms": reread, "scan_loops": loops})

    # -------------------------------------------------- NOSTATS SIGNATURE
    for n in plan.nodes:
        if A and n.node_type == "Seq Scan" and not n.get("Filter") and n.loops == 1 and (n.actual_rows or 0) >= 1000:
            est, act = n.plan_rows, n.actual_rows or 0
            if abs(est - act) / act >= 0.15:
                add("PLAN-NOSTATS-SIGNATURE", "low",
                    f"{n.describe()} không có điều kiện lọc nhưng ước lượng {fmt_num(est)} vs thực tế {fmt_num(act)} – "
                    f"reltuples có thể chưa biết/cũ", [n])

    # --------------------------------------------------------- TEMP IO
    if A:
        r = plan.root
        tr, tw = float(r.get("Temp Read Blocks") or 0), float(r.get("Temp Written Blocks") or 0)
        gb = tr * 8192 / 1024 ** 3
        if gb >= 1:
            sev = "high" if gb >= 10 else "medium"
            ev = [f"temp read = {fmt_num(tr)} block (~{gb:.1f} GB), temp written = {fmt_num(tw)} block (~{tw * 8192 / 1024 ** 3:.2f} GB)"]
            if tw and tr >= 10 * tw:
                ev.append(f"Đọc gấp {tr / tw:.0f}× lượng ghi → cùng một file tạm bị đọc lại nhiều lần (kết quả trung gian bị quét lại)")
            hot = [x for x in plan.nodes if float(x.get("Temp Read Blocks") or 0) >= 0.5 * tr and x is not r]
            if hot:
                deepest = max(hot, key=lambda x: x.depth)
                ev.append(f"Node sâu nhất chịu phần lớn temp read: {deepest.describe()}")
            add("PLAN-TEMP-IO", sev, f"Đọc ~{gb:.1f} GB từ file tạm", [r] + ([deepest] if hot else []), ev, score=gb)

    # --------------------------------------------------------- IO COLD
    if A:
        r = plan.root
        hit, read = r.get("Shared Hit Blocks"), r.get("Shared Read Blocks")
        if hit is not None and read is not None and read >= 10000 and read / max(hit + read, 1) >= 0.5:
            iot = r.get("I/O Read Time")
            sev = "medium" if (iot and plan.execution_time and iot >= 0.3 * plan.execution_time) else "low"
            add("PLAN-IO-COLD", sev,
                f"{_pct(read / (hit + read))} block đọc từ đĩa (read {fmt_num(read)}, hit {fmt_num(hit)})"
                + (f", I/O read time {fmt_ms(iot)}" if iot else ""), [r])

    # ------------------------------------------------------------ SAOP
    for n in plan.nodes:
        cond = n.get("Index Cond") or ""
        if "Index" in n.node_type and re.search(r"=\s*ANY\s*\(", cond):
            share = _share(plan, n.total_time_incl) if A else 0
            if side == "new" and (not A or share >= 0.2):
                add("PLAN-SAOP", "low" if not A else "medium",
                    f"{n.describe()} dùng IN-list/= ANY" + (f", chiếm {_pct(share)} thời gian" if A else ""), [n],
                    [f"Index Cond: {cond[:300]}"])
    return out


UNIT_BYTES = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def _parse_mem(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    m = re.match(r"^\s*([\d.]+)\s*(B|kB|MB|GB)?\s*$", str(v))
    if not m:
        return None
    return float(m.group(1)) * UNIT_BYTES.get(m.group(2) or "kB", 1024)


def _mb(b: Optional[float]) -> str:
    if b is None:
        return "?"
    return f"{b / 1024 ** 2:.1f}MB" if b >= 1024 ** 2 else f"{b / 1024:.0f}kB"


DEFAULT_WORK_MEM = 4 * 1024 ** 2          # PostgreSQL default, also Cloud SQL default flag value (4MB, every tier)
HEAP_TUPLE_HEADER = 24                    # MAXALIGN(SizeofHeapTupleHeader = 23)


def default_hash_mem_multiplier(major: Optional[int]) -> float:
    """hash_mem_multiplier default: 1.0 on PG13/14, 2.0 from PG15 (commit 8f388f6f5)."""
    return 2.0 if (major or 17) >= 15 else 1.0


def subplan_info(plan: Plan, sp: Node) -> dict:
    """Is this SubPlan hashed? Is it an ANY / NOT IN style sublink? Hash table size vs limit, and the minimum
    work_mem / hash_mem_multiplier that makes the planner hash it again.

    PG13 prints  'NOT (hashed SubPlan 2)'      / 'NOT (SubPlan 2)'
    PG17 prints  'NOT (ANY (x = (hashed SubPlan 2).col1))' / 'NOT (ANY (x = (SubPlan 2).col1))'

    Planner rule (subselect.c subplan_is_hashable, identical in PG13..PG18):
        plan_rows * (MAXALIGN(plan_width) + MAXALIGN(SizeofHeapTupleHeader)) <= work_mem * hash_mem_multiplier
    and the sublink must be uncorrelated (no outer parameters).  Verified to the kB in the lab (E1).
    """
    name = sp.subplan_name or ""
    hashed_re = re.compile(r"hashed\s+" + re.escape(name) + r"\b")
    ref_re = re.compile(re.escape(name) + r"\b")
    expr, hashed = "", False
    for n in plan.nodes:
        for k, v in n.raw.items():
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if isinstance(x, str) and k != "Subplan Name" and ref_re.search(x):
                    expr = expr or f"{k}: {x}"
                    if hashed_re.search(x):
                        hashed = True
    any_type = hashed or bool(re.search(r"ANY\s*\(", expr)) or bool(
        re.search(r"NOT\s*\(\s*" + re.escape(name), expr)) or bool(re.search(r"\(\s*" + re.escape(name) + r"\s*\)", expr))
    width = float(sp.get("Plan Width") or 0)
    entry = ((int(width) + 7) // 8) * 8 + HEAP_TUPLE_HEADER
    hash_bytes = sp.plan_rows * entry
    sett = plan.settings or {}
    wm = _parse_mem(sett.get("work_mem"))
    hm = float(sett["hash_mem_multiplier"]) if sett.get("hash_mem_multiplier") else None
    wm_eff = wm or DEFAULT_WORK_MEM
    hm_eff = hm or default_hash_mem_multiplier(plan.major)
    limit = wm * (hm or 1.0) if wm else None      # only when EXPLAIN SETTINGS printed work_mem
    limit_eff = wm_eff * hm_eff
    return {"hashed": hashed, "any_type": any_type, "expr": expr, "hash_bytes": hash_bytes, "limit_bytes": limit,
            "rows": sp.plan_rows, "width": width, "entry_bytes": entry,
            "work_mem_bytes": wm_eff, "hash_mem_multiplier": hm_eff, "assumed_defaults": wm is None or hm is None,
            "limit_eff_bytes": limit_eff,
            "min_work_mem_kb": int(math.ceil(hash_bytes / (1024 * hm_eff))),
            "min_hash_mem_multiplier": hash_bytes / wm_eff,
            "correlated": subplan_correlated(sp)}


_QUAL_RE = re.compile(r"(?<![\w.)])([a-z_][\w$]*)\.(?:[a-z_][\w$]*|\"[^\"]+\")", re.I)
_COND_KEYS = ("Filter", "Index Cond", "Recheck Cond", "Join Filter", "Hash Cond", "Merge Cond", "One-Time Filter",
              "TID Cond", "Cache Key")


def subplan_correlated(sp: Node) -> bool:
    """Heuristic: does the SubPlan reference columns of the outer query (correlated -> can never be hashed)?

    PG13 prints outer references as $n params; PG17 prints the outer column itself (alias.col) inside the subplan."""
    inside = {x.alias for x in sp.walk() if x.alias} | {x.relation for x in sp.walk() if x.relation} | \
             {x.cte_name for x in sp.walk() if x.cte_name}
    for x in sp.walk():
        for k in _COND_KEYS:
            v = x.get(k)
            for t in (v if isinstance(v, list) else [v]):
                if not isinstance(t, str):
                    continue
                if re.search(r"(?<![\w)])\$\d+", t):
                    return True
                for alias in _QUAL_RE.findall(re.sub(r"'(?:[^']|'')*'", "''", t)):
                    if alias not in inside and alias.lower() not in ("pg_catalog", "public"):
                        return True
    return False


def _merge_inner(n: Node) -> bool:
    c, p = n, n.parent
    while p is not None:
        if p.node_type == "Merge Join":
            return c is p.inner_child()
        if p.node_type not in ("Sort", "Materialize", "Incremental Sort"):
            return False
        c, p = p, p.parent
    return False


def _may_stop_early(n: Node) -> bool:
    """True if the executor may legitimately stop pulling rows from n before it is exhausted."""
    c, p = n, n.parent
    while p is not None:
        if p.node_type in ("Limit",):
            return True
        if p.node_type == "Merge Join":
            return True
        if p.node_type in ("Nested Loop", "Hash Join") and p.get("Join Type") in ("Semi", "Anti") and c is p.inner_child():
            return True
        if p.node_type not in ("Sort", "Materialize", "Hash", "Memoize", "Gather", "Gather Merge", "Result", "Unique",
                               "Aggregate", "Subquery Scan", "Incremental Sort"):
            return False
        c, p = p, p.parent
    return False


def _is_parameterized(n: Node) -> bool:
    txt = " ".join(str(n.get(k) or "") for k in ("Recheck Cond", "Index Cond"))
    for c in n.children:
        txt += " " + str(c.get("Index Cond") or "")
    # parameterized conditions reference outer columns (alias.col) or $n
    return bool(re.search(r"\$\d+|=\s*\(?[a-z_][\w]*\.[a-z_]", txt, re.I))
