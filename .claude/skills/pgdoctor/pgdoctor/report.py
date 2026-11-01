"""Self-contained HTML report (no external assets, works offline)."""
from __future__ import annotations

import datetime as _dt
import html
import json
from typing import Any, Dict, List, Optional

from . import __version__
from .findings import SEVERITIES, SEV_RANK, Finding, fmt_ms, fmt_num, sort_findings
from .kb import REF, RULES, refs_for
from .plan import Node, Plan

E = html.escape

SEV_VI = {"critical": "Nghiêm trọng", "high": "Cao", "medium": "Trung bình", "low": "Thấp", "info": "Thông tin"}
SCOPE_VI = {"compare": "So sánh plan", "plan": "Plan từng phiên bản", "settings": "Cấu hình (pg_settings)",
            "health": "Thống kê & catalog", "statements": "pg_stat_statements"}

CSS = r"""
:root{--bg:#f7f7f5;--panel:#ffffff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#2f5bd3;
--crit:#b3261e;--high:#d1501b;--med:#b7791f;--low:#3f7d4f;--info:#5b6b7f;--chip:#f0efea;--hl:#fff4c2;--div:#ffe1dc;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151515;--panel:#1e1e1d;--ink:#ecebe6;--muted:#a3a29b;
--line:#33332f;--accent:#7ea2ff;--chip:#2a2a27;--hl:#4a4220;--div:#4d2620;--crit:#ff6b5e;--high:#ff8f57;--med:#e5b04f;--low:#7fc58f;--info:#9fb0c4}}
:root[data-theme="dark"]{--bg:#151515;--panel:#1e1e1d;--ink:#ecebe6;--muted:#a3a29b;--line:#33332f;--accent:#7ea2ff;--chip:#2a2a27;
--hl:#4a4220;--div:#4d2620;--crit:#ff6b5e;--high:#ff8f57;--med:#e5b04f;--low:#7fc58f;--info:#9fb0c4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
header{padding:28px 32px 8px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}
h3{font-size:15px;margin:20px 0 8px}main{padding:0 32px 48px;max-width:1500px}
.muted{color:var(--muted)}.mono{font-family:var(--mono);font-size:12.5px}
nav{display:flex;gap:14px;flex-wrap:wrap;padding:6px 32px 14px;border-bottom:1px solid var(--line)}
nav a{color:var(--accent);text-decoration:none}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:120px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted);font-size:12px}
.sev{display:inline-block;border-radius:999px;padding:1px 9px;font-size:11.5px;font-weight:600;color:#fff;white-space:nowrap}
.sev.critical{background:var(--crit)}.sev.high{background:var(--high)}.sev.medium{background:var(--med)}
.sev.low{background:var(--low)}.sev.info{background:var(--info)}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--info);border-radius:8px;margin:8px 0}
.card.critical{border-left-color:var(--crit)}.card.high{border-left-color:var(--high)}.card.medium{border-left-color:var(--med)}
.card.low{border-left-color:var(--low)}
.card summary{cursor:pointer;padding:10px 14px;list-style:none;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.card summary::-webkit-details-marker{display:none}
.card .body{padding:0 16px 14px 16px;border-top:1px dashed var(--line)}
.rid{font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.subj{font-size:12px;color:var(--muted)}
ul.ev{margin:8px 0;padding-left:18px}ul.ev li{font-family:var(--mono);font-size:12.5px;word-break:break-word}
pre{background:var(--chip);padding:8px 10px;border-radius:6px;overflow:auto;font-size:12.5px;margin:6px 0}
.why,.act{margin:8px 0}.why b,.act b{display:block;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:13px}
th{background:var(--chip);font-weight:600}
.plans{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:1000px){.plans{grid-template-columns:1fr}}
.tree{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 0;overflow:auto}
.tree h4{margin:2px 12px 8px;font-size:13px}
.nd{display:grid;grid-template-columns:minmax(260px,1fr) 70px 90px 64px 110px;gap:6px;padding:2px 12px;
font-family:var(--mono);font-size:12px;align-items:center;border-left:3px solid transparent}
.nd.hdr{color:var(--muted);font-family:var(--sans);font-size:11.5px;border-bottom:1px solid var(--line);padding-bottom:4px}
.nd.flag{background:var(--hl)}.nd.diverge{background:var(--div);border-left-color:var(--crit)}
.nd.hover{outline:2px solid var(--accent)}
.lbl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.num{text-align:right}
.q{display:inline-block;border-radius:4px;padding:0 5px;text-align:center}
.q.ok{color:var(--muted)}.q.w{background:var(--med);color:#fff}.q.b{background:var(--high);color:#fff}.q.x{background:var(--crit);color:#fff}
.bar{height:8px;background:var(--chip);border-radius:4px;position:relative}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:4px}
.legend{font-size:12px;color:var(--muted);margin:6px 0 10px}
.kv{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0}.kv div{font-size:13px}.kv b{font-size:15px}
.top li{margin:4px 0}
footer{color:var(--muted);font-size:12px;padding:20px 32px}
.toggle{float:right;font-size:12px}
"""

