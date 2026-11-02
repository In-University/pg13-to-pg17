"""Live mode: run EXPLAIN of one statement through psql and return the plan as FORMAT JSON.

Safety: the statement runs inside BEGIN … ROLLBACK with SET LOCAL statement_timeout, and only with --analyze is it
actually executed (plain EXPLAIN never executes it).  An executed UPDATE/DELETE still takes its row locks until the
ROLLBACK, so prefer a restored copy or a quiet window for production servers."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Dict, List, Optional, Tuple

from .loader import parse_any

TIMEOUT_RE = re.compile(r"^\d+\s*(ms|s|min|h)?$")


def _scan_problem(sql: str) -> Optional[str]:
    """Return why the text is not exactly one SQL statement without psql meta-commands, or None if it is."""
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        if ch in ("'", '"'):
            esc = ch == "'" and i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            i += 1
            while i < n:
                if esc and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == ch:
                    if i + 1 < n and sql[i + 1] == ch:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                i = n if j < 0 else j + len(tag)
                continue
        if ch == "\\":
            return "chứa lệnh meta của psql (dấu \\ ngoài chuỗi)"
        if ch == ";":
            if sql[i + 1:].strip():
                return "chứa nhiều hơn một câu lệnh"
        i += 1
    return None


def read_statement(path: str) -> str:
    with open(path, encoding="utf-8-sig") as fh:
        sql = fh.read().strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if not sql:
        raise ValueError(f"{path}: file rỗng")
    if re.match(r"^\s*EXPLAIN\b", sql, re.I):
        raise ValueError(f"{path}: hãy để câu lệnh gốc, không có EXPLAIN (planviz tự thêm)")
    prob = _scan_problem(sql)
    if prob:
        raise ValueError(f"{path}: {prob}")
    return sql


def build_script(sql: str, analyze: bool, timeout: str, verbose: bool = False, settings: bool = True) -> str:
    if not TIMEOUT_RE.match(timeout.strip()):
        raise ValueError(f"timeout không hợp lệ: {timeout!r} (vd. 30s, 10min, 1h)")
    opts: List[str] = []
    if analyze:
        opts += ["ANALYZE", "BUFFERS"]
    if verbose:
        opts.append("VERBOSE")
    if settings:
        opts.append("SETTINGS")
    opts.append("FORMAT JSON")
    return "\n".join([
        "BEGIN;",
        f"SET LOCAL statement_timeout = '{timeout.strip()}';",
        f"EXPLAIN ({', '.join(opts)})",
        sql + ";",
        "ROLLBACK;",
        "",
    ])


def run(sql: str, dsn: Optional[str], psql: str = "psql", analyze: bool = False, timeout: str = "30min",
        verbose: bool = False, settings: bool = True, env: Optional[Dict[str, str]] = None) -> Tuple[dict, str]:
    """Return (plan object, psql stderr)."""
    script = build_script(sql, analyze, timeout, verbose, settings)
    cmd = shlex.split(psql) + ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    if dsn:
        cmd.append(dsn)
    try:
        proc = subprocess.run(cmd, input=script, capture_output=True, text=True, env=env or os.environ.copy())
    except FileNotFoundError as e:
        raise RuntimeError(f"không chạy được '{cmd[0]}': {e}. Cài psql hoặc dùng --psql (vd. 'docker exec -i <container> psql -U <user>').")
    err = proc.stderr.strip()
    if proc.returncode != 0:
        msg = re.sub(r"^psql:<stdin>:\d+:\s*", "", err, flags=re.M) or f"psql thoát với mã {proc.returncode}"
        raise RuntimeError(msg)
    obj, _ = parse_any(proc.stdout)
    return obj, err


def redact(dsn: Optional[str]) -> str:
    if not dsn:
        return ""
    dsn = re.sub(r"(password\s*=\s*)('[^']*'|\S+)", r"\1***", dsn, flags=re.I)
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", dsn)
