"""Command line interface.

    python -m pgdoctor analyze --old-plan q1.pg13.json --new-plan q1.pg17.json -o report.html
    python -m pgdoctor analyze --plans-dir plans/ --old-dir out/pg13 --new-dir out/pg17 -o report.html
    python -m pgdoctor remedy --plan q1.pg17.json --app-role batch_user --app-db appdb -o fix.sql
    python -m pgdoctor run --old-dsn "host=pg13 dbname=app user=batch" --new-dsn "host=pg17 …" --query-file q.sql
    python -m pgdoctor capture --dsn "…" --query-file q.sql --out-dir out/pg17   (+ qdiff --old-dir … --new-dir …)
    python -m pgdoctor kb
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

from . import __version__
from .compare import compare_plans
from .findings import SEV_RANK, SEVERITIES, Finding, fmt_ms, sort_findings
from .health import analyze_health, compare_health, load_health
from .kb import RULES
from .plan import Plan, load_plan, major_from_label
from .querycfg import config_findings, query_config_diff
from .remedy import RemedyContext, remedies_for_query, render_runbook
from .report import render
from .rules_plan import analyze_plan
from .runner import capture, load_capture, major_of, read_query
from .settings import compare_settings, load_settings, pretty
from .statements import compare_statements, load_statements

PLAN_EXT = (".json", ".txt", ".out", ".log", ".plan")
LABEL_RE = re.compile(r"^(?P<stem>.+?)[._-](?P<label>old|new|before|after|pg\d+|v?\d+)$", re.I)


def _label_rank(label: str) -> Tuple[int, int]:
    l = label.lower()
    if l in ("old", "before"):
        return (0, 0)
    if l in ("new", "after"):
        return (2, 0)
    m = re.search(r"\d+", l)
    return (1, int(m.group()) if m else 0)


def discover_plans(d: str) -> List[Tuple[str, Optional[str], Optional[str]]]:
    groups: Dict[str, Dict[str, str]] = {}
    for fn in sorted(os.listdir(d)):
        path = os.path.join(d, fn)
        if not os.path.isfile(path):
            continue
        base, ext = os.path.splitext(fn)
        if ext.lower() not in PLAN_EXT:
            continue
        m = LABEL_RE.match(base)
        if not m:
            continue
        groups.setdefault(m.group("stem"), {})[m.group("label")] = path
    out = []
    for stem, files in sorted(groups.items()):
        labels = sorted(files, key=_label_rank)
        if len(labels) == 1:
            only = labels[0]
            is_old = _label_rank(only)[0] == 0
            out.append((stem, files[only] if is_old else None, None if is_old else files[only]))
        else:
            out.append((stem, files[labels[0]], files[labels[-1]]))
    return out


def _maybe(path: Optional[str], name: str) -> Optional[str]:
    if not path:
        return None
    p = os.path.join(path, name)
    return p if os.path.isfile(p) else None


def run_analyze(args) -> int:
    findings: List[Finding] = []
    queries = []
    inputs: List[str] = []

    pairs: List[Tuple[str, Optional[str], Optional[str]]] = []
    if args.old_plan or args.new_plan:
        stem = os.path.splitext(os.path.basename(args.new_plan or args.old_plan))[0]
        m = LABEL_RE.match(stem)
        name = args.name or (m.group("stem") if m else stem)
        pairs.append((name, args.old_plan, args.new_plan))
    if args.plans_dir:
        pairs.extend(discover_plans(args.plans_dir))

    for i, (name, po, pn) in enumerate(pairs):
        old = new = None
        try:
            if po:
                old = load_plan(po); inputs.append(po)
                old.major_hint = old.major_hint or major_from_label(args.old_label)
            if pn:
                new = load_plan(pn); inputs.append(pn)
                new.major_hint = new.major_hint or major_from_label(args.new_label)
        except Exception as ex:  # keep going with other queries
            findings.append(Finding("CMP-DIVERGE", "info", "compare", name, f"Không đọc được plan: {ex}"))
            continue
        q = _query_entry(i, name, old, new, args)
        queries.append(q)
        findings += q["findings"]

    settings_table = None
    so = args.old_settings or _maybe(args.old_dir, "settings.json")
    sn = args.new_settings or _maybe(args.new_dir, "settings.json")
    if so and sn:
        f, settings_table = compare_settings(load_settings(so), load_settings(sn))
        findings += f; inputs += [so, sn]

    ho = args.old_health or _maybe(args.old_dir, "health.json")
    hn = args.new_health or _maybe(args.new_dir, "health.json")
    findings += _health_findings(ho, hn, inputs)

    statements_rows = None
    qo = args.old_statements or _maybe(args.old_dir, "statements.json")
    qn = args.new_statements or _maybe(args.new_dir, "statements.json")
    if qo and qn:
        f, statements_rows = compare_statements(load_statements(qo), load_statements(qn), min_calls=args.min_calls)
        findings += f; inputs += [qo, qn]

    if not inputs:
        print("Không có input. Xem: python -m pgdoctor analyze --help", file=sys.stderr)
        return 1

    findings = sort_findings(findings)
    ctx = {"queries": queries, "findings": findings, "settings_table": settings_table,
           "statements_rows": statements_rows, "labels": (args.old_label, args.new_label), "inputs": inputs}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(render(ctx))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([f.to_dict() for f in findings], fh, ensure_ascii=False, indent=2)
    if args.remedies:
        with open(args.remedies, "w", encoding="utf-8") as fh:
            fh.write(render_runbook([{"name": q["name"], "remedies": q["remedies"]} for q in queries], _remedy_ctx(args)))
    _print_summary(findings, queries, args)
    if args.fail_on:
        lim = SEV_RANK[args.fail_on]
        if any(SEV_RANK[f.severity] <= lim for f in findings):
            return 2
    return 0


def _query_entry(i: int, name: str, old: Optional[Plan], new: Optional[Plan], args) -> Dict:
    qf: List[Finding] = []
    if old:
        qf += analyze_plan(old, "old", name)
    if new:
        qf += analyze_plan(new, "new", name)
    cmp_ = None
    if old and new:
        cmp_ = compare_plans(old, new, name)
        qf += cmp_.findings
    # When both plans exist, problems that already existed on the OLD plan are context, not the regression:
    # cap them at "low" so the report leads with what changed.
    if old and new:
        for f in qf:
            if f.side == "old" and f.scope == "plan" and SEV_RANK[f.severity] < SEV_RANK["low"]:
                f.severity = "low"
    rem = remedies_for_query(name, old, new, qf, _remedy_ctx(args)) if new else []
    return {"id": str(i), "name": name, "old": old, "new": new, "cmp": cmp_, "findings": qf, "remedies": rem}


def _health_findings(ho: Optional[str], hn: Optional[str], inputs: List[str]) -> List[Finding]:
    out: List[Finding] = []
    h_old = load_health(ho) if ho else None
    h_new = load_health(hn) if hn else None
    if h_old:
        out += [_demote(f) for f in analyze_health(h_old, "old")]; inputs.append(ho)
    if h_new:
        out += analyze_health(h_new, "new"); inputs.append(hn)
    if h_old and h_new:
        out += compare_health(h_old, h_new)
    return out


def _demote(f: Finding) -> Finding:
    if f.rule == "HEALTH-SEARCHPATH-FUNC":
        return f  # pre-upgrade risk: keep its severity
    if f.severity not in ("info",):
        f.severity = SEVERITIES[min(SEV_RANK[f.severity] + 1, len(SEVERITIES) - 1)]
    return f


def _print_summary(findings: List[Finding], queries, args) -> None:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    print(f"pgdoctor {__version__}: " + ", ".join(f"{s}={counts[s]}" for s in SEVERITIES))
    for q in queries:
        o, n = q["old"], q["new"]
        print(f"  [{q['name']}] {args.old_label}: {fmt_ms(o.execution_time) if o else '-'}  "
              f"{args.new_label}: {fmt_ms(n.execution_time) if n else '-'}")
    for f in findings[: args.top]:
        side = {"old": args.old_label, "new": args.new_label, "both": "cmp"}.get(f.side or "", "")
        print(f"  - {f.severity.upper():8} {f.rule:24} {f.subject[:24]:24} {side:5} {f.message}")
    for q in queries:
        eff = [r for r in q.get("remedies", []) if r.effective]
        if eff:
            print(f"  Khắc phục [{q['name']}] (thứ tự ưu tiên):")
            for r in eff[:4]:
                print(f"    {r.rank}. {'[sửa code] ' if r.code_change else '[không sửa code] '}{r.title} – phạm vi: {r.blast}")
    if args.output:
        print(f"Report: {os.path.abspath(args.output)}")
    if getattr(args, "remedies", None):
        print(f"Runbook SQL: {os.path.abspath(args.remedies)}")


def _remedy_ctx(args) -> RemedyContext:
    return RemedyContext(app_role=args.app_role, app_db=args.app_db, app_name=args.app_name, target=args.target,
                         instance=args.instance, keyword=args.keyword)


def _add_remedy_args(p) -> None:
    g = p.add_argument_group("khắc phục (remedy)")
    g.add_argument("--app-role", default="<app_role>", help="Role mà ứng dụng/job batch đăng nhập")
    g.add_argument("--app-db", default="<db>", help="Database của ứng dụng")
    g.add_argument("--app-name", default="", help="application_name của job (để hint chỉ áp dụng cho job đó)")
    g.add_argument("--target", choices=("cloudsql", "selfhosted"), default="cloudsql")
    g.add_argument("--instance", default="<INSTANCE>", help="Tên instance Cloud SQL (cho lệnh gcloud)")
    g.add_argument("--keyword", default="", help="Đoạn text để tìm câu lệnh trong pg_stat_statements")


def run_remedy(args) -> int:
    """Offline: plan(s) of one slow statement -> SQL runbook of fixes, no-code-change options first."""
    new = load_plan(args.plan)
    new.major_hint = new.major_hint or major_from_label(args.new_label)
    old = None
    if args.old_plan:
        old = load_plan(args.old_plan)
        old.major_hint = old.major_hint or major_from_label(args.old_label)
    name = args.name or os.path.basename(args.plan)
    fs = analyze_plan(new, "new", name)
    if old:
        fs += compare_plans(old, new, name).findings
    if args.work_mem or args.hash_mem_multiplier:
        sett = dict(new.settings)
        if args.work_mem:
            sett["work_mem"] = args.work_mem
        if args.hash_mem_multiplier:
            sett["hash_mem_multiplier"] = str(args.hash_mem_multiplier)
        new.meta["Settings"] = sett
    rem = remedies_for_query(name, old, new, fs, _remedy_ctx(args))
    text = render_runbook([{"name": name, "remedies": rem}], _remedy_ctx(args))
    if not rem:
        text += "-- Không có finding nào ở mức medium trở lên trên plan này cần khắc phục.\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Runbook SQL: {os.path.abspath(args.output)}")
        for r in rem:
            if r.effective:
                print(f"  {r.rank}. {'[sửa code] ' if r.code_change else '[không sửa code] '}{r.title} – phạm vi: {r.blast}")
    else:
        sys.stdout.write(text)
    return 0


# ------------------------------------------------------------------ live mode: capture / qdiff / run
def _capture_one(args, dsn: str, psql_cmd: str, analyze: bool, out_dir: str, label: str) -> Dict:
    return capture(dsn, read_query(args.query_file), out_dir, psql_cmd=psql_cmd, analyze=analyze,
                   timeout=(args.timeout if analyze and args.timeout not in (None, "", "0") else None),
                   params=args.param or (), generic_plan=args.generic_plan, app_name=args.app_name or None,
                   client_options=args.client_options, label=label)


def run_capture(args) -> int:
    """One server: EXPLAIN (+ optional ANALYZE) of the statement and the configuration of that very session."""
    try:
        meta = _capture_one(args, args.dsn, args.psql, args.analyze, args.out_dir, args.label or "")
    except (RuntimeError, ValueError) as ex:
        print(f"Lỗi: {ex}", file=sys.stderr)
        return 1
    _print_capture(meta, args.out_dir)
    return 0


def _print_capture(meta: Dict, out_dir: str) -> None:
    print(f"[{meta.get('label') or 'PG' + str(major_of(meta))}] {str(meta.get('server_version', '')).split(' on ')[0]} · "
          f"user={meta.get('current_user')} db={meta.get('database')} application_name={meta.get('application_name')}"
          f" · {'EXPLAIN ANALYZE' if os.path.isfile(os.path.join(out_dir, 'plan_analyze.json')) else 'EXPLAIN (không ANALYZE)'}"
          f"{' · ANALYZE lỗi: ' + meta['analyze_error'] if meta.get('analyze_error') else ''} → {os.path.abspath(out_dir)}")


def run_run(args) -> int:
    """Both servers in parallel, then the per-statement comparison (same as qdiff)."""
    from concurrent.futures import ThreadPoolExecutor
    od, nd = os.path.join(args.out_dir, "old"), os.path.join(args.out_dir, "new")
    an_old, an_new = args.analyze in ("old", "both"), args.analyze in ("new", "both")
    if args.analyze != "none":
        print("Lưu ý: EXPLAIN ANALYZE chạy thật câu lệnh trong BEGIN … ROLLBACK – dữ liệu không đổi nhưng câu UPDATE vẫn "
              "giữ row lock trong lúc chạy. Giới hạn bởi statement_timeout = " + (args.timeout or "không giới hạn") + ".",
              file=sys.stderr)
    with ThreadPoolExecutor(max_workers=2) as ex:
        fo = ex.submit(_capture_one, args, args.old_dsn, args.old_psql or args.psql, an_old, od, args.old_label or "old")
        fn = ex.submit(_capture_one, args, args.new_dsn, args.new_psql or args.psql, an_new, nd, args.new_label or "new")
        try:
            mo, mn = fo.result(), fn.result()
        except (RuntimeError, ValueError) as e:
            print(f"Lỗi: {e}", file=sys.stderr)
            return 1
    _print_capture(mo, od)
    _print_capture(mn, nd)
    args.old_dir, args.new_dir = od, nd
    if not args.output:
        args.output = os.path.join(args.out_dir, "report.html")
    if not args.remedies:
        args.remedies = os.path.join(args.out_dir, "remedies.sql")
    if not args.json:
        args.json = os.path.join(args.out_dir, "findings.json")
    return run_qdiff(args)


def run_qdiff(args) -> int:
    """Offline: two directories written by 'capture' (or 'run') -> plan + effective-config + stats comparison."""
    try:
        co, cn = load_capture(args.old_dir), load_capture(args.new_dir)
    except (ValueError, OSError) as ex:
        print(f"Lỗi: {ex}", file=sys.stderr)
        return 1
    mo_, mn_ = major_of(co["meta"]), major_of(cn["meta"])
    lo = args.old_label if args.old_label not in (None, "", "old") else f"PG{mo_}"
    ln = args.new_label if args.new_label not in (None, "", "new") else f"PG{mn_}"
    if lo == ln:
        lo, ln = f"{lo} ({os.path.basename(os.path.normpath(args.old_dir))})", f"{ln} ({os.path.basename(os.path.normpath(args.new_dir))})"
    old, new = load_plan(co["plan_path"], mo_), load_plan(cn["plan_path"], mn_)
    so, sn = load_settings(co["settings_path"]), load_settings(cn["settings_path"])
    explain_settings = {lo: dict(old.settings), ln: dict(new.settings)}
    for p, sett in ((old, so), (new, sn)):        # rules / remedies must use what the session really had
        s = dict(p.settings)
        if "work_mem" in sett:
            s["work_mem"] = pretty(sett["work_mem"])
        if "hash_mem_multiplier" in sett:
            s["hash_mem_multiplier"] = sett["hash_mem_multiplier"].get("setting", "")
        p.meta["Settings"] = s
    # remedy defaults straight from the captured session of the NEW server
    if args.app_role in (None, "", "<app_role>"):
        args.app_role = cn["meta"].get("current_user") or "<app_role>"
    if args.app_db in (None, "", "<db>"):
        args.app_db = cn["meta"].get("database") or "<db>"
    if not args.app_name and cn["meta"].get("application_name") not in (None, "", "psql", "pgdoctor"):
        args.app_name = cn["meta"]["application_name"]
    name = args.name or "query"
    inputs = [co["plan_path"], cn["plan_path"], co["settings_path"], cn["settings_path"]]
    q = _query_entry(0, name, old, new, args)
    findings = list(q["findings"]) + _health_findings(co["health_path"], cn["health_path"], inputs)
    diff = query_config_diff(so, sn, old, new, findings, (lo, ln), prepared=bool(cn["meta"].get("params")),
                             explain_settings=explain_settings)
    findings += config_findings(diff, (lo, ln))
    findings = sort_findings(findings)
    captures = {lo: co, ln: cn}
    ctx = {"queries": [q], "findings": findings, "settings_table": None, "statements_rows": None,
           "labels": (lo, ln), "inputs": inputs, "query_config": diff, "captures": captures}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(render(ctx))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"labels": [lo, ln], "findings": [f.to_dict() for f in findings],
                       "query_config": {k: diff[k] for k in ("rows", "key", "thresholds", "verdict", "explain_settings")}},
                      fh, ensure_ascii=False, indent=2, default=str)
    if args.remedies:
        with open(args.remedies, "w", encoding="utf-8") as fh:
            fh.write(render_runbook([{"name": name, "remedies": q["remedies"]}], _remedy_ctx(args)))
    args.old_label, args.new_label = lo, ln
    _print_query_config(diff, captures, (lo, ln))
    _print_summary(findings, [q], args)
    return 0


def _print_query_config(diff: Dict, captures: Dict, labels: Tuple[str, str]) -> None:
    lo, ln = labels
    print(f"\n== Cấu hình hiệu lực khi chạy câu này: {lo} → {ln}")
    for lbl in labels:
        m = captures[lbl]["meta"]
        print(f"  {lbl}: {str(m.get('server_version', '')).split(' on ')[0]} · user={m.get('current_user')} "
              f"db={m.get('database')} app={m.get('application_name')} · "
              f"{'EXPLAIN ANALYZE' if captures[lbl]['analyzed'] else 'EXPLAIN (không ANALYZE)'}")
    print("  Kết luận:")
    for v in diff["verdict"]:
        print(f"    - {v}")
    if diff["thresholds"]:
        print("  Quyết định hash của SubPlan (tính bằng giá trị thật của session):")
        for t in diff["thresholds"]:
            print(f"    - {t['text']}")
    if diff["key"]:
        print("  Tham số then chốt (in cả khi giống nhau):")
        for k in diff["key"]:
            print(f"    {k['name']:28} {k['old'] + ' (' + k['old_src'] + ')':30} {k['new'] + ' (' + k['new_src'] + ')':30} "
                  f"{'giống' if k['same'] else 'KHÁC'}  ← {', '.join(k['rules'])}")
    rel = [r for r in diff["rows"] if r["relevance"] != "other"]
    c = diff["counts"]
    print(f"  Khác biệt: {c['critical']} then chốt · {c['plan']} liên quan node trong plan · {c['planner']} planner chung"
          f" · {c['other']} khác (chỉ trong report)")
    for r in rel[:25]:
        flag = {"changed": "", "only_new": f"chỉ có ở {ln}", "only_old": f"chỉ có ở {lo}"}[r["kind"]]
        print(f"    [{r['relevance']:8}] {r['name']:32} {r['old'] + (' (' + r['old_src'] + ')' if r['old_src'] else ''):30} → "
              f"{r['new'] + (' (' + r['new_src'] + ')' if r['new_src'] else ''):30} {flag}"
              f"{' (mặc định đổi theo phiên bản)' if r['default_changed'] else ''}")
    es = diff["explain_settings"]
    print(f"  EXPLAIN (SETTINGS) – chỉ in tham số khác mặc định: {lo}: {es.get(lo) or '{}'} · {ln}: {es.get(ln) or '{}'}")


def _add_live_args(p, both: bool) -> None:
    g = p.add_argument_group("chạy câu lệnh")
    g.add_argument("--query-file", required=True, help="File chứa đúng 1 câu lệnh (nguyên văn như ứng dụng gửi, không có EXPLAIN)")
    if both:
        g.add_argument("--analyze", choices=("none", "old", "new", "both"), default="none",
                       help="Chạy EXPLAIN ANALYZE ở bên nào (mặc định none: chỉ EXPLAIN, ra kết quả ngay). "
                            "Gợi ý: 'old' khi bản mới chạy hàng giờ")
    else:
        g.add_argument("--analyze", action="store_true", help="Chạy thêm EXPLAIN ANALYZE (trong BEGIN … ROLLBACK)")
    g.add_argument("--timeout", default="30min", help="statement_timeout cho EXPLAIN ANALYZE (mặc định 30min; 0 = không giới hạn)")
    g.add_argument("--param", action="append", help="Giá trị cho $1, $2… (lặp lại theo thứ tự) – chạy qua PREPARE/EXECUTE")
    g.add_argument("--generic-plan", action="store_true",
                   help="Ép generic plan (plan_cache_mode=force_generic_plan) như JDBC sau 5 lần thực thi; cần --param")
    g.add_argument("--client-options", help="Tham số kết nối giống ứng dụng, vd. \"-c work_mem=64MB\" (JDBC 'options')")
    g.add_argument("--psql", default="psql", help="Lệnh psql (vd. \"docker exec -i rpg17 psql\")")


def run_kb(_args) -> int:
    for r in RULES.values():
        print(f"{r.id:26} [{r.category}] {r.title}" + (f" (PG{r.since}+)" if r.since else ""))
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(prog="pgdoctor", description="Offline detector cho regression khi nâng cấp PostgreSQL (13 → 17).")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="Phân tích plan / settings / health / pg_stat_statements")
    a.add_argument("--old-plan"), a.add_argument("--new-plan"), a.add_argument("--name")
    a.add_argument("--plans-dir", help="Thư mục chứa cặp file <tên>.old|new|pg13|pg17.json|txt")
    a.add_argument("--old-dir", help="Thư mục output của collector trên bản cũ (settings.json, health.json, statements.json)")
    a.add_argument("--new-dir", help="Thư mục output của collector trên bản mới")
    a.add_argument("--old-settings"), a.add_argument("--new-settings")
    a.add_argument("--old-health"), a.add_argument("--new-health")
    a.add_argument("--old-statements"), a.add_argument("--new-statements")
    a.add_argument("--old-label", default="PG13"), a.add_argument("--new-label", default="PG17")
    a.add_argument("--min-calls", type=int, default=1)
    a.add_argument("-o", "--output", default="pgdoctor-report.html")
    a.add_argument("--json", help="Ghi findings ra file JSON")
    a.add_argument("--top", type=int, default=15, help="Số finding in ra console")
    a.add_argument("--fail-on", choices=SEVERITIES, help="Exit code 2 nếu có finding từ mức này trở lên (dùng cho CI)")
    a.add_argument("--remedies", help="Ghi runbook SQL các phương án khắc phục (ưu tiên không sửa code) ra file")
    _add_remedy_args(a)
    a.set_defaults(func=run_analyze)
    r = sub.add_parser("remedy", help="Sinh runbook SQL khắc phục cho 1 câu lệnh (ưu tiên không sửa code)")
    r.add_argument("--plan", required=True, help="Plan trên bản MỚI (EXPLAIN là đủ, không cần ANALYZE)")
    r.add_argument("--old-plan", help="Plan trên bản cũ (tuỳ chọn, để so sánh)")
    r.add_argument("--name")
    r.add_argument("--old-label", default="PG13"), r.add_argument("--new-label", default="PG17")
    r.add_argument("--work-mem", help="work_mem thực tế của session ứng dụng nếu plan không có EXPLAIN SETTINGS (vd. 4MB)")
    r.add_argument("--hash-mem-multiplier", type=float, help="hash_mem_multiplier thực tế (Cloud SQL mặc định 2)")
    r.add_argument("-o", "--output", help="Ghi runbook ra file (mặc định in ra stdout)")
    _add_remedy_args(r)
    r.set_defaults(func=run_remedy)
    c = sub.add_parser("capture", help="Kết nối 1 server: EXPLAIN câu lệnh + cấu hình hiệu lực của chính session đó")
    c.add_argument("--dsn", default="", help="libpq conninfo hoặc URI (đăng nhập BẰNG role của ứng dụng)")
    c.add_argument("--out-dir", required=True, help="Thư mục output (vd. out/pg17)")
    c.add_argument("--label", help="Nhãn hiển thị (mặc định PG<major>)")
    _add_live_args(c, both=False)
    c.add_argument("--app-name", default="", help="application_name khi kết nối (giống job của ứng dụng)")
    c.set_defaults(func=run_capture)
    qd = sub.add_parser("qdiff", help="So sánh 2 thư mục của 'capture': plan + cấu hình hiệu lực + thống kê + khắc phục")
    qd.add_argument("--old-dir", required=True), qd.add_argument("--new-dir", required=True)
    qd.add_argument("--old-label", default=""), qd.add_argument("--new-label", default="")
    qd.add_argument("--name")
    qd.add_argument("-o", "--output", default="pgdoctor-query.html")
    qd.add_argument("--json"), qd.add_argument("--remedies")
    qd.add_argument("--top", type=int, default=12)
    _add_remedy_args(qd)
    qd.set_defaults(func=run_qdiff)
    rn = sub.add_parser("run", help="Chạy câu lệnh trên CẢ HAI server rồi liệt kê khác biệt cấu hình/plan/thống kê")
    rn.add_argument("--old-dsn", required=True, help="Server cũ (PG13 / bản restore trước upgrade)")
    rn.add_argument("--new-dsn", required=True, help="Server mới (PG17)")
    rn.add_argument("--old-psql"), rn.add_argument("--new-psql")
    rn.add_argument("--old-label", default=""), rn.add_argument("--new-label", default="")
    rn.add_argument("--out-dir", default="pgdoctor-run", help="Thư mục output (old/, new/, report.html, remedies.sql)")
    rn.add_argument("--name")
    rn.add_argument("-o", "--output"), rn.add_argument("--json"), rn.add_argument("--remedies")
    rn.add_argument("--top", type=int, default=12)
    _add_live_args(rn, both=True)
    _add_remedy_args(rn)
    rn.set_defaults(func=run_run)
    k = sub.add_parser("kb", help="Liệt kê các rule/pattern")
    k.set_defaults(func=run_kb)
    args = ap.parse_args(argv)
    return args.func(args)