JS = r"""
document.querySelectorAll('.nd[data-m]').forEach(el=>{
  el.addEventListener('mouseenter',()=>{const t=document.getElementById(el.dataset.m);el.classList.add('hover');if(t)t.classList.add('hover');});
  el.addEventListener('mouseleave',()=>{const t=document.getElementById(el.dataset.m);el.classList.remove('hover');if(t)t.classList.remove('hover');});
});
document.querySelectorAll('[data-expand]').forEach(b=>b.addEventListener('click',()=>{
  const open=b.dataset.expand==='1';document.querySelectorAll(b.dataset.target+' details').forEach(d=>d.open=open);}));
const tb=document.getElementById('theme');if(tb)tb.addEventListener('click',()=>{const r=document.documentElement;
  r.dataset.theme=(r.dataset.theme==='dark'||(!r.dataset.theme&&matchMedia('(prefers-color-scheme: dark)').matches))?'light':'dark';});
"""


def _sev(s: str) -> str:
    return f'<span class="sev {s}">{E(SEV_VI.get(s, s))}</span>'


def _q_chip(n: Node) -> str:
    q = n.qerror
    if q is None:
        return '<span class="q ok">–</span>'
    a = abs(q)
    cls = "ok" if a < 10 else ("w" if a < 100 else ("b" if a < 1000 else "x"))
    arrow = "↑" if q > 0 else "↓"
    return f'<span class="q {cls}" title="q-error: {"thiếu" if q > 0 else "thừa"} {a:.1f}×">{arrow}{fmt_num(round(a, 1))}×</span>'


def _finding_card(f: Finding, anchor: str = "") -> str:
    r = RULES.get(f.rule)
    title = r.title if r else f.rule
    ev = "".join(f"<li>{E(x)}</li>" for x in f.evidence)
    why = f'<div class="why"><b>Vì sao</b>{E(r.why)}</div>' if r else ""
    act = f'<div class="act"><b>Nên làm</b>{E(r.action)}</div>' if r else ""
    diag = "".join(f"<pre>{E(d)}</pre>" for d in (r.diag if r else []))
    refs = refs_for(f.rule)
    ref_html = ("<div class='act'><b>Nguồn</b>" + " · ".join(f'<a href="{E(u)}">{E(t)}</a>' for t, u in refs) + "</div>") if refs else ""
    side = {"old": "bản cũ", "new": "bản mới", "both": "so sánh"}.get(f.side or "", "")
    since = f" · PG{r.since}+" if r and r.since else ""
    return (f'<details class="card {f.severity}"{(" id=" + chr(34) + anchor + chr(34)) if anchor else ""}><summary>{_sev(f.severity)}'
            f'<span><b>{E(f.message)}</b></span><span class="rid">{E(f.rule)}{E(since)}</span>'
            f'<span class="subj">{E(f.subject)}{(" · " + side) if side else ""}</span></summary>'
            f'<div class="body"><div class="why"><b>Rule</b>{E(title)}</div>'
            f'{"<ul class=ev>" + ev + "</ul>" if ev else ""}{why}{act}{diag}{ref_html}</div></details>')


