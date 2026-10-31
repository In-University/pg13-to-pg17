"""pg_settings snapshot comparison."""
from __future__ import annotations

import csv
import io
import json
from typing import Dict, List, Optional, Tuple

from .findings import Finding

# name -> (impact, note).  impact: high = changes plans/exec directly
WATCH: Dict[str, Tuple[str, str]] = {
    "work_mem": ("high", "Bộ nhớ mỗi sort/hash; ảnh hưởng cost và tràn đĩa."),
    "hash_mem_multiplier": ("high", "Mặc định 1.0 (PG13–14) → 2.0 (PG15+). Hash join/agg được dùng nhiều RAM hơn → cost model ưu tiên hash."),
    "random_page_cost": ("high", "Chi phí đọc ngẫu nhiên; quyết định index scan vs seq scan."),
    "seq_page_cost": ("high", "Chi phí đọc tuần tự."),
    "effective_cache_size": ("high", "Planner giả định bao nhiêu dữ liệu nằm trong cache → index scan rẻ hơn."),
    "cpu_tuple_cost": ("high", "Cost model."), "cpu_index_tuple_cost": ("high", "Cost model."),
    "cpu_operator_cost": ("high", "Cost model."),
    "default_statistics_target": ("high", "Độ chi tiết MCV/histogram khi ANALYZE."),
    "join_collapse_limit": ("high", "Số bảng tối đa planner tự sắp xếp thứ tự join."),
    "from_collapse_limit": ("high", "Gộp subquery vào FROM để tối ưu thứ tự join."),
    "geqo": ("medium", "Genetic optimizer cho nhiều bảng join."), "geqo_threshold": ("medium", "Số bảng kích hoạt GEQO."),
    "plan_cache_mode": ("high", "Generic vs custom plan cho prepared statement."),
    "jit": ("high", "Bật/tắt JIT."), "jit_above_cost": ("high", "Ngưỡng cost bật JIT."),
    "jit_inline_above_cost": ("medium", "Ngưỡng inlining JIT."), "jit_optimize_above_cost": ("medium", "Ngưỡng tối ưu JIT."),
    "max_parallel_workers_per_gather": ("high", "Số worker song song mỗi Gather."),
    "max_parallel_workers": ("medium", "Tổng worker song song."), "max_worker_processes": ("medium", "Giới hạn background workers."),
    "parallel_setup_cost": ("medium", "Cost khởi tạo song song."), "parallel_tuple_cost": ("medium", "Cost chuyển tuple song song."),
    "min_parallel_table_scan_size": ("medium", "Ngưỡng bảng để scan song song."),
    "min_parallel_index_scan_size": ("medium", "Ngưỡng index để scan song song."),
    "effective_io_concurrency": ("medium", "Prefetch cho bitmap heap scan (PG18 đổi mặc định 1→16)."),
    "maintenance_io_concurrency": ("low", "Prefetch cho maintenance."),
    "io_combine_limit": ("low", "PG17: streaming I/O gộp đọc."),
    "constraint_exclusion": ("medium", "Loại partition/bảng con theo CHECK."),
    "cursor_tuple_fraction": ("medium", "Ưu tiên plan trả dòng đầu nhanh cho cursor."),
    "shared_buffers": ("medium", "Cache dữ liệu."), "temp_buffers": ("low", "Bộ nhớ bảng tạm."),
    "maintenance_work_mem": ("low", "Bộ nhớ cho VACUUM/CREATE INDEX."),
    "autovacuum": ("medium", "Tắt autovacuum → stats/visibility map không được cập nhật."),
    "autovacuum_analyze_scale_factor": ("medium", "Ngưỡng tự ANALYZE."),
    "autovacuum_analyze_threshold": ("low", "Ngưỡng tự ANALYZE."),
    "autovacuum_vacuum_scale_factor": ("low", "Ngưỡng tự VACUUM."),
    "autovacuum_naptime": ("low", "Chu kỳ autovacuum."), "autovacuum_max_workers": ("low", "Số worker autovacuum."),
    "track_io_timing": ("low", "Cần để thấy I/O time trong EXPLAIN."),
    "statement_timeout": ("low", "Timeout câu lệnh."), "lock_timeout": ("low", "Timeout khoá."),
    "synchronous_commit": ("low", "Độ trễ commit (ảnh hưởng UPDATE lớn)."),
    "max_wal_size": ("low", "Tần suất checkpoint khi ghi nhiều."), "checkpoint_timeout": ("low", "Chu kỳ checkpoint."),
    "wal_compression": ("low", "Nén WAL."), "full_page_writes": ("low", "WAL volume."),
}
REMOVED_NOTES = {
    "vacuum_cleanup_index_scale_factor": "bị bỏ ở PG14",
    "operator_precedence_warning": "bị bỏ ở PG14",
    "stats_temp_directory": "bị bỏ ở PG15 (stats chuyển vào shared memory)",
    "force_parallel_mode": "đổi tên thành debug_parallel_query ở PG16",
    "promote_trigger_file": "bị bỏ ở PG16",
    "vacuum_defer_cleanup_age": "bị bỏ ở PG16",
    "old_snapshot_threshold": "bị bỏ ở PG17",
    "db_user_namespace": "bị bỏ ở PG17",
    "trace_recovery_messages": "bị bỏ ở PG17",
    "wal_keep_segments": "thay bằng wal_keep_size ở PG13",
}
NEW_PLANNER_GUCS = {
    "enable_memoize": "PG14 – Memoize cho Nested Loop",
    "enable_async_append": "PG14 – async Append (FDW)",
    "enable_presorted_aggregate": "PG16 – aggregate ORDER BY/DISTINCT dùng input đã sort",
    "enable_group_by_reordering": "PG17 – đổi thứ tự GROUP BY theo sort order của node con",
    "enable_distinct_reordering": "PG18 – đổi thứ tự DISTINCT",
    "enable_self_join_elimination": "PG18 – bỏ self-join thừa",
    "enable_incremental_sort": "PG13 – Incremental Sort",
}
UNIT_FACTORS = {"B": 1, "kB": 1024, "8kB": 8192, "16kB": 16384, "32kB": 32768, "MB": 1024 ** 2, "GB": 1024 ** 3,
                "ms": 1, "s": 1000, "min": 60000, "h": 3600000, "d": 86400000, "us": 0.001}


