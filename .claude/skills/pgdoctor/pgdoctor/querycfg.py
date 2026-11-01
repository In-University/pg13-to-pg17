"""Effective-configuration diff for ONE statement.

Input: the pg_settings captured in the session that ran the statement on each server (see runner.py), both plans
and the findings.  Output: only the parameters that can change how THIS statement is planned or executed, ranked by
how directly they relate to the plan:

  critical  the parameter is part of the mechanism of a finding (e.g. work_mem / hash_mem_multiplier for a
            NOT IN SubPlan that lost 'hashed'); shown for both sides even when the values are equal
  plan      governs a node type that appears in one of the plans (Hash Join -> enable_hashjoin, work_mem…)
  planner   planner-wide (cost constants, search_path, statistics target…)
  other     in a planner/executor category but not tied to anything in these plans

EXPLAIN (SETTINGS) is not enough for this: it only prints parameters that differ from the built-in default, so a
default that changed between versions (hash_mem_multiplier 1.0 on PG13 -> 2.0 on PG15+) never shows up there.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .findings import SEV_RANK, Finding
from .plan import Plan
from .rules_plan import subplan_info
from .settings import NEW_PLANNER_GUCS, UNIT_FACTORS, WATCH, normalize, pretty

CATEGORY_PREFIXES = ("Query Tuning", "Resource Usage / Memory", "Resource Usage / Asynchronous Behavior",
                     "Resource Usage / Disk", "Resource Usage / I/O", "Resource Usage / Worker Processes")
EXTRA = {"jit", "jit_provider", "search_path", "statement_timeout", "lock_timeout", "plan_cache_mode",
         "default_statistics_target", "compute_query_id", "shared_preload_libraries", "session_preload_libraries",
         "temp_file_limit", "transform_null_equals", "default_transaction_isolation", "standard_conforming_strings"}
EXCLUDE = {"autovacuum_work_mem", "maintenance_work_mem", "logical_decoding_work_mem", "max_stack_depth",
           "shared_memory_type", "dynamic_shared_memory_type", "huge_pages", "huge_page_size", "huge_pages_status",
           "min_dynamic_shared_memory", "vacuum_buffer_usage_limit", "max_prepared_transactions", "shared_memory_size",
           "shared_memory_size_in_huge_pages", "max_parallel_maintenance_workers", "backend_flush_after",
           "maintenance_io_concurrency", "max_notify_queue_pages", "io_method", "io_workers", "io_max_concurrency",
           "file_copy_method", "debug_discard_caches"}
SOURCE_OVERRIDES = ("database", "user", "database user", "client", "session", "override")

PARALLEL = ("max_parallel_workers_per_gather", "max_parallel_workers", "max_worker_processes", "parallel_setup_cost",
            "parallel_tuple_cost", "min_parallel_table_scan_size", "min_parallel_index_scan_size",
            "enable_parallel_hash", "enable_parallel_append", "enable_gathermerge", "parallel_leader_participation")
JIT = ("jit", "jit_above_cost", "jit_inline_above_cost", "jit_optimize_above_cost", "jit_provider")
JOIN_METHODS = ("enable_nestloop", "enable_hashjoin", "enable_mergejoin", "enable_material")
SCAN_METHODS = ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan", "enable_bitmapscan",
                "random_page_cost", "seq_page_cost", "effective_cache_size")
HASH_MEM = ("work_mem", "hash_mem_multiplier")

# plan feature -> parameters that govern it
FEATURE_GUCS: Dict[str, Tuple[str, ...]] = {
    "Hash Join": ("enable_hashjoin",) + HASH_MEM,
    "Hash": HASH_MEM,
    "Merge Join": ("enable_mergejoin", "work_mem"),
    "Nested Loop": ("enable_nestloop",),
    "Memoize": ("enable_memoize",) + HASH_MEM,
    "Materialize": ("enable_material", "work_mem"),
    "HashAggregate": ("enable_hashagg",) + HASH_MEM,
    "Aggregate": ("enable_presorted_aggregate", "enable_group_by_reordering"),
    "Sort": ("enable_sort", "work_mem"),
    "Incremental Sort": ("enable_incremental_sort", "enable_sort", "work_mem"),
    "Seq Scan": ("enable_seqscan", "seq_page_cost"),
    "Index Scan": ("enable_indexscan", "random_page_cost", "effective_cache_size", "cpu_index_tuple_cost"),
    "Index Only Scan": ("enable_indexonlyscan", "random_page_cost", "effective_cache_size"),
    "Bitmap Heap Scan": ("enable_bitmapscan", "effective_io_concurrency", "work_mem", "random_page_cost",
                         "effective_cache_size"),
    "Tid Scan": ("enable_tidscan",), "Tid Range Scan": ("enable_tidscan",),
    "Parallel": PARALLEL,
    "CTE Scan": ("work_mem",),
    "WindowAgg": ("work_mem",),
    "HashSetOp": ("enable_hashagg",) + HASH_MEM,
    "Append": ("enable_partition_pruning", "enable_partitionwise_join", "enable_partitionwise_aggregate",
               "enable_parallel_append", "enable_async_append", "constraint_exclusion"),
    "Merge Append": ("enable_partition_pruning", "enable_partitionwise_join", "constraint_exclusion"),
    "SubPlan (hashed)": HASH_MEM,
    "SubPlan (không hash)": HASH_MEM,
    "JIT": JIT,
    "Nhiều bảng (≥8)": ("join_collapse_limit", "from_collapse_limit", "geqo", "geqo_threshold"),
    "Prepared statement": ("plan_cache_mode",),
}
PLANNER_WIDE = ("random_page_cost", "seq_page_cost", "cpu_tuple_cost", "cpu_index_tuple_cost", "cpu_operator_cost",
                "effective_cache_size", "default_statistics_target", "search_path", "join_collapse_limit",
                "from_collapse_limit", "geqo_threshold", "cursor_tuple_fraction", "constraint_exclusion",
                "recursive_worktable_factor", "transform_null_equals")

# finding rule -> parameters that are part of its mechanism
RULE_GUCS: Dict[str, Tuple[str, ...]] = {
    "PLAN-SUBPLAN-NOHASH": HASH_MEM, "CMP-SUBPLAN-HASH-LOST": HASH_MEM,
    "PLAN-NL-RESCAN": JOIN_METHODS, "PLAN-NL-BET": JOIN_METHODS, "PLAN-CTE-RESCAN": ("work_mem",) + JOIN_METHODS,
    "CMP-CTE-CORRELATION": JOIN_METHODS + HASH_MEM,
    "PLAN-MEMOIZE": ("enable_memoize",) + HASH_MEM,
    "PLAN-SPILL": HASH_MEM, "CMP-SPILL": HASH_MEM, "PLAN-TEMP-IO": ("work_mem", "temp_file_limit"),
    "PLAN-JIT": JIT, "CMP-JIT": JIT, "PLAN-PARALLEL": PARALLEL,
    "CMP-GROUPKEY-ORDER": ("enable_group_by_reordering",),
    "PLAN-COLLAPSE": ("join_collapse_limit", "from_collapse_limit", "geqo_threshold"),
    "CMP-JOIN-METHOD": JOIN_METHODS + HASH_MEM + ("random_page_cost", "effective_cache_size"),
    "CMP-SCAN-METHOD": SCAN_METHODS,
    "CMP-NEW-NODE": ("enable_memoize", "enable_incremental_sort", "enable_presorted_aggregate",
                     "enable_group_by_reordering"),
}
NEW_NODE_GUC = {"Memoize": "enable_memoize", "Incremental Sort": "enable_incremental_sort"}


def _relevant_name(name: str, rec: Optional[Dict[str, str]]) -> bool:
    if name in EXCLUDE:
        return False
    if name in EXTRA or name in WATCH or name.startswith("enable_") or name.startswith("pg_hint_plan."):
        return True
    if name.endswith("_buffers") and name not in ("shared_buffers", "temp_buffers"):
        return False
    cat = (rec or {}).get("category") or ""
    return any(cat.startswith(p) for p in CATEGORY_PREFIXES)


def plan_features(p: Optional[Plan]) -> Dict[str, List[str]]:
    feats: Dict[str, List[str]] = {}
    if not p:
        return feats

    def add(f: str, example: str) -> None:
        lst = feats.setdefault(f, [])
        if example not in lst and len(lst) < 4:
            lst.append(example)
    rels: Set[str] = set()
    for n in p.nodes:
        t = n.node_type
        lbl = n.label[:60]
        if t == "Aggregate":
            hashed = (n.get("Strategy") or "").lower() in ("hashed", "mixed")
            add("HashAggregate" if hashed else "Aggregate", lbl)
        elif t == "SetOp" and (n.get("Strategy") or "").lower() == "hashed":
            add("HashSetOp", lbl)
        elif t in FEATURE_GUCS:
            add(t, lbl)
        if t in ("Gather", "Gather Merge") or n.get("Parallel Aware"):
            add("Parallel", lbl)
        if n.relation:
            rels.add(n.relation)
        if n.subplan_name and (n.get("Parent Relationship") == "SubPlan"):
            info = subplan_info(p, n)
            if info["any_type"]:
                add("SubPlan (hashed)" if info["hashed"] else "SubPlan (không hash)", n.subplan_name)
    if p.jit:
        add("JIT", "JIT")
    if len(rels) >= 8:
        add("Nhiều bảng (≥8)", f"{len(rels)} bảng")
    return feats


def _num(rec: Optional[Dict[str, str]]) -> Optional[float]:
    if not rec:
        return None
    try:
        v = float(rec.get("setting", ""))
    except ValueError:
        return None
    u = rec.get("unit") or ""
    return v * UNIT_FACTORS.get(u, 1) if u in ("B", "kB", "8kB", "MB", "GB", "16kB", "32kB") else v


def _n(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _fmt_bytes(b: Optional[float]) -> str:
    if b is None:
        return "?"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024:.0f} kB"


def subplan_thresholds(p: Optional[Plan], sett: Dict[str, Dict[str, str]], label: str) -> List[Dict[str, Any]]:
    """Hash decision of every ANY/NOT IN SubPlan, computed with the REAL session values (not EXPLAIN SETTINGS)."""
    out: List[Dict[str, Any]] = []
    if not p:
        return out
    wm = _num(sett.get("work_mem"))
    hm = _num(sett.get("hash_mem_multiplier"))
    for n in p.nodes:
        if not (n.subplan_name and n.get("Parent Relationship") == "SubPlan"):
            continue
        info = subplan_info(p, n)
        if not info["any_type"] or wm is None:
            continue
        hm_eff = hm if hm is not None else info["hash_mem_multiplier"]
        limit = wm * hm_eff
        need = info["hash_bytes"]
        fits = need <= limit and not info["correlated"]
        text = (f"{label} {n.subplan_name}: ước lượng {_n(info['rows'])} dòng × {info['entry_bytes']} B = {_fmt_bytes(need)} "
                f"{'≤' if need <= limit else '>'} work_mem {pretty(sett['work_mem'])} × hash_mem_multiplier {hm_eff:g} = "
                f"{_fmt_bytes(limit)} → {'được hash' if fits else 'KHÔNG hash'}"
                f"{' (subquery tương quan: không bao giờ hash)' if info['correlated'] else ''}"
                f" · plan: {'hashed SubPlan' if info['hashed'] else 'SubPlan không hash'}")
        if not fits and not info["correlated"]:
            text += (f" · cần work_mem ≥ {int(-(-need // (1024 * hm_eff)))}kB hoặc hash_mem_multiplier ≥ "
                     f"{need / wm:.2f}")
        out.append({"label": label, "subplan": n.subplan_name, "rows": info["rows"], "need_bytes": need,
                    "limit_bytes": limit, "fits": fits, "hashed": info["hashed"], "text": text})
    return out


def _why(name: str) -> str:
    if name in WATCH:
        return WATCH[name][1]
    if name in NEW_PLANNER_GUCS:
        return NEW_PLANNER_GUCS[name]
    if name.startswith("enable_"):
        return "Công tắc cho phép/cấm một loại node của planner."
    if name.startswith("pg_hint_plan."):
        return "Tham số của pg_hint_plan (hint table, debug…)."
    return {"search_path": "Quyết định tên bảng/hàm không ghi schema trỏ tới đâu.",
            "temp_file_limit": "Giới hạn dung lượng file tạm của một session (sort/hash/tuplestore tràn đĩa).",
            "compute_query_id": "Cần để có query_id (pg_stat_statements, hint table của pg_hint_plan).",
            "shared_preload_libraries": "Extension nạp sẵn (pg_hint_plan, pg_stat_statements, auto_explain…).",
            "statement_timeout": "Giới hạn thời gian câu lệnh.",
            "plan_cache_mode": "Generic plan hay custom plan cho prepared statement (JDBC sau 5 lần thực thi)."
            }.get(name, "")


def query_config_diff(old_sett: Dict[str, Dict[str, str]], new_sett: Dict[str, Dict[str, str]],
                      old_plan: Optional[Plan], new_plan: Optional[Plan], findings: Sequence[Finding],
                      labels: Tuple[str, str] = ("PG13", "PG17"), prepared: bool = False,
                      explain_settings: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Any]:
    lo, ln = labels
    f_old, f_new = plan_features(old_plan), plan_features(new_plan)
    if prepared:
        f_new.setdefault("Prepared statement", ["EXECUTE"])
    guc_feats: Dict[str, List[str]] = {}
    for side, feats in ((lo, f_old), (ln, f_new)):
        for feat, _ in feats.items():
            for g in FEATURE_GUCS.get(feat, ()):
                tag = f"{feat} ({side})"
                if tag not in guc_feats.setdefault(g, []):
                    guc_feats[g].append(tag)
    critical: Dict[str, List[str]] = {}
    for f in findings:
        if f.side in ("new", "both") and SEV_RANK.get(f.severity, 9) <= SEV_RANK["medium"]:
            for g in RULE_GUCS.get(f.rule, ()):
                if f.rule not in critical.setdefault(g, []):
                    critical[g].append(f.rule)
    for feat, guc in NEW_NODE_GUC.items():
        if feat in f_new and feat not in f_old:
            critical.setdefault(guc, []).append(f"node mới: {feat}")

    rows: List[Dict[str, Any]] = []
    for name in sorted(set(old_sett) | set(new_sett)):
        o, n = old_sett.get(name), new_sett.get(name)
        if not _relevant_name(name, n or o):
            continue
        if o and n:
            if normalize(o.get("setting", ""), o.get("unit")) == normalize(n.get("setting", ""), n.get("unit")):
                continue
            kind = "changed"
        else:
            kind = "only_new" if n else "only_old"
        default_changed = bool(o and n and o.get("source") == "default" and n.get("source") == "default"
                               and o.get("boot_val") != n.get("boot_val"))
        if name in critical:
            rel = "critical"
        elif name in guc_feats:
            rel = "plan"
        elif kind == "changed" and (name in PLANNER_WIDE or name in WATCH or name.startswith("enable_")):
            rel = "planner"
        else:
            rel = "other"          # incl. parameters that exist on one version only and govern nothing in these plans
        rows.append({"name": name, "kind": kind, "relevance": rel, "default_changed": default_changed,
                     "old": pretty(o) if o else "—", "new": pretty(n) if n else "—",
                     "old_src": (o or {}).get("source", ""), "new_src": (n or {}).get("source", ""),
                     "category": (n or o or {}).get("category", ""), "why": _why(name),
                     "nodes": guc_feats.get(name, []), "rules": critical.get(name, [])})
    order = {"critical": 0, "plan": 1, "planner": 2, "other": 3}
    rows.sort(key=lambda r: (order[r["relevance"]], r["kind"] != "changed", r["name"]))

    key: List[Dict[str, Any]] = []
    for name in sorted(critical, key=lambda g: (g not in HASH_MEM, g)):
        o, n = old_sett.get(name), new_sett.get(name)
        if not (o or n):
            continue
        same = bool(o and n and normalize(o.get("setting", ""), o.get("unit")) ==
                    normalize(n.get("setting", ""), n.get("unit")))
        key.append({"name": name, "old": pretty(o) if o else "—", "new": pretty(n) if n else "—",
                    "old_src": (o or {}).get("source", ""), "new_src": (n or {}).get("source", ""),
                    "same": same, "rules": critical[name], "why": _why(name)})

    thresholds = subplan_thresholds(old_plan, old_sett, lo) + subplan_thresholds(new_plan, new_sett, ln)
    verdict = _verdict(rows, key, thresholds, labels)
    return {"rows": rows, "key": key, "thresholds": thresholds, "verdict": verdict,
            "features": {lo: f_old, ln: f_new},
            "explain_settings": explain_settings or {lo: dict(old_plan.settings) if old_plan else {},
                                                     ln: dict(new_plan.settings) if new_plan else {}},
            "counts": {r: sum(1 for x in rows if x["relevance"] == r) for r in order}}


def _verdict(rows, key, thresholds, labels) -> List[str]:
    lo, ln = labels
    out: List[str] = []
    crit_changed = [r for r in rows if r["relevance"] == "critical" and r["kind"] == "changed"]
    real = [r for r in crit_changed if not r["default_changed"]]
    by_default = [r for r in crit_changed if r["default_changed"]]
    if real:
        out.append("Config KHÁC ở tham số then chốt của plan này: " +
                   ", ".join(f"{r['name']} {r['old']} ({r['old_src']}) → {r['new']} ({r['new_src']})" for r in real) +
                   ". Đồng bộ hoặc giải thích khác biệt này trước khi kết luận do phiên bản.")
    if by_default:
        out.append("Khác do mặc định đổi theo phiên bản (không ai cấu hình): " +
                   ", ".join(f"{r['name']} {r['old']} → {r['new']}" for r in by_default) + ".")
    if key and not real:
        same = [k["name"] for k in key if k["same"]]
        if same:
            out.append("Các tham số then chốt giống nhau ở hai bản: " + ", ".join(same) + ".")
    plan_changed = [r for r in rows if r["relevance"] == "plan" and r["kind"] == "changed"]
    if plan_changed:
        out.append("Khác ở tham số điều khiển node có trong plan: " +
                   ", ".join(f"{r['name']} {r['old']} ({r['old_src']}) → {r['new']} ({r['new_src']})" for r in plan_changed[:8]) +
                   (" – có giá trị đặt bằng ALTER ROLE/DATABASE … SET hoặc tham số kết nối."
                    if any(r["old_src"] in SOURCE_OVERRIDES or r["new_src"] in SOURCE_OVERRIDES for r in plan_changed) else "."))
    t_old = [t for t in thresholds if t["label"] == lo]
    t_new = [t for t in thresholds if t["label"] == ln]
    for tn in t_new:
        to = next((t for t in t_old if t["subplan"] == tn["subplan"]), None)
        if to and to["fits"] and not tn["fits"]:
            more_mem = tn["limit_bytes"] >= to["limit_bytes"]
            out.append(f"{tn['subplan']}: {ln} có ngân sách bộ nhớ {'BẰNG hoặc LỚN HƠN' if more_mem else 'NHỎ HƠN'} "
                       f"{lo} ({_fmt_bytes(tn['limit_bytes'])} so với {_fmt_bytes(to['limit_bytes'])}) nhưng ước lượng "
                       f"subquery tăng từ {_n(to['rows'])} lên {_n(tn['rows'])} dòng" +
                       (" → nguyên nhân là ƯỚC LƯỢNG của planner, không phải config." if more_mem else
                        " → cả config lẫn ước lượng đều góp phần."))
    if not out:
        out.append("Không có khác biệt config nào liên quan trực tiếp tới các node của plan này → khác biệt plan "
                   "đến từ planner/thống kê của phiên bản mới.")
    return out


def config_findings(diff: Dict[str, Any], labels: Tuple[str, str]) -> List[Finding]:
    lo, ln = labels
    F: List[Finding] = []
    for r in diff["rows"]:
        if r["relevance"] not in ("critical", "plan"):
            continue
        if r["kind"] == "changed":
            sev = "high" if r["relevance"] == "critical" and not r["default_changed"] else "medium"
            if r["relevance"] == "plan":
                sev = "low" if r["default_changed"] else "medium"
            ev = [r["why"], f"source: {r['old_src'] or '?'} → {r['new_src'] or '?'}"
                  + (" (mặc định đổi theo phiên bản)" if r["default_changed"] else "")]
            if r["rules"]:
                ev.append("Liên quan finding: " + ", ".join(r["rules"]))
            if r["nodes"]:
                ev.append("Node trong plan: " + ", ".join(r["nodes"][:6]))
            if r["old_src"] in SOURCE_OVERRIDES or r["new_src"] in SOURCE_OVERRIDES:
                ev.append("Giá trị đến từ ALTER ROLE/DATABASE … SET hoặc tham số kết nối của client – kiểm tra cả "
                          "role_settings.json và connection string của ứng dụng.")
            F.append(Finding("QCFG-DIFF", sev, "settings", r["name"],
                             f"Khi chạy câu này: {r['name']} = {r['old']} ({lo}) → {r['new']} ({ln})"
                             + (" – do mặc định đổi theo phiên bản" if r["default_changed"] else ""), ev, side="both",
                             data={k: r[k] for k in ("relevance", "default_changed", "old_src", "new_src")}))
        elif r["kind"] == "only_new" and r["relevance"] == "critical":
            F.append(Finding("QCFG-NEW-GUC", "medium", "settings", r["name"],
                             f"{ln} dùng tính năng không có ở {lo}: {r['name']} = {r['new']} ({_why(r['name'])})",
                             [f"Liên quan: {', '.join(r['rules'])}",
                              f"Chẩn đoán: BEGIN; SET LOCAL {r['name']} = off; EXPLAIN (ANALYZE) …; ROLLBACK;"],
                             side="new"))
    planner_rows = [r for r in diff["rows"] if r["relevance"] == "planner" and r["kind"] == "changed"]
    if planner_rows:
        F.append(Finding("QCFG-DIFF", "low", "settings", f"{len(planner_rows)} tham số",
                         f"{len(planner_rows)} tham số planner khác nhau (cost/statistics/search_path…)",
                         [f"{r['name']}: {r['old']} ({r['old_src']}) → {r['new']} ({r['new_src']})" for r in planner_rows[:15]],
                         side="both"))
    for v in diff["verdict"]:
        if "ƯỚC LƯỢNG" in v:
            F.append(Finding("QCFG-VERDICT", "info", "settings", "kết luận config", v, side="both"))
    return F