def _remedy_card(i: int, r) -> str:
    if not r.effective:
        tag, cls = "Không hiệu quả", "info"
    elif r.code_change:
        tag, cls = "Cần sửa code", "medium"
    else:
        tag, cls = "Không sửa code", "low"
    meta = f"Phạm vi: {r.blast}" + (" · cần restart instance" if r.restart else "")
    parts = [f'<details class="card {cls}"><summary><span class="sev {cls}">{E(tag)}</span>'
             f'<span><b>{i}. {E(r.title)}</b></span><span class="subj">{E(meta)}</span></summary><div class="body">']
    if r.target_desc:
        parts.append(f'<div class="why"><b>Áp dụng cho</b><span class="mono">{E(r.target_desc)}</span></div>')
    parts.append(f'<div class="why"><b>Vì sao</b>{E(r.why)}</div>')
    if r.lab:
        parts.append(f'<div class="act"><b>Đo trong lab</b>{E(r.lab)}</div>')
    if r.caveats:
        parts.append('<div class="act"><b>Lưu ý</b><ul class="ev">' + "".join(f"<li>{E(c)}</li>" for c in r.caveats) + "</ul></div>")
    for lbl, lines in (("Các bước", r.steps), ("Kiểm tra", r.verify), ("Hoàn tác", r.rollback)):
        if lines:
            parts.append(f'<div class="act"><b>{lbl}</b><pre>{E(chr(10).join(lines))}</pre></div>')
    parts.append("</div></details>")
    return "".join(parts)


