"""planviz command line.

  planviz report PLAN [PLAN ...] [-o planviz.html] [--labels A,B] [--json analysis.json]
  planviz summary PLAN [PLAN ...]            console only (what an agent reads)
  planviz app [-o planviz.html] [--no-demo]  the empty tool page (paste / drop plans), with an example loaded
  planviz explain --dsn DSN --query-file q.sql [--analyze] [-o plan.json] [--html out.html]

PLAN: EXPLAIN output as JSON or text (psql copy, auto_explain log), a 'pgdoctor capture' directory, or '-' (stdin).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .analyze import (SEV_ORDER, BLOCK, analyze, fmt_bytes, fmt_factor, fmt_ms, fmt_num, load_rules, match,
                      render_insight, _conds)
from .loader import LoadedPlan, load
from .render import demo_plans, write_html

SEV_VI = {"critical": "Nghiêm trọng", "high": "Cao", "medium": "Trung bình", "low": "Thấp", "info": "Thông tin"}


def _load_all(paths: List[str], labels: Optional[str]) -> List[LoadedPlan]:
    lbls = [x.strip() for x in labels.split(",")] if labels else []
    out = []
    for i, p in enumerate(paths):
        out.append(load(p, lbls[i] if i < len(lbls) and lbls[i] else None))
    seen: Dict[str, int] = {}
    for lp in out:                      # keep labels unique (two files called plan.json, ...)
        if lp.label in seen:
            seen[lp.label] += 1
            lp.label = f"{lp.label} ({seen[lp.label]})"
        else:
            seen[lp.label] = 1
    return out


def _name(n: Dict[str, Any]) -> str:
    s = (n["label"] + (" " + n["object"] if n["object"] else "")).strip()
    return s + (f" [{n['sub']}]" if n["sub"] else "")


def summarize(plans: List[LoadedPlan], analyses: List[Dict[str, Any]], top: int = 5, out=None) -> None:
    rules = load_rules()
    w = (out or sys.stdout).write
    for lp, a in zip(plans, analyses):
        S = a["summary"]
        kind = "EXPLAIN ANALYZE" if S["analyzed"] else "EXPLAIN (không ANALYZE)"
        w(f"\n== {lp.label}  ({lp.source}) – {kind}, {S['nodes']} node\n")
        parts = []
        if S["execution_ms"] is not None:
            parts.append(f"thực thi {fmt_ms(S['execution_ms'])}")
        if S["planning_ms"] is not None:
            parts.append(f"lập kế hoạch {fmt_ms(S['planning_ms'])}")
        if S["shared_hit"] is not None or S["shared_read"] is not None:
            hit, rd = S["shared_hit"] or 0, S["shared_read"] or 0
            parts.append(f"buffers {fmt_bytes((hit + rd) * BLOCK)}" + (f" ({hit / (hit + rd) * 100:.0f}% cache)" if hit + rd else ""))
        if S["temp_blocks"]:
            parts.append(f"file tạm {fmt_bytes(S['temp_blocks'] * BLOCK)}")
        if S["trigger_ms"]:
            parts.append(f"trigger {fmt_ms(S['trigger_ms'])}")
        if S["query_id"] is not None:
            parts.append(f"query_id {S['query_id']}")
        if parts:
            w("   " + " · ".join(parts) + "\n")
        nodes = a["nodes"]
        R = S["runtime_ms"]
        timed = sorted((n for n in nodes if n["excl"] is not None), key=lambda n: -n["excl"])
        if timed and R:
            w("   Thời gian riêng lớn nhất:\n")
            for n in timed[:top]:
                if n["excl"] / R < 0.01 and n is not timed[0]:
                    break
                loops = f"  ×{fmt_num(n['loops'])} loop" if (n["loops"] or 0) > 1 else ""
                w(f"     {n['excl'] / R * 100:5.1f}%  {fmt_ms(n['excl']):>9}  #{n['id']} {_name(n)}{loops}\n")
        born = sorted((n for n in nodes if n["local_lq"] is not None and abs(n["local_lq"]) >= 1),
                      key=lambda n: -abs(n["local_lq"]))
        if born:
            w("   Sai số ước lượng sinh ra tại node (ước lượng → thực tế mỗi loop):\n")
            for n in born[:top]:
                w(f"     {fmt_factor(n['local_lq']):>8} tại chỗ (tích luỹ {fmt_factor(n['lq'])})  #{n['id']} {_name(n)}"
                  f"  {fmt_num(n['est'])} → {fmt_num(n['rows'])}\n")
                c = _conds(n["raw"])
                if c:
                    w(f"              {c[:160]}\n")
        elif S["analyzed"]:
            w("   Không node nào tự sinh sai số ước lượng ≥10×.\n")
        if a["insights"]:
            w("   Phát hiện:\n")
            for ins in a["insights"]:
                r = render_insight(ins, rules)
                where = f"#{ins['node']} " if ins["node"] is not None else ""
                w(f"     [{SEV_VI[ins['sev']]}] {ins['rule']} {where}{r['title']}\n")
                if ins["sev"] in ("critical", "high", "medium"):
                    w(f"         Vì sao: {r['detail']}\n")
                    if r["fix"]:
                        w(f"         Xử lý: {r['fix']}\n")
    if len(analyses) >= 2:
        A, la = analyses[0], plans[0].label
        for j in range(1, len(analyses)):
            B, lb = analyses[j], plans[j].label
            ea, eb = A["summary"]["execution_ms"], B["summary"]["execution_ms"]
            w(f"\n== So sánh {la} → {lb}\n")
            if ea and eb:
                r = eb / ea
                w(f"   Thời gian thực thi {fmt_ms(ea)} → {fmt_ms(eb)} ({'×' + fmt_num(round(r, 1)) + ' chậm hơn' if r >= 1 else '÷' + fmt_num(round(1 / r, 1)) + ' nhanh hơn'})\n")
            pairs = match(A, B)
            rows = []
            for i, k in pairs:
                na, nb = A["nodes"][i], B["nodes"][k]
                if na["excl"] is None or nb["excl"] is None:
                    continue
                rows.append((nb["excl"] - na["excl"], na, nb))
            rows.sort(key=lambda t: -abs(t[0]))
            if rows:
                w("   Node đổi nhiều nhất (thời gian riêng, ước lượng → thực tế, loops):\n")
                for d, na, nb in rows[:top]:
                    if abs(d) < 1 and rows[0][0] != d:
                        break
                    w(f"     {'+' if d >= 0 else '−'}{fmt_ms(abs(d)):>9}  #{nb['id']} {_name(nb)}: "
                      f"{fmt_ms(na['excl'])} → {fmt_ms(nb['excl'])}; {fmt_num(na['est'])}→{fmt_num(na['rows'])} "
                      f"→ {fmt_num(nb['est'])}→{fmt_num(nb['rows'])}; loops {fmt_num(na['loops'])} → {fmt_num(nb['loops'])}\n")
            used_a = {p[0] for p in pairs}
            used_b = {p[1] for p in pairs}
            only_a = [n for n in A["nodes"] if n["id"] not in used_a]
            only_b = [n for n in B["nodes"] if n["id"] not in used_b]
            if only_a or only_b:
                w("   Plan khác hình dạng:\n")
                if only_a:
                    w(f"     chỉ có ở {la}: " + "; ".join(f"#{n['id']} {_name(n)}" for n in only_a) + "\n")
                if only_b:
                    w(f"     chỉ có ở {lb}: " + "; ".join(f"#{n['id']} {_name(n)}" for n in only_b) + "\n")


def export_json(plans: List[LoadedPlan], analyses: List[Dict[str, Any]]) -> Dict[str, Any]:
    rules = load_rules()
    out: Dict[str, Any] = {"version": __version__, "plans": [], "matches": {}}
    keep = ("id", "parent", "depth", "children", "type", "label", "object", "index", "rel", "sub", "scope", "est",
            "width", "cost", "analyzed", "loops", "never", "rows", "est_total", "act_total", "lq", "base_lq",
            "local_lq", "incl", "excl", "pct", "removed", "buf", "buf_excl", "hot", "insights")
    for lp, a in zip(plans, analyses):
        nodes = []
        for n in a["nodes"]:
            d = {k: n.get(k) for k in keep}
            d["conds"] = _conds(n["raw"])
            d["q"] = fmt_factor(n["lq"])
            nodes.append(d)
        ins = []
        for i in a["insights"]:
            r = render_insight(i, rules)
            ins.append({"rule": i["rule"], "severity": i["sev"], "node": i["node"], **r, "params": i["p"]})
        out["plans"].append({"label": lp.label, "source": lp.source, "format": lp.fmt, "meta": lp.meta,
                             "summary": a["summary"], "nodes": nodes, "insights": ins})
    for i in range(len(analyses)):
        for j in range(i + 1, len(analyses)):
            out["matches"][f"{i}:{j}"] = match(analyses[i], analyses[j])
    return out


def _analyse(plans: List[LoadedPlan]) -> List[Dict[str, Any]]:
    return [analyze(lp.obj, lp.label, lp.meta) for lp in plans]


def cmd_report(args) -> int:
    plans = _load_all(args.plans, args.labels)
    analyses = _analyse(plans)
    write_html(args.output, plans, title=args.title)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(export_json(plans, analyses), fh, ensure_ascii=False, indent=1, default=str)
    if not args.quiet:
        summarize(plans, analyses, top=args.top)
    print(f"\nĐã ghi {args.output}" + (f" và {args.json}" if args.json else "") + " – mở bằng trình duyệt (chạy offline).",
          file=sys.stderr)
    return 0


def cmd_summary(args) -> int:
    plans = _load_all(args.plans, args.labels)
    summarize(plans, _analyse(plans), top=args.top)
    return 0


def cmd_app(args) -> int:
    plans = [] if args.no_demo else demo_plans()
    write_html(args.output, plans, title=args.title or "planviz", example=not args.no_demo)
    print(f"Đã ghi {args.output}: mở bằng trình duyệt, dán hoặc kéo-thả EXPLAIN vào để phân tích (không cần mạng).",
          file=sys.stderr)
    return 0


def cmd_explain(args) -> int:
    from . import explain as ex
    sql = ex.read_statement(args.query_file)
    if args.analyze and not args.quiet:
        print("Lưu ý: --analyze THỰC SỰ chạy câu lệnh (trong BEGIN … ROLLBACK, statement_timeout "
              f"{args.timeout}); UPDATE/DELETE giữ khoá dòng cho tới khi ROLLBACK.", file=sys.stderr)
    try:
        obj, err = ex.run(sql, args.dsn, psql=args.psql, analyze=args.analyze, timeout=args.timeout,
                          verbose=args.verbose, settings=not args.no_settings)
    except RuntimeError as e:
        print(f"planviz explain: {e}", file=sys.stderr)
        return 2
    label = args.label or "plan"
    lp = LoadedPlan(obj, label, ex.redact(args.dsn) or args.psql, "json", {"query": sql})
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump([obj], fh, ensure_ascii=False, indent=2)
        print(f"Đã ghi plan: {args.output}", file=sys.stderr)
    if args.html:
        write_html(args.html, [lp], title=args.title)
        print(f"Đã ghi {args.html}", file=sys.stderr)
    if not args.quiet:
        summarize([lp], _analyse([lp]), top=args.top)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="planviz", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"planviz {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, many=True):
        if many:
            p.add_argument("plans", nargs="+", help="file EXPLAIN (JSON/text), thư mục pgdoctor capture, hoặc '-'")
            p.add_argument("--labels", help="tên các plan, cách nhau dấu phẩy (mặc định lấy từ tên file: .pg17. → PG17)")
        p.add_argument("--top", type=int, default=5, help="số dòng mỗi mục trong tóm tắt (mặc định 5)")

    p = sub.add_parser("report", help="ghi trang HTML (1 plan: phân tích; 2+ plan: thêm so sánh) và in tóm tắt")
    common(p)
    p.add_argument("-o", "--output", default="planviz.html")
    p.add_argument("--title")
    p.add_argument("--json", help="ghi thêm kết quả phân tích dạng JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="không in tóm tắt")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("summary", help="chỉ in tóm tắt ra console")
    common(p)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("app", help="ghi trang công cụ trống (dán / kéo-thả plan), có sẵn ví dụ")
    p.add_argument("-o", "--output", default="planviz.html")
    p.add_argument("--title")
    p.add_argument("--no-demo", action="store_true", help="không nạp plan ví dụ")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("explain", help="chạy EXPLAIN qua psql rồi phân tích")
    p.add_argument("--dsn", help="conninfo / URI; bỏ trống nếu --psql đã có tham số kết nối")
    p.add_argument("--query-file", required=True, help="file chứa đúng 1 câu lệnh, không có EXPLAIN")
    p.add_argument("--psql", default="psql", help="lệnh psql, vd. 'docker exec -i pg17 psql -U postgres -d app'")
    p.add_argument("--analyze", action="store_true", help="chạy thật (EXPLAIN ANALYZE, BUFFERS) trong BEGIN … ROLLBACK")
    p.add_argument("--timeout", default="30min", help="statement_timeout cho lần chạy (mặc định 30min)")
    p.add_argument("--verbose", action="store_true", help="thêm VERBOSE (Output, tên schema)")
    p.add_argument("--no-settings", action="store_true", help="bỏ SETTINGS (server < PG12)")
    p.add_argument("--label")
    p.add_argument("--title")
    p.add_argument("-o", "--output", help="ghi plan JSON")
    p.add_argument("--html", help="ghi trang HTML")
    p.add_argument("-q", "--quiet", action="store_true")
    common(p, many=False)
    p.set_defaults(func=cmd_explain)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError) as e:
        print(f"planviz: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