def load_settings(path: str) -> Dict[str, Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read().strip()
    rows: List[Dict[str, str]]
    if text.startswith("[") or text.startswith("{"):
        obj = json.loads(text)
        rows = obj["settings"] if isinstance(obj, dict) and "settings" in obj else obj
    else:
        rows = list(csv.DictReader(io.StringIO(text, newline='')))
    out = {}
    for r in rows:
        name = r.get("name")
        if name:
            out[name] = {k: ("" if v is None else str(v)) for k, v in r.items()}
    return out


def normalize(val: str, unit: Optional[str]) -> str:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return (val or "").strip().lower()
    if unit and unit in UNIT_FACTORS:
        num *= UNIT_FACTORS[unit]
    return repr(round(num, 6))


def pretty(rec: Dict[str, str]) -> str:
    v, u = rec.get("setting", ""), rec.get("unit") or ""
    try:
        num = float(v)
        if u in ("kB", "8kB", "MB", "B", "16kB", "32kB"):
            b = num * UNIT_FACTORS[u]
            for unit, f in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("kB", 1024)):
                if b >= f and b % f == 0:
                    return f"{int(b // f)}{unit}"
            return f"{int(b)}B"
        if u in ("ms", "s", "min") and num >= 0:
            return f"{v}{u}"
    except ValueError:
        pass
    return f"{v}{u if u and u not in ('8kB',) else ''}"


def compare_settings(old: Dict[str, Dict[str, str]], new: Dict[str, Dict[str, str]]) -> Tuple[List[Finding], List[Dict[str, str]]]:
    F: List[Finding] = []
    table: List[Dict[str, str]] = []
    for name in sorted(set(old) | set(new)):
        o, n = old.get(name), new.get(name)
        watched = name in WATCH or name.startswith("enable_")
        if o and n:
            same = normalize(o.get("setting", ""), o.get("unit")) == normalize(n.get("setting", ""), n.get("unit"))
            if not same and watched:
                impact, note = WATCH.get(name, ("high", "Công tắc loại plan."))
                table.append({"name": name, "old": pretty(o), "new": pretty(n), "impact": impact, "note": note,
                              "source_old": o.get("source", ""), "source_new": n.get("source", "")})
                sev = {"high": "medium", "medium": "low", "low": "info"}[impact]
                if name in ("hash_mem_multiplier",) and o.get("source") == "default" and n.get("source") == "default":
                    note += " (giá trị khác do thay đổi mặc định giữa các phiên bản)"
                F.append(Finding("SET-DIFF", sev, "settings", name, f"{name}: {pretty(o)} → {pretty(n)}",
                                 [note, f"source: {o.get('source','?')} → {n.get('source','?')}"]))
        elif o and not n:
            if name in REMOVED_NOTES or watched:
                F.append(Finding("SET-REMOVED", "low" if o.get("source") not in ("default", "") else "info", "settings", name,
                                 f"{name} = {pretty(o)} không còn ở bản mới ({REMOVED_NOTES.get(name, 'đã bị bỏ/đổi tên')})"))
        elif n and not o and name in NEW_PLANNER_GUCS:
            F.append(Finding("SET-NEW-PLANNER-GUC", "info", "settings", name,
                             f"{name} = {pretty(n)} – {NEW_PLANNER_GUCS[name]}",
                             [f"Chẩn đoán: BEGIN; SET LOCAL {name} = off; EXPLAIN (ANALYZE) ...; ROLLBACK;"]))
    for side, snap in (("old", old), ("new", new)):
        for name, rec in snap.items():
            if name.startswith("enable_") and rec.get("setting") == "off" and rec.get("boot_val", "on") != "off":
                F.append(Finding("SET-ENABLE-OFF", "medium", "settings", name,
                                 f"{name} = off trên bản {'cũ' if side == 'old' else 'mới'} (source: {rec.get('source')})", side=side))
    return F, table
