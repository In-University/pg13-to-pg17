"""Command line interface.

    python -m pgdoctor analyze --old-plan q1.pg13.json --new-plan q1.pg17.json -o report.html
    python -m pgdoctor analyze --plans-dir plans/ --old-dir out/pg13 --new-dir out/pg17 -o report.html
    python -m pgdoctor remedy --plan q1.pg17.json --app-role batch_user --app-db appdb -o fix.sql
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
from .remedy import RemedyContext, remedies_for_query, render_runbook
from .report import render
from .rules_plan import analyze_plan
from .settings import compare_settings, load_settings
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
        qf: List[Finding] = []
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
        queries.append({"id": str(i), "name": name, "old": old, "new": new, "cmp": cmp_, "findings": qf, "remedies": rem})
        findings += qf

    settings_table = None
    so = args.old_settings or _maybe(args.old_dir, "settings.json")
    sn = args.new_settings or _maybe(args.new_dir, "settings.json")
    if so and sn:
        f, settings_table = compare_settings(load_settings(so), load_settings(sn))
        findings += f; inputs += [so, sn]

    ho = args.old_health or _maybe(args.old_dir, "health.json")
    hn = args.new_health or _maybe(args.new_dir, "health.json")
    h_old = load_health(ho) if ho else None
    h_new = load_health(hn) if hn else None
    if h_old:
        findings += [_demote(f) for f in analyze_health(h_old, "old")]; inputs.append(ho)
    if h_new:
        findings += analyze_health(h_new, "new"); inputs.append(hn)
    if h_old and h_new:
        findings += compare_health(h_old, h_new)

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
    k = sub.add_parser("kb", help="Liệt kê các rule/pattern")
    k.set_defaults(func=run_kb)
    args = ap.parse_args(argv)
    return args.func(args)
