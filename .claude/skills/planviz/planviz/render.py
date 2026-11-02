"""Build the self-contained HTML page: shell.html + style.css + core.js + ui.js + the plans as embedded JSON.

The page analyses the embedded plans itself (core.js), so the same file doubles as an offline tool: plans can be
pasted or dropped into it later.  No external requests (fonts, scripts) are made."""
from __future__ import annotations

import html
import json
import os
import re
from typing import Iterable, List, Optional

from . import __version__
from .analyze import load_rules
from .loader import LoadedPlan, load

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
DEMO = [("demo/pg13_baseline.json", "PG13"), ("demo/pg17_baseline.json", "PG17"),
        ("demo/pg17_hint_work_mem.json", "PG17 + hint work_mem"), ("demo/pg17_not_exists.txt", "PG17 NOT EXISTS")]


def _read(name: str) -> str:
    with open(os.path.join(ASSETS, name), encoding="utf-8") as fh:
        return fh.read()


def demo_plans() -> List[LoadedPlan]:
    out = []
    for name, label in DEMO:
        lp = load(os.path.join(ASSETS, name), label)
        lp.source = "ví dụ: lab/remedies (PG 13.23 / 17.11)"
        out.append(lp)
    return out


def build_html(plans: Iterable[LoadedPlan], title: Optional[str] = None, example: bool = False) -> str:
    plans = list(plans)
    payload = []
    for p in plans:
        d = p.to_payload()
        d["example"] = example
        if isinstance(d["plan"].get("Query Identifier"), int):   # 64-bit: JS numbers would round it
            d["plan"] = dict(d["plan"], **{"Query Identifier": str(d["plan"]["Query Identifier"])})
        payload.append(d)
    data = {"version": __version__, "example": example, "rules": load_rules(), "plans": payload}
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # keep the JSON inert inside <script>: no '</script>' / '<!--' can appear
    data_json = data_json.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    labels = [p.label for p in plans]
    if not title:
        title = "planviz" if not labels or example else " vs ".join(labels[:3]) + " · planviz"
    sources = ""
    if plans and not example:
        sources = "Nguồn: " + "; ".join(f"{p.label} = {p.source}" for p in plans) + "."
    core, ui = _read("core.js"), _read("ui.js")
    for js in (core, ui):
        if "</script" in js.lower():
            raise ValueError("asset contains '</script'")
    mapping = {
        "TITLE": html.escape(title), "VERSION": __version__, "CSS": _read("style.css"),
        "SUBTITLE": "phân tích EXPLAIN của PostgreSQL", "SOURCES": html.escape(sources),
        "DATA": data_json, "CORE": core, "UI": ui,
    }
    return re.sub(r"__(TITLE|VERSION|CSS|SUBTITLE|SOURCES|DATA|CORE|UI)__", lambda m: mapping[m.group(1)], _read("shell.html"))


def write_html(path: str, plans: Iterable[LoadedPlan], title: Optional[str] = None, example: bool = False) -> str:
    text = build_html(plans, title=title, example=example)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
