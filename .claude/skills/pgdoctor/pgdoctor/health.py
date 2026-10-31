"""Catalog / statistics health snapshot analysis (output of collect/pgdoctor_collect.sql)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .findings import Finding, fmt_num

SAFE_LANGS = {"c", "internal"}


def load_health(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.loads(fh.read())


def _side_label(side: str) -> str:
    return "cũ" if side == "old" else "mới"


def analyze_health(h: Dict[str, Any], side: str) -> List[Finding]:
    F: List[Finding] = []
    lbl = _side_label(side)
    tables = h.get("tables") or []

    # never analyzed / no stats
    bad = [t for t in tables if t.get("relkind") in ("r", "m") and not t.get("relispartition_parent")
           and ((t.get("reltuples") is not None and float(t["reltuples"]) < 0) or not t.get("has_stats"))
           and (t.get("size_bytes") or 0) > 0]
    bad = [t for t in bad if (t.get("n_live_tup") or 0) > 0 or float(t.get("reltuples") or 0) != 0 or (t.get("size_bytes") or 0) > 65536]
    if bad:
        bad.sort(key=lambda t: -(t.get("size_bytes") or 0))
        big = sum(1 for t in bad if (t.get("size_bytes") or 0) >= 100 * 1024 * 1024)
        mid = sum(1 for t in bad if (t.get("size_bytes") or 0) >= 10 * 1024 * 1024)
        F.append(Finding("HEALTH-NEVER-ANALYZED", "critical" if big else ("high" if mid else "medium"), "health", f"{len(bad)} bảng",
                         f"Bản {lbl}: {len(bad)} bảng chưa có thống kê (reltuples<0 hoặc không có pg_stats), {big} bảng ≥100MB",
                         [f"{t['schema']}.{t['name']}: reltuples={t.get('reltuples')}, size={_mb(t.get('size_bytes'))}, "
                          f"last_analyze={t.get('last_analyze') or '-'}, last_autoanalyze={t.get('last_autoanalyze') or '-'}"
                          for t in bad[:25]], side=side, score=len(bad)))

    # partitioned parents
    parents = [t for t in tables if t.get("relkind") == "p" and not t.get("has_stats")]
    if parents:
        F.append(Finding("HEALTH-PARTITION-PARENT", "medium", "health", f"{len(parents)} bảng partitioned",
                         f"Bản {lbl}: {len(parents)} bảng partitioned cha chưa có stats (autovacuum không ANALYZE bảng cha)",
                         [f"{t['schema']}.{t['name']}" for t in parents[:25]], side=side))

    # stale / dead
    stale, dead = [], []
    for t in tables:
        lt = float(t.get("n_live_tup") or 0) or max(float(t.get("reltuples") or 0), 0)
        if lt >= 10000 and (t.get("n_mod_since_analyze") or 0) >= 0.2 * lt:
            stale.append(t)
        if lt >= 10000 and (t.get("n_dead_tup") or 0) >= 0.2 * lt:
            dead.append(t)
    if stale:
        F.append(Finding("HEALTH-STALE", "low", "health", f"{len(stale)} bảng",
                         f"Bản {lbl}: {len(stale)} bảng thay đổi ≥20% kể từ lần ANALYZE cuối",
                         [f"{t['schema']}.{t['name']}: n_mod_since_analyze={fmt_num(t.get('n_mod_since_analyze'))}, n_live_tup={fmt_num(t.get('n_live_tup'))}"
                          for t in stale[:20]], side=side))
    if dead:
        F.append(Finding("HEALTH-DEAD", "low", "health", f"{len(dead)} bảng",
                         f"Bản {lbl}: {len(dead)} bảng có dead tuple ≥20%",
                         [f"{t['schema']}.{t['name']}: n_dead_tup={fmt_num(t.get('n_dead_tup'))}, n_live_tup={fmt_num(t.get('n_live_tup'))}"
                          for t in dead[:20]], side=side))

    # extended stats without data
    nodata = [s for s in (h.get("ext_stats") or []) if not s.get("has_data")]
    if nodata:
        F.append(Finding("HEALTH-EXTSTATS-NODATA", "high", "health", f"{len(nodata)} extended stats",
                         f"Bản {lbl}: {len(nodata)} extended statistics chưa có dữ liệu",
                         [f"{s.get('schema')}.{s.get('name')} on {s.get('table')} ({s.get('kinds')})" for s in nodata[:25]], side=side))

    # functions used by expression indexes / matviews without pinned search_path (PG17 maintenance search_path)
    risky = [f for f in (h.get("index_functions") or [])
             if (f.get("lang") or "").lower() not in SAFE_LANGS and not _has_search_path(f.get("proconfig"))]
    if risky:
        ver = int(h.get("server_version_num") or 0)
        nostats = {f"{t['schema']}.{t['name']}" for t in tables if not t.get("has_stats") or float(t.get("reltuples") or 0) < 0}
        nostats |= {t["name"] for t in tables if not t.get("has_stats") or float(t.get("reltuples") or 0) < 0}
        ext_nodata = {s.get("table") for s in (h.get("ext_stats") or []) if not s.get("has_data")}
        ev, suspects = [], []
        for f in risky[:25]:
            tbl = f.get("table") or ""
            line = f"{f.get('kind')}: {f.get('object')} → {f.get('func')} [{f.get('lang')}]" + (f" trên bảng {tbl}" if tbl else "")
            if f.get("is_extension"):
                line += " (thuộc extension)"
            if tbl and (tbl in nostats or tbl in ext_nodata):
                line += "  ⚠ bảng này đang thiếu stats/extended stats → nhiều khả năng ANALYZE đã LỖI"
                suspects.append(tbl)
            ev.append(line)
        if ver >= 170000:
            sev = "critical" if suspects else "high"
            msg = (f"Bản {lbl}: {len(risky)} hàm dùng trong expression index/matview không cố định search_path – PG17 chạy "
                   f"ANALYZE/VACUUM/REINDEX/REFRESH với search_path an toàn nên có thể lỗi")
            if suspects:
                msg += f"; {len(set(suspects))} bảng liên quan đang thiếu thống kê"
        else:
            sev = "medium"
            msg = (f"Bản {lbl} (trước upgrade): {len(risky)} hàm dùng trong expression index/matview không cố định search_path – "
                   f"SAU KHI lên PG17, ANALYZE/VACUUM/REINDEX trên các bảng này có thể lỗi")
        F.append(Finding("HEALTH-SEARCHPATH-FUNC", sev, "health", f"{len(risky)} hàm", msg, ev, side=side))

    # extensions
    outdated = [e for e in (h.get("extensions") or []) if e.get("default_version") and e.get("installed") != e.get("default_version")]
    if outdated:
        F.append(Finding("HEALTH-EXT-OUTDATED", "medium", "health", f"{len(outdated)} extension",
                         f"Bản {lbl}: {len(outdated)} extension chưa ALTER EXTENSION ... UPDATE",
                         [f"{e['name']}: {e.get('installed')} → {e.get('default_version')}" for e in outdated], side=side))

    inv = h.get("invalid_indexes") or []
    if inv:
        F.append(Finding("HEALTH-INVALID-INDEX", "high", "health", f"{len(inv)} index",
                         f"Bản {lbl}: {len(inv)} index không hợp lệ", [str(i.get("index") if isinstance(i, dict) else i) for i in inv[:25]],
                         side=side))

    coll = h.get("collation") or {}
    mism = []
    if coll.get("datcollversion") and coll.get("actual") and coll["datcollversion"] != coll["actual"]:
        mism.append(f"database: {coll['datcollversion']} → {coll['actual']}")
    for c in coll.get("mismatched") or []:
        mism.append(f"{c.get('collname')}: {c.get('collversion')} → {c.get('actual')}")
    if mism:
        F.append(Finding("HEALTH-COLLATION", "high", "health", "collation", f"Bản {lbl}: collation version không khớp", mism, side=side))
    return F


def compare_health(old: Dict[str, Any], new: Dict[str, Any]) -> List[Finding]:
    F: List[Finding] = []
    so = {(c["schemaname"], c["tablename"], c["attname"], bool(c.get("inherited"))): c for c in (old.get("column_stats") or [])}
    sn = {(c["schemaname"], c["tablename"], c["attname"], bool(c.get("inherited"))): c for c in (new.get("column_stats") or [])}
    tables_new = {(t["schema"], t["name"]) for t in (new.get("tables") or [])}
    missing = [k for k in so if k not in sn and (k[0], k[1]) in tables_new]
    if missing:
        by_table: Dict[str, int] = {}
        for k in missing:
            by_table[f"{k[0]}.{k[1]}"] = by_table.get(f"{k[0]}.{k[1]}", 0) + 1
        F.append(Finding("HEALTH-STATS-MISSING-COLS", "high", "health", f"{len(by_table)} bảng",
                         f"{len(missing)} cột có stats ở bản cũ nhưng không có ở bản mới ({len(by_table)} bảng)",
                         [f"{t}: {n} cột" for t, n in sorted(by_table.items(), key=lambda x: -x[1])[:25]], side="new",
                         score=len(missing)))
    shifts = []
    for k, c_new in sn.items():
        c_old = so.get(k)
        if not c_old:
            continue
        nd_o, nd_n = _abs_nd(c_old, old, k), _abs_nd(c_new, new, k)
        if nd_o is None or nd_n is None or max(nd_o, nd_n) < 50:
            continue
        r = max(nd_o, nd_n) / max(min(nd_o, nd_n), 1)
        if r >= 3:
            shifts.append((r, k, nd_o, nd_n, c_old, c_new))
    shifts.sort(key=lambda x: -x[0])
    if shifts:
        F.append(Finding("HEALTH-NDISTINCT-SHIFT", "medium", "health", f"{len(shifts)} cột",
                         f"{len(shifts)} cột có n_distinct lệch ≥3× giữa hai bản",
                         [f"{k[0]}.{k[1]}.{k[2]}{' (inherited)' if k[3] else ''}: {fmt_num(a)} → {fmt_num(b)} "
                          f"(raw {co.get('n_distinct')} → {cn.get('n_distinct')})" for r, k, a, b, co, cn in shifts[:25]],
                         side="both", score=len(shifts)))
    to_, tn_ = str(old.get("default_statistics_target") or ""), str(new.get("default_statistics_target") or "")
    targets_o = {(t["table"], t["column"]): t["target"] for t in (old.get("column_targets") or [])}
    targets_n = {(t["table"], t["column"]): t["target"] for t in (new.get("column_targets") or [])}
    diff_t = [f"{k[0]}.{k[1]}: {targets_o.get(k, 'default')} → {targets_n.get(k, 'default')}"
              for k in sorted(set(targets_o) | set(targets_n)) if targets_o.get(k) != targets_n.get(k)]
    if to_ and tn_ and to_ != tn_:
        diff_t.insert(0, f"default_statistics_target: {to_} → {tn_}")
    if diff_t:
        F.append(Finding("HEALTH-STATS-TARGET", "medium", "health", "statistics target", "Statistics target khác nhau giữa hai bản",
                         diff_t[:30], side="both"))
    return F


def _abs_nd(c: Dict[str, Any], snap: Dict[str, Any], key) -> Optional[float]:
    nd = c.get("n_distinct")
    if nd is None:
        return None
    nd = float(nd)
    if nd >= 0:
        return nd
    for t in snap.get("tables") or []:
        if t["schema"] == key[0] and t["name"] == key[1]:
            rt = float(t.get("reltuples") or 0)
            return -nd * rt if rt > 0 else None
    return None


def _has_search_path(proconfig) -> bool:
    if not proconfig:
        return False
    if isinstance(proconfig, str):
        return "search_path" in proconfig
    return any(str(x).startswith("search_path") for x in proconfig)


def _mb(b) -> str:
    if b is None:
        return "-"
    b = float(b)
    if b >= 1024 ** 3:
        return f"{b/1024**3:.1f}GB"
    return f"{b/1024**2:.1f}MB"
