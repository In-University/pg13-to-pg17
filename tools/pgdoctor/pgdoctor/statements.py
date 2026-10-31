"""pg_stat_statements snapshot comparison (match by normalised query text)."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, List, Tuple

from .findings import Finding, fmt_ms, fmt_num

ALIASES = {
    "total_exec_time": ("total_exec_time", "total_time"),
    "mean_exec_time": ("mean_exec_time", "mean_time"),
}


def _f(r: Dict[str, Any], *names, default=0.0) -> float:
    for n in names:
        if n in r and r[n] not in (None, ""):
            try:
                return float(r[n])
            except ValueError:
                pass
    return default


def normalize_query(q: str) -> str:
    q = re.sub(r"/\*.*?\*/", " ", q or "", flags=re.S)
    q = re.sub(r"--[^\n]*", " ", q)
    q = re.sub(r"\s+", " ", q).strip().rstrip(";").strip().lower()
    return q


def load_statements(path: str) -> Dict[str, Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read().strip()
    if text.startswith("[") or text.startswith("{"):
        obj = json.loads(text)
        rows = obj.get("statements", []) if isinstance(obj, dict) else obj
    else:
        rows = list(csv.DictReader(io.StringIO(text, newline='')))
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        key = normalize_query(r.get("query", ""))
        if not key:
            continue
        a = agg.setdefault(key, {"query": r.get("query", ""), "calls": 0.0, "total": 0.0, "rows": 0.0,
                                 "hit": 0.0, "read": 0.0, "temp": 0.0, "queryids": set()})
        a["calls"] += _f(r, "calls")
        a["total"] += _f(r, *ALIASES["total_exec_time"])
        a["rows"] += _f(r, "rows")
        a["hit"] += _f(r, "shared_blks_hit")
        a["read"] += _f(r, "shared_blks_read")
        a["temp"] += _f(r, "temp_blks_written")
        if r.get("queryid") not in (None, ""):
            a["queryids"].add(str(r.get("queryid")))
    for a in agg.values():
        a["mean"] = a["total"] / a["calls"] if a["calls"] else 0.0
        a["queryids"] = sorted(a["queryids"])
    return agg


def compare_statements(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]], min_calls: int = 1,
                       min_extra_ms: float = 1000.0, top: int = 30) -> Tuple[List[Finding], List[Dict[str, Any]]]:
    rows = []
    for key, n in new.items():
        o = old.get(key)
        if not o or o["calls"] < min_calls or n["calls"] < min_calls:
            continue
        ratio = n["mean"] / o["mean"] if o["mean"] > 0 else float("inf")
        extra = (n["mean"] - o["mean"]) * n["calls"]
        rows.append({"query": n["query"], "calls_old": o["calls"], "calls_new": n["calls"], "mean_old": o["mean"],
                     "mean_new": n["mean"], "ratio": ratio, "extra_ms": extra,
                     "temp_old": o["temp"] / max(o["calls"], 1), "temp_new": n["temp"] / max(n["calls"], 1),
                     "read_old": o["read"] / max(o["calls"], 1), "read_new": n["read"] / max(n["calls"], 1)})
    rows.sort(key=lambda r: -r["extra_ms"])
    F: List[Finding] = []
    for r in rows:
        if r["ratio"] < 2 or r["extra_ms"] < min_extra_ms:
            continue
        sev = "low"
        if r["ratio"] >= 3:
            sev = "medium"
        if r["ratio"] >= 10:
            sev = "high"
        if r["ratio"] >= 30:
            sev = "critical"
        q = re.sub(r"\s+", " ", r["query"]).strip()
        F.append(Finding("PGSS-REGRESSION", sev, "statements", q[:80],
                         f"{r['ratio']:.1f}× chậm hơn: mean {fmt_ms(r['mean_old'])} → {fmt_ms(r['mean_new'])}, "
                         f"{fmt_num(r['calls_new'])} calls, thêm {fmt_ms(r['extra_ms'])} tổng",
                         [q[:600]], score=r["extra_ms"]))
        if len([f for f in F if f.rule == "PGSS-REGRESSION"]) >= top:
            break
    for r in rows:
        q = re.sub(r"\s+", " ", r["query"]).strip()
        if r["temp_new"] >= 1000 and r["temp_new"] >= 4 * max(r["temp_old"], 1):
            F.append(Finding("PGSS-TEMP", "medium", "statements", q[:80],
                             f"temp_blks_written/call {fmt_num(r['temp_old'])} → {fmt_num(r['temp_new'])}", [q[:600]],
                             score=r["temp_new"]))
        if r["read_new"] >= 10000 and r["read_new"] >= 10 * max(r["read_old"], 1):
            F.append(Finding("PGSS-IO", "low", "statements", q[:80],
                             f"shared_blks_read/call {fmt_num(r['read_old'])} → {fmt_num(r['read_new'])}", [q[:600]],
                             score=r["read_new"]))
    unmatched = len([k for k in new if k not in old])
    if unmatched:
        F.append(Finding("PGSS-REGRESSION", "info", "statements", "unmatched",
                         f"{unmatched} query ở bản mới không ghép được với bản cũ theo text (đã chuẩn hoá) – có thể do "
                         "thay đổi cách normalise giữa các phiên bản hoặc workload khác"))
    return F, rows[:200]