def _tree(plan: Plan, prefix: str, other_prefix: str, mapping: Dict[int, int], flagged: set,
          diverge: Optional[int], title: str) -> str:
    rt = plan.runtime_ms or 0
    rows = [f'<div class="tree"><h4>{E(title)}</h4>',
            '<div class="nd hdr"><span>Node</span><span class="num">Ước lượng</span><span class="num">Thực tế (tổng)</span>'
            '<span class="num">Sai số</span><span>Thời gian riêng</span></div>']
    for n in plan.nodes:
        indent = "&nbsp;" * (n.depth * 2)
        pre = f"<span class='muted'>[{E(n.subplan_name)}]</span> " if n.subplan_name else ""
        cls = "nd"
        if n.id in flagged:
            cls += " flag"
        if diverge is not None and n.id == diverge:
            cls += " diverge"
        m = mapping.get(n.id)
        mattr = f' data-m="{other_prefix}{m}"' if m is not None else ""
        act = fmt_num(n.actual_rows_total) if n.analyzed else "–"
        if n.analyzed and n.loops > 1:
            act += f" <span class='muted'>({fmt_num(n.loops)} loop)</span>"
        if n.never_executed:
            act = "<span class='muted'>never executed</span>"
        ex = n.exclusive_time
        share = (ex / rt * 100) if (ex is not None and rt) else 0
        bar = (f'<div class="bar" title="{fmt_ms(ex)} ({share:.0f}%)"><i style="width:{min(share,100):.1f}%"></i></div>'
               if ex is not None else "<span class='muted'>–</span>")
        tip = []
        for k in ("Filter", "Index Cond", "Hash Cond", "Merge Cond", "Join Filter", "Group Key", "Sort Key", "Cache Key"):
            v = n.get(k)
            if v:
                tip.append(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
        rows.append(f'<div class="{cls}" id="{prefix}{n.id}"{mattr} title="{E(chr(10).join(tip))}">'
                    f'<span class="lbl">{indent}{pre}{E(n.describe())}</span><span class="num">{fmt_num(n.est_rows_total)}</span>'
                    f'<span class="num">{act}</span><span class="num">{_q_chip(n)}</span>{bar}</div>')
    rows.append("</div>")
    return "".join(rows)


_REL_VI = {"critical": ("then chốt", "critical"), "plan": ("node trong plan", "high"), "planner": ("planner chung", "medium"),
           "other": ("khác", "info")}


def _src(v: str) -> str:
    return f" <span class='muted'>({E(v)})</span>" if v else ""


def _query_config_section(ctx: Dict[str, Any]) -> str:
    """Effective configuration of the session that ran the statement on each server (pgdoctor run / qdiff)."""
    d = ctx["query_config"]
    lo, ln = ctx["labels"]
    caps = ctx.get("captures") or {}
    o: List[str] = ["<h2 id='qconfig'>Cấu hình hiệu lực khi chạy chính câu lệnh này</h2>",
                    "<div class='legend'>Lấy từ pg_settings NGAY trong session chạy câu lệnh trên mỗi server (đã gồm ALTER ROLE / "
                    "ALTER DATABASE … SET và tham số kết nối của client). Chỉ giữ tham số điều khiển planner/executor; xếp theo "
                    "mức liên quan tới plan.</div>"]
    if caps:
        o.append("<table><tr><th></th><th>Phiên bản</th><th>Session</th><th>Chế độ</th><th>Kết nối</th></tr>")
        for lbl in (lo, ln):
            c = caps.get(lbl)
            if not c:
                continue
            m = c["meta"]
            mode = "EXPLAIN ANALYZE" if c.get("analyzed") else "EXPLAIN (không ANALYZE)"
            if m.get("analyze_error"):
                mode += f" · ANALYZE lỗi: {m['analyze_error']}"
            if m.get("params"):
                mode += f" · PREPARE/EXECUTE ({len(m['params'])} tham số{', generic plan' if m.get('generic_plan') else ''})"
            o.append(f"<tr><td><b>{E(lbl)}</b></td><td class='mono'>{E(str(m.get('server_version', '')).split(' on ')[0])}</td>"
                     f"<td class='mono'>user={E(str(m.get('current_user')))} db={E(str(m.get('database')))} "
                     f"application_name={E(str(m.get('application_name')))}</td><td>{E(mode)}</td>"
                     f"<td class='mono'>{E(m.get('dsn') or '')}{(' · options ' + E(m['client_options'])) if m.get('client_options') else ''}</td></tr>")
        o.append("</table>")
    o.append("<h3>Kết luận</h3><ul>" + "".join(f"<li>{E(v)}</li>" for v in d["verdict"]) + "</ul>")
    if d["thresholds"]:
        o.append("<h3>Quyết định hash của SubPlan (NOT IN / = ANY)</h3><ul>" +
                 "".join(f"<li class='mono'>{E(t['text'])}</li>" for t in d["thresholds"]) + "</ul>")
    if d["key"]:
        o.append(f"<h3>Tham số then chốt của plan này (in cả khi giống nhau)</h3><table><tr><th>Tham số</th><th>{E(lo)}</th>"
                 f"<th>{E(ln)}</th><th></th><th>Liên quan</th></tr>")
        for k in d["key"]:
            o.append(f"<tr><td class='mono'>{E(k['name'])}</td><td class='mono'>{E(k['old'])}{_src(k['old_src'])}</td>"
                     f"<td class='mono'>{E(k['new'])}{_src(k['new_src'])}</td>"
                     f"<td>{'giống' if k['same'] else '<b>KHÁC</b>'}</td><td>{E(', '.join(k['rules']))}<br>"
                     f"<span class='muted'>{E(k['why'])}</span></td></tr>")
        o.append("</table>")

    def table(rows) -> str:
        t = [f"<table><tr><th>Mức</th><th>Tham số</th><th>{E(lo)}</th><th>{E(ln)}</th><th>Vì sao liên quan</th></tr>"]
        for r in rows:
            vi, cls = _REL_VI[r["relevance"]]
            flag = {"changed": "", "only_new": f"chỉ có ở {ln}", "only_old": f"chỉ có ở {lo}"}[r["kind"]]
            if r["default_changed"]:
                flag = "mặc định đổi theo phiên bản"
            note = r["why"] + (f" · Node: {', '.join(r['nodes'][:5])}" if r["nodes"] else "") + \
                (f" · Finding: {', '.join(r['rules'])}" if r["rules"] else "")
            t.append(f"<tr><td><span class='sev {cls}'>{E(vi)}</span></td><td class='mono'>{E(r['name'])}"
                     f"{('<br><span class=muted>' + E(flag) + '</span>') if flag else ''}</td>"
                     f"<td class='mono'>{E(r['old'])}{_src(r['old_src'])}</td><td class='mono'>{E(r['new'])}{_src(r['new_src'])}</td>"
                     f"<td>{E(note)}</td></tr>")
        t.append("</table>")
        return "".join(t)
    main_rows = [r for r in d["rows"] if r["relevance"] != "other"]
    other_rows = [r for r in d["rows"] if r["relevance"] == "other"]
    o.append(f"<h3>Khác biệt liên quan tới câu lệnh <span class='muted'>({len(main_rows)})</span></h3>")
    o.append(table(main_rows) if main_rows else "<p class='muted'>Không có khác biệt nào ở tham số liên quan tới plan này.</p>")
    if other_rows:
        o.append(f"<details><summary>Khác biệt khác trong nhóm planner/executor – không điều khiển node nào của hai plan "
                 f"({len(other_rows)})</summary>{table(other_rows)}</details>")
    es = d["explain_settings"]
    o.append("<h3>EXPLAIN (SETTINGS)</h3><div class='legend'>EXPLAIN chỉ in tham số khác giá trị mặc định của chính phiên bản "
             "đó, nên mặc định đổi theo phiên bản (vd. hash_mem_multiplier 1 → 2 từ PG15) KHÔNG hiện ở đây – vì vậy cần bảng "
             "ở trên.</div><ul>" + "".join(f"<li><b>{E(l)}</b>: <span class='mono'>{E(json.dumps(es.get(l) or {}, ensure_ascii=False))}"
                                            f"</span></li>" for l in (lo, ln)) + "</ul>")
    if caps:
        rs = []
        for lbl in (lo, ln):
            c = caps.get(lbl) or {}
            for e in c.get("role_settings") or []:
                rs.append(f"<li><b>{E(lbl)}</b> role={E(str(e.get('role')))} database={E(str(e.get('database')))}: "
                          f"<span class='mono'>{E(', '.join(e.get('config') or []))}</span></li>")
        o.append("<h3>ALTER ROLE / ALTER DATABASE … SET đang áp dụng cho session</h3>" +
                 ("<ul>" + "".join(rs) + "</ul>" if rs else "<p class='muted'>Không có.</p>"))
        hx = []
        for lbl in (lo, ln):
            ex = (caps.get(lbl) or {}).get("extras") or {}
            if ex.get("query_id") is not None:
                hx.append(f"<li><b>{E(lbl)}</b> query_id <span class='mono'>{E(str(ex['query_id']))}</span></li>")
            if ex.get("has_hint_table"):
                hs = ex.get("hints") or []
                hx.append(f"<li><b>{E(lbl)}</b> pg_hint_plan hint table: {len(hs)} dòng khớp" +
                          "".join(f"<br><span class='mono'>{E(json.dumps(h, ensure_ascii=False, default=str))}</span>" for h in hs[:10]) + "</li>")
            for st in ex.get("statements") or []:
                hx.append(f"<li><b>{E(lbl)}</b> pg_stat_statements: calls={E(str(st.get('calls')))}, "
                          f"mean={fmt_ms(st.get('mean_exec_time'))}, max={fmt_ms(st.get('max_exec_time'))}, "
                          f"temp_blks_read={E(str(st.get('temp_blks_read')))}</li>")
        if hx:
            o.append("<h3>query_id · hint table · pg_stat_statements</h3><ul>" + "".join(hx) + "</ul>"
                     "<div class='legend'>Câu gửi bằng literal và câu gửi bằng tham số $1 (JDBC) có query_id KHÁC nhau – "
                     "hint table phải dùng query_id của câu ứng dụng thật gửi.</div>")
    return "".join(o)


def render(ctx: Dict[str, Any]) -> str:
    """ctx keys: queries[list of {name, old, new, cmp, findings}], findings, settings_table, statements_rows,
    labels(old,new), inputs"""
    findings: List[Finding] = sort_findings(ctx["findings"])
    lo, ln = ctx["labels"]
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    gen = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"<!doctype html><html lang='vi'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>pgdoctor – {E(lo)} → {E(ln)}</title><style>{CSS}</style></head><body>"]
    out.append(f"<header><button class='toggle' id='theme' type='button'>Sáng/Tối</button>"
               f"<h1>pgdoctor · Báo cáo regression {E(lo)} → {E(ln)}</h1>"
               f"<div class='muted'>Tạo lúc {gen} · pgdoctor {__version__} · phân tích offline từ file EXPLAIN / pg_settings / "
               f"health / pg_stat_statements</div></header>")
    nav = ["<a href='#summary'>Tóm tắt</a>", "<a href='#findings'>Findings</a>"]
    if ctx["queries"]:
        nav.append("<a href='#plans'>So sánh plan</a>")
    if ctx.get("query_config") is not None:
        nav.insert(2, "<a href='#qconfig'>Cấu hình khi chạy</a>")
    if ctx.get("settings_table") is not None:
        nav.append("<a href='#settings'>Cấu hình</a>")
    if ctx.get("statements_rows") is not None:
        nav.append("<a href='#statements'>pg_stat_statements</a>")
    if any(q.get("remedies") for q in ctx["queries"]):
        nav.insert(2, "<a href='#remedies'>Khắc phục</a>")
    nav.append("<a href='#kb'>Knowledge base</a>")
    out.append("<nav>" + "".join(nav) + "</nav><main>")

    # ---------------------------------------------------------------- summary
    out.append("<h2 id='summary'>Tóm tắt</h2><div class='tiles'>")
    for s in SEVERITIES:
        out.append(f"<div class='tile'><b>{counts[s]}</b>{_sev(s)}</div>")
    out.append("</div>")
    if ctx["queries"]:
        out.append("<table><tr><th>Query</th><th>Định dạng</th><th>" + E(lo) + "</th><th>" + E(ln) +
                   "</th><th>Tỉ lệ</th><th>Finding nặng nhất</th></tr>")
        for q in ctx["queries"]:
            o, n = q["old"], q["new"]
            ratio = (n.execution_time / o.execution_time) if (o and n and o.execution_time and n.execution_time) else None
            qf = sort_findings(q["findings"])
            worst = qf[0] if qf else None
            out.append(f"<tr><td><a href='#q-{E(q['id'])}'>{E(q['name'])}</a></td>"
                       f"<td class='muted'>{E((o.fmt if o else '-') + ' / ' + (n.fmt if n else '-'))}"
                       f"{'' if (o and o.analyzed and n and n.analyzed) else ' · thiếu ANALYZE'}</td>"
                       f"<td>{fmt_ms(o.execution_time) if o else '-'}</td><td>{fmt_ms(n.execution_time) if n else '-'}</td>"
                       f"<td>{(f'{ratio:.1f}×') if ratio else '-'}</td>"
                       f"<td>{(_sev(worst.severity) + ' ' + E(worst.message)) if worst else '-'}</td></tr>")
        out.append("</table>")
    top = [f for f in findings if SEV_RANK[f.severity] <= SEV_RANK["high"]][:8]
    if top:
        out.append("<h3>Nghi phạm hàng đầu</h3><ol class='top'>")
        for f in top:
            out.append(f"<li>{_sev(f.severity)} <b>{E(f.message)}</b> <span class='rid'>{E(f.rule)} · {E(f.subject)}</span></li>")
        out.append("</ol>")

    if ctx.get("query_config") is not None:
        out.append(_query_config_section(ctx))

    # --------------------------------------------------------------- findings
    out.append("<h2 id='findings'>Findings <span class='muted' style='font-size:13px'>"
               "<a href='#' data-expand='1' data-target='#fl' onclick='return false'>mở tất cả</a> · "
               "<a href='#' data-expand='0' data-target='#fl' onclick='return false'>đóng tất cả</a></span></h2><div id='fl'>")
    for scope in ("compare", "plan", "health", "settings", "statements"):
        fs = [f for f in findings if f.scope == scope]
        if not fs:
            continue
        out.append(f"<h3>{E(SCOPE_VI[scope])} <span class='muted'>({len(fs)})</span></h3>")
        out.extend(_finding_card(f) for f in fs)
    if not findings:
        out.append("<p class='muted'>Không có finding nào.</p>")
    out.append("</div>")

    # --------------------------------------------------------------- remedies
    rq = [q for q in ctx["queries"] if q.get("remedies")]
    if rq:
        out.append("<h2 id='remedies'>Khắc phục <span class='muted' style='font-size:13px'>ưu tiên phương án KHÔNG sửa code, "
                   "phạm vi hẹp nhất trước – mỗi phương án là một lựa chọn thay thế</span></h2>")
        for q in rq:
            out.append(f"<h3>{E(q['name'])}</h3>")
            for i, r in enumerate(q["remedies"], 1):
                out.append(_remedy_card(i, r))

    # ------------------------------------------------------------------ plans
    if ctx["queries"]:
        out.append("<h2 id='plans'>So sánh plan</h2>"
                   "<div class='legend'>Hàng vàng: node có finding · Hàng đỏ: điểm phân kỳ đầu tiên · Rê chuột lên một node để thấy node "
                   "tương ứng ở bản kia · ↑ = ước lượng thiếu, ↓ = ước lượng thừa · 'Thời gian riêng' là xấp xỉ (exclusive time).</div>")
    for q in ctx["queries"]:
        o, n, cmp_ = q["old"], q["new"], q.get("cmp")
        out.append(f"<h3 id='q-{E(q['id'])}'>{E(q['name'])}</h3><div class='kv'>")
        for lbl, p in ((lo, o), (ln, n)):
            if p:
                out.append(f"<div>{E(lbl)}: <b>{fmt_ms(p.execution_time)}</b> exec · planning {fmt_ms(p.planning_time)}"
                           f"{' · JIT ' + fmt_ms(p.jit_total_ms) if p.jit_total_ms else ''}"
                           f" <span class='muted'>({E(p.source)})</span></div>")
        out.append("</div>")
        flagged_new = {i for f in q["findings"] if f.side in ("new", "both") for i in f.node_ids}
        flagged_old = {i for f in q["findings"] if f.side == "old" for i in f.node_ids} | \
                      {i for f in q["findings"] if f.side == "both" for i in f.old_node_ids}
        qid = q["id"]
        out.append("<div class='plans'>")
        if o:
            out.append(_tree(o, f"o{qid}-", f"n{qid}-", cmp_.old_to_new if cmp_ else {}, flagged_old,
                             cmp_.divergence_old if cmp_ else None, f"{lo}"))
        if n:
            out.append(_tree(n, f"n{qid}-", f"o{qid}-", cmp_.new_to_old if cmp_ else {}, flagged_new,
                             cmp_.divergence_new if cmp_ else None, f"{ln}"))
        out.append("</div>")

    # --------------------------------------------------------------- settings
    st = ctx.get("settings_table")
    if st is not None:
        out.append("<h2 id='settings'>Cấu hình khác nhau (tham số ảnh hưởng planner/executor)</h2>")
        if st:
            out.append(f"<table><tr><th>Tham số</th><th>{E(lo)}</th><th>{E(ln)}</th><th>Mức ảnh hưởng</th><th>Ghi chú</th></tr>")
            order = {"high": 0, "medium": 1, "low": 2}
            for r in sorted(st, key=lambda r: (order.get(r["impact"], 3), r["name"])):
                out.append(f"<tr><td class='mono'>{E(r['name'])}</td><td class='mono'>{E(r['old'])} <span class='muted'>({E(r['source_old'])})</span></td>"
                           f"<td class='mono'>{E(r['new'])} <span class='muted'>({E(r['source_new'])})</span></td>"
                           f"<td>{E(r['impact'])}</td><td>{E(r['note'])}</td></tr>")
            out.append("</table>")
        else:
            out.append("<p class='muted'>Không có khác biệt ở các tham số được theo dõi.</p>")

    # ------------------------------------------------------------- statements
    sr = ctx.get("statements_rows")
    if sr is not None:
        out.append("<h2 id='statements'>pg_stat_statements (ghép theo text đã chuẩn hoá)</h2>")
        if sr:
            out.append(f"<table><tr><th>Query</th><th>calls {E(lo)}/{E(ln)}</th><th>mean {E(lo)}</th><th>mean {E(ln)}</th>"
                       "<th>Tỉ lệ</th><th>Thêm (tổng)</th><th>temp blks/call</th></tr>")
            for r in sr[:60]:
                ratio = "∞" if r["ratio"] == float("inf") else f"{r['ratio']:.1f}×"
                out.append(f"<tr><td class='mono' style='max-width:520px'>{E(' '.join(r['query'].split())[:300])}</td>"
                           f"<td>{fmt_num(r['calls_old'])} / {fmt_num(r['calls_new'])}</td><td>{fmt_ms(r['mean_old'])}</td>"
                           f"<td>{fmt_ms(r['mean_new'])}</td><td>{ratio}</td><td>{fmt_ms(r['extra_ms'])}</td>"
                           f"<td>{fmt_num(r['temp_old'])} → {fmt_num(r['temp_new'])}</td></tr>")
            out.append("</table>")
        else:
            out.append("<p class='muted'>Không ghép được query nào giữa hai bản.</p>")

    # --------------------------------------------------------------------- KB
    out.append("<h2 id='kb'>Knowledge base – các pattern tool phát hiện</h2><table><tr><th>Rule</th><th>Pattern</th><th>Vì sao</th><th>Nguồn</th></tr>")
    for r in RULES.values():
        refs = " · ".join(f'<a href="{E(REF[k][1])}">{E(REF[k][0])}</a>' for k in r.refs if k in REF)
        out.append(f"<tr><td class='mono'>{E(r.id)}{('<br><span class=muted>PG' + E(r.since) + '+</span>') if r.since else ''}</td>"
                   f"<td><b>{E(r.title)}</b></td><td>{E(r.why)}</td><td>{refs}</td></tr>")
    out.append("</table></main>")
    how = ("Plan và cấu hình được lấy trực tiếp bằng psql trong session chạy câu lệnh (pgdoctor run/capture), câu lệnh chạy "
           "trong BEGIN … ROLLBACK. " if ctx.get("captures") else "Tool chỉ đọc các file đầu vào; không kết nối database. ")
    out.append(f"<footer>Inputs: {E(', '.join(ctx.get('inputs', [])))}<br>{how}"
               f"Mọi finding là heuristic – hãy xác nhận bằng EXPLAIN (ANALYZE, BUFFERS) và thử nghiệm SET LOCAL trong transaction.</footer>")
    data = json.dumps([f.to_dict() for f in findings], ensure_ascii=False).replace("</", "<\\/")
    out.append(f"<script type='application/json' id='findings-data'>{data}</script><script>{JS}</script></body></html>")
    return "".join(out)
