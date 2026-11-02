"""Load EXPLAIN output from any common source and normalise it to the FORMAT JSON structure.

Accepted inputs (auto-detected):
  * EXPLAIN (FORMAT JSON) output - raw, or inside psql's table decoration ('QUERY PLAN', '+' continuations)
  * text EXPLAIN / EXPLAIN ANALYZE - raw or psql-decorated, also auto_explain log lines (JSON or text)
  * a directory written by 'pgdoctor capture' / 'pgdoctor run' (plan_analyze.json preferred over plan.json)
  * '-' for stdin
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .textplan import parse_text_plan


@dataclass
class LoadedPlan:
    obj: Dict[str, Any]          # {"Plan": {...}, "Planning Time": .., "Execution Time": .., ...}
    label: str
    source: str
    fmt: str                     # json | text
    meta: Dict[str, Any] = field(default_factory=dict)   # extra context (server version, query text, ...)

    def to_payload(self) -> Dict[str, Any]:
        return {"label": self.label, "source": self.source, "format": self.fmt, "meta": self.meta, "plan": self.obj}


def _strip_psql_decoration(text: str) -> str:
    out = []
    for ln in text.splitlines():
        s = ln.rstrip()
        if re.match(r"^\s*(SET|BEGIN|ROLLBACK|COMMIT|START TRANSACTION|RESET|DISCARD ALL|EXPLAIN)\s*;?\s*$", s) \
                or re.match(r"^Time:\s*[\d.]+\s*ms", s.strip()):
            continue
        if re.match(r"^\s*QUERY PLAN\s*$", s) or re.match(r"^\s*-{5,}\s*$", s) or re.match(r"^\(\d+ rows?\)$", s.strip()):
            continue
        if s.endswith("+"):
            s = s[:-1].rstrip()
        out.append(s)
    return "\n".join(out)


def _normalise_json(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, list):
        if not obj:
            raise ValueError("empty EXPLAIN JSON list")
        obj = obj[0]
    if not isinstance(obj, dict):
        raise ValueError("unexpected EXPLAIN JSON structure")
    if "Plan" not in obj:
        if "Node Type" in obj:
            return {"Plan": obj}
        if "QUERY PLAN" in obj:          # some GUI tools export rows as {"QUERY PLAN": [...]}
            return _normalise_json(obj["QUERY PLAN"])
        raise ValueError("JSON has no 'Plan' key - was it produced with EXPLAIN (FORMAT JSON)?")
    return obj


def parse_any(text: str) -> tuple:
    """Return (plan_obj, fmt) for EXPLAIN output in JSON or text form."""
    raw = text.strip().lstrip("﻿")
    candidate = _strip_psql_decoration(raw)
    for blob in (raw, candidate):
        starts = [i for i in (blob.find("["), blob.find("{")) if i >= 0]
        if not starts:
            continue
        start = min(starts)
        end = max(blob.rfind("]"), blob.rfind("}"))
        for chunk in (blob[start:], blob[start:end + 1] if end > start else None):
            if not chunk:
                continue
            try:
                return _normalise_json(json.loads(chunk)), "json"
            except (json.JSONDecodeError, ValueError):
                pass
    # auto_explain text: "LOG:  duration: 12.3 ms  plan:\n\tQuery Text: ...\n\tUpdate on ..." -> drop the prefix lines
    lines = candidate.splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"\(cost=[\d.]+\.\.[\d.]+ rows=", ln):
            candidate = "\n".join(lines[i:])
            break
    return parse_text_plan(candidate.replace("\t", "  ")), "text"


def _label_from_path(path: str) -> str:
    base = os.path.basename(path.rstrip("/\\")) or path
    m = re.search(r"(?:^|[._-])pg(\d{1,2})(?=[._-]|$)", base, re.I)
    if m:
        return f"PG{m.group(1)}"
    return re.sub(r"\.(json|txt|log|sql|explain)$", "", base, flags=re.I)


def load(path: str, label: Optional[str] = None) -> LoadedPlan:
    if path == "-":
        obj, fmt = parse_any(sys.stdin.read())
        return LoadedPlan(obj, label or "stdin", "stdin", fmt)
    if os.path.isdir(path):
        return _load_capture_dir(path, label)
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    obj, fmt = parse_any(text)
    return LoadedPlan(obj, label or _label_from_path(path), path, fmt)


def _load_capture_dir(d: str, label: Optional[str]) -> LoadedPlan:
    """Directory from 'pgdoctor capture' / 'pgdoctor run' (out_dir/old, out_dir/new)."""
    for name in ("plan_analyze.json", "plan.json"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                obj = _normalise_json(json.load(fh))
            meta: Dict[str, Any] = {"capture": name}
            mp = os.path.join(d, "meta.json")
            if os.path.isfile(mp):
                with open(mp, encoding="utf-8") as fh:
                    m = json.load(fh)
                for k in ("server_version", "server_version_num", "current_user", "database", "application_name",
                          "collected_at", "analyze_error"):
                    if m.get(k) not in (None, ""):
                        meta[k] = m[k]
            qp = os.path.join(d, "query.sql")
            if os.path.isfile(qp):
                with open(qp, encoding="utf-8") as fh:
                    meta["query"] = fh.read().strip()
            if not label:
                ver = ""
                m2 = re.search(r"PostgreSQL (\d+(?:\.\d+)?)", str(meta.get("server_version") or ""))
                if m2:
                    ver = m2.group(1)
                elif meta.get("server_version_num"):
                    vn = int(meta["server_version_num"])
                    ver = f"{vn // 10000}.{vn % 10000}"
                label = f"PG {ver}" if ver else _label_from_path(d)
            return LoadedPlan(obj, label, d, "json", meta)
    raise ValueError(f"{d}: no plan_analyze.json or plan.json - is this a pgdoctor capture directory?")
