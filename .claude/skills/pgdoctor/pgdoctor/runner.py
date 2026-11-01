"""Run ONE statement on a live server through psql and capture what its planner actually saw.

No Python database driver is needed: a script is fed to `psql` (any version >= 10) on stdin and every result is
printed between '@@PGDOCTOR <section>@@' markers.  The psql command itself is configurable, so the same code works
with a local psql, `docker exec -i <container> psql`, `kubectl exec -i … psql`, or the Cloud SQL Auth Proxy.

What is captured, in the SAME session that runs the statement (so role / database / client overrides are visible):
  meta            server version, current_user, database, application_name, extensions
  settings        the whole pg_settings view (value + source: default / configuration file / database / user /
                  database user / client / session) - this is the configuration the statement really ran with
  role_settings   ALTER ROLE / ALTER DATABASE … SET entries that apply to this role + database
  explain         EXPLAIN (VERBOSE, SETTINGS, FORMAT JSON)               - always (instant)
  explain_analyze EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) - only when asked, under statement_timeout
A second, read-only session then collects statistics of the tables in the plan, pg_hint_plan hint-table rows and
pg_stat_statements rows for the plan's query_id.

The statement always runs inside BEGIN … ROLLBACK: EXPLAIN ANALYZE of an UPDATE changes nothing, but it DOES take
the same row locks as the real statement while it runs.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

MARK = "@@PGDOCTOR "
_SECTION_RE = re.compile(r"^@@PGDOCTOR (\w+)@@\s*$")
_TIMEOUT_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(us|ms|s|min|h|d)?\s*$")


# ----------------------------------------------------------------------------------------------- inputs
def read_query(path: str) -> str:
    """Read the statement from a file: one statement, trailing ';' optional."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    q = text.strip()
    while q.endswith(";"):
        q = q[:-1].rstrip()
    if not q:
        raise ValueError(f"{path}: file rỗng")
    if re.match(r"^\s*explain\b", q, re.I):
        raise ValueError(f"{path}: chỉ đưa câu lệnh gốc, không kèm EXPLAIN (pgdoctor tự thêm)")
    problem = _statement_problem(q)
    if problem:
        raise ValueError(f"{path}: {problem}")
    return q


def _statement_problem(q: str) -> Optional[str]:
    """The text is pasted into a psql script: refuse psql meta-commands (an unquoted backslash, e.g. '\\!' or
    '\\gset') and more than one statement.  Quotes, E'' strings, dollar quotes and comments are skipped."""
    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c == "\\":
            return "có lệnh psql (dấu '\\' ngoài chuỗi ký tự) – chỉ đưa câu SQL"
        if q.startswith("--", i):
            j = q.find("\n", i)
            i = n if j < 0 else j
        elif q.startswith("/*", i):
            j = q.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            escape = c == "'" and i > 0 and q[i - 1] in "eE" and (i < 2 or not (q[i - 2].isalnum() or q[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escape and q[j] == "\\":
                    j += 2
                elif q[j] == c and q.startswith(c * 2, j):
                    j += 2
                elif q[j] == c:
                    break
                else:
                    j += 1
            i = j + 1
        elif c == "$" and re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", q[i:]):
            tag = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", q[i:]).group(0)
            j = q.find(tag, i + len(tag))
            i = n if j < 0 else j + len(tag)
        elif c == ";":
            return "có nhiều hơn 1 câu lệnh – mỗi file chỉ chứa đúng 1 câu"
        else:
            i += 1
    return None


def augment_dsn(dsn: str, app_name: Optional[str] = None, options: Optional[str] = None) -> str:
    """Add application_name / options (e.g. '-c work_mem=64MB', what a JDBC 'options' parameter sends) to a libpq
    conninfo string or URI, so they arrive as CLIENT settings exactly like the application's."""
    if not app_name and not options:
        return dsn
    if re.match(r"^postgres(ql)?://", dsn or ""):
        parts = urlsplit(dsn)
        q = parse_qsl(parts.query, keep_blank_values=True)
        if app_name:
            q = [(k, v) for k, v in q if k != "application_name"] + [("application_name", app_name)]
        if options:
            old = " ".join(v for k, v in q if k == "options")
            q = [(k, v) for k, v in q if k != "options"] + [("options", (old + " " + options).strip())]
        return urlunsplit(parts._replace(query=urlencode(q, quote_via=quote)))   # libpq: %20, not '+'

    def lit(v: str) -> str:
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    extra = []
    if app_name:
        extra.append(f"application_name={lit(app_name)}")
    if options:
        extra.append(f"options={lit(options)}")
    return ((dsn or "").strip() + " " + " ".join(extra)).strip()


def redact_dsn(dsn: str) -> str:
    s = re.sub(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", r"\1***", dsn or "", flags=re.I)
    s = re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", s)
    return re.sub(r"([?&]password=)[^&]*", r"\1***", s, flags=re.I)


def _lit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


# ---------------------------------------------------------------------------------------------- scripts
_PSQL_HEAD = ["\\set QUIET on", "\\pset format unaligned", "\\pset tuples_only on", "\\pset pager off"]


def capture_script(query: str, analyze: bool = False, timeout: Optional[str] = None,
                   params: Sequence[str] = (), generic_plan: bool = False) -> str:
    if timeout and not _TIMEOUT_RE.match(timeout):
        raise ValueError(f"timeout không hợp lệ: {timeout!r} (vd. 30s, 10min, 2h)")
    L = ["\\set ON_ERROR_STOP on"] + _PSQL_HEAD
    L += [f"\\echo {MARK}meta@@",
          "SELECT json_build_object('server_version_num', current_setting('server_version_num')::int, "
          "'server_version', version(), 'current_user', current_user, 'session_user', session_user, "
          "'database', current_database(), 'application_name', current_setting('application_name'), "
          "'collected_at', now(), "
          "'extensions', (SELECT coalesce(json_agg(json_build_object('name', extname, 'version', extversion) "
          "ORDER BY extname), '[]'::json) FROM pg_extension));",
          f"\\echo {MARK}settings@@",
          "SELECT json_agg(json_build_object('name', name, 'setting', setting, 'unit', unit, 'source', source, "
          "'boot_val', boot_val, 'reset_val', reset_val, 'vartype', vartype, 'category', category, 'context', context) "
          "ORDER BY name) FROM pg_settings;",
          f"\\echo {MARK}role_settings@@",
          "SELECT coalesce(json_agg(json_build_object('role', coalesce(r.rolname, '*'), 'database', coalesce(d.datname, '*'), "
          "'config', s.setconfig) ORDER BY s.setrole, s.setdatabase), '[]'::json) "
          "FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid = s.setrole LEFT JOIN pg_database d ON d.oid = s.setdatabase "
          "WHERE s.setrole IN (0, (SELECT oid FROM pg_roles WHERE rolname = current_user)) "
          "AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()));",
          "BEGIN;"]
    target = query
    if params:
        L.append(f"PREPARE pgdoctor_q AS {query};")
        target = "EXECUTE pgdoctor_q(" + ", ".join(_lit(p) for p in params) + ")"
        if generic_plan:
            L.append("SET LOCAL plan_cache_mode = force_generic_plan;")
    L += [f"\\echo {MARK}explain@@", f"EXPLAIN (VERBOSE, SETTINGS, FORMAT JSON) {target};"]
    if analyze:
        if timeout:
            L.append(f"SET LOCAL statement_timeout = {_lit(timeout.strip())};")
        L += [f"\\echo {MARK}explain_analyze@@",
              f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) {target};"]
    L += ["ROLLBACK;", f"\\echo {MARK}end@@"]
    return "\n".join(L) + "\n"


def context_script(relations: Sequence[Tuple[str, str]], query_id: Optional[int]) -> str:
    """Read-only second session: stats of the plan's tables, hint-table rows, pg_stat_statements rows."""
    L = list(_PSQL_HEAD)
    if relations:
        vals = ", ".join(f"({_lit(s)}, {_lit(t)})" for s, t in relations)
        L += [f"\\echo {MARK}tables@@",
              "SELECT coalesce(json_agg(t ORDER BY t.size_bytes DESC), '[]'::json) FROM ("
              "SELECT n.nspname AS schema, c.relname AS name, c.relkind, c.reltuples, c.relpages, "
              "pg_total_relation_size(c.oid) AS size_bytes, c.relispartition, s.last_analyze, s.last_autoanalyze, "
              "s.last_vacuum, s.last_autovacuum, s.n_live_tup, s.n_dead_tup, s.n_mod_since_analyze, "
              "EXISTS (SELECT 1 FROM pg_stats ps WHERE ps.schemaname = n.nspname AND ps.tablename = c.relname) AS has_stats "
              "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
              "LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid "
              f"WHERE (n.nspname, c.relname) IN (VALUES {vals})) t;",
              f"\\echo {MARK}column_stats@@",
              "SELECT coalesce(json_agg(x), '[]'::json) FROM (SELECT schemaname, tablename, attname, inherited, null_frac, "
              "n_distinct, correlation, coalesce(array_length(most_common_freqs, 1), 0) AS mcv_count FROM pg_stats "
              f"WHERE (schemaname, tablename) IN (VALUES {vals})) x;",
              f"\\echo {MARK}column_targets@@",
              "SELECT coalesce(json_agg(x), '[]'::json) FROM (SELECT a.attrelid::regclass::text AS table, a.attname AS column, "
              "a.attstattarget AS target FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
              "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE a.attnum > 0 AND NOT a.attisdropped "
              f"AND coalesce(a.attstattarget, -1) >= 0 AND (n.nspname, c.relname) IN (VALUES {vals})) x;",
              f"\\echo {MARK}ext_stats@@",
              "SELECT coalesce(json_agg(x), '[]'::json) FROM (SELECT sn.nspname AS schema, s.stxname AS name, "
              "s.stxrelid::regclass::text AS table, s.stxkind::text AS kinds, EXISTS (SELECT 1 FROM pg_stats_ext e "
              "WHERE e.statistics_schemaname = sn.nspname AND e.statistics_name = s.stxname) AS has_data "
              "FROM pg_statistic_ext s JOIN pg_namespace sn ON sn.oid = s.stxnamespace "
              "JOIN pg_class c ON c.oid = s.stxrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
              f"WHERE (n.nspname, c.relname) IN (VALUES {vals})) x;"]
    L += ["SELECT to_regclass('hint_plan.hints') IS NOT NULL AS pgd_has_hints \\gset",
          "\\if :pgd_has_hints",
          f"\\echo {MARK}hints@@",
          "SELECT coalesce(json_agg(h), '[]'::json) FROM (SELECT * FROM hint_plan.hints ORDER BY id LIMIT 200) h;",
          "\\endif"]
    if query_id is not None:
        L += ["SELECT to_regclass('pg_stat_statements') IS NOT NULL AS pgd_has_pgss \\gset",
              "\\if :pgd_has_pgss",
              f"\\echo {MARK}statements@@",
              "SELECT coalesce(json_agg(x), '[]'::json) FROM (SELECT queryid, calls, total_exec_time, mean_exec_time, "
              "max_exec_time, rows, temp_blks_read, temp_blks_written, left(query, 400) AS query FROM pg_stat_statements "
              f"WHERE queryid = {int(query_id)}) x;",
              "\\endif"]
    L.append(f"\\echo {MARK}end@@")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------------------------- execution
def parse_sections(stdout: str) -> Dict[str, Any]:
    """Split psql output on the markers and JSON-decode every section (raw text kept when it is not JSON)."""
    out: Dict[str, Any] = {}
    cur: Optional[str] = None
    buf: List[str] = []

    def flush() -> None:
        if cur is None:
            return
        text = "\n".join(buf).strip()
        if not text:
            out[cur] = None
            return
        try:
            out[cur] = json.loads(text)
        except ValueError:
            out[cur] = text
    for line in stdout.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            flush()
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(line)
    flush()
    return out


def run_psql(psql_cmd: str, dsn: str, script: str, stop_on_error: bool) -> Tuple[int, str, str]:
    cmd = shlex.split(psql_cmd or "psql") + ["-X", "-q", "-v", f"ON_ERROR_STOP={1 if stop_on_error else 0}", "-f", "-"]
    if dsn:
        cmd.append(dsn)
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True)
    except FileNotFoundError as ex:
        raise RuntimeError(f"Không chạy được psql ({ex}). Cài postgresql-client hoặc dùng --psql/--old-psql/--new-psql "
                           f"(vd. \"docker exec -i <container> psql\").") from ex
    return p.returncode, p.stdout, p.stderr


def plan_relations(plan_json: Any) -> List[Tuple[str, str]]:
    rels: List[Tuple[str, str]] = []

    def walk(n: Dict[str, Any]) -> None:
        r = n.get("Relation Name")
        if r:
            key = (n.get("Schema") or "public", r)
            if key not in rels:
                rels.append(key)
        for c in n.get("Plans") or []:
            walk(c)
    obj = plan_json[0] if isinstance(plan_json, list) and plan_json else plan_json
    if isinstance(obj, dict) and isinstance(obj.get("Plan"), dict):
        walk(obj["Plan"])
    return rels


def _query_id(plan_json: Any) -> Optional[int]:
    obj = plan_json[0] if isinstance(plan_json, list) and plan_json else plan_json
    try:
        v = obj.get("Query Identifier") if isinstance(obj, dict) else None
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)


def capture(dsn: str, query: str, out_dir: str, psql_cmd: str = "psql", analyze: bool = False,
            timeout: Optional[str] = None, params: Sequence[str] = (), generic_plan: bool = False,
            app_name: Optional[str] = None, client_options: Optional[str] = None, label: str = "",
            log=None) -> Dict[str, Any]:
    """Capture one side into out_dir. Returns a summary dict (also written to out_dir/meta.json)."""
    log = log or (lambda msg: print(msg, file=sys.stderr))
    os.makedirs(out_dir, exist_ok=True)
    full_dsn = augment_dsn(dsn, app_name, client_options)
    tag = f"[{label}] " if label else ""
    log(f"{tag}EXPLAIN{' + EXPLAIN ANALYZE (timeout ' + (timeout or 'không giới hạn') + ')' if analyze else ''} …")
    rc, so, se = run_psql(psql_cmd, full_dsn, capture_script(query, analyze, timeout, params, generic_plan), True)
    sec = parse_sections(so)
    with open(os.path.join(out_dir, "query.sql"), "w", encoding="utf-8") as fh:
        fh.write(query + ";\n")
    with open(os.path.join(out_dir, "psql.stderr.txt"), "w", encoding="utf-8") as fh:
        fh.write(se)
    if not isinstance(sec.get("meta"), dict) or not isinstance(sec.get("settings"), list):
        raise RuntimeError(f"{tag}psql thất bại (exit {rc}): {se.strip()[:800] or so.strip()[:800]}")
    if not isinstance(sec.get("explain"), list):
        raise RuntimeError(f"{tag}EXPLAIN thất bại (exit {rc}): {se.strip()[:800]}")
    meta = dict(sec["meta"])
    analyze_error = None
    if analyze and not isinstance(sec.get("explain_analyze"), list):
        analyze_error = re.sub(r"^psql:[^:]*:\d+:\s*", "", (se.strip().splitlines() or [f"psql exit {rc}"])[-1])
        log(f"{tag}EXPLAIN ANALYZE không hoàn tất: {analyze_error} → dùng plan EXPLAIN (không ANALYZE)")
    meta.update({"label": label, "dsn": redact_dsn(full_dsn), "psql": redact_dsn(re.sub(r"(PGPASSWORD=)\S+", r"\1***", psql_cmd or "")),
                 "analyze_requested": analyze,
                 "timeout": timeout, "params": list(params), "generic_plan": generic_plan,
                 "client_options": client_options, "analyze_error": analyze_error})
    _write_json(os.path.join(out_dir, "settings.json"),
                {"server_version_num": meta.get("server_version_num"), "settings": sec["settings"]})
    _write_json(os.path.join(out_dir, "role_settings.json"), sec.get("role_settings") or [])
    _write_json(os.path.join(out_dir, "plan.json"), sec["explain"])
    if isinstance(sec.get("explain_analyze"), list):
        _write_json(os.path.join(out_dir, "plan_analyze.json"), sec["explain_analyze"])

    qid = _query_id(sec["explain"])
    rels = plan_relations(sec["explain"])
    rc2, so2, se2 = run_psql(psql_cmd, full_dsn, context_script(rels, qid), False)
    ctx = parse_sections(so2)
    dst = next((s.get("setting") for s in sec["settings"] if s.get("name") == "default_statistics_target"), None)
    health = {"server_version_num": meta.get("server_version_num"), "database": meta.get("database"),
              "default_statistics_target": dst, "scope": "plan tables",
              "tables": _list(ctx.get("tables")), "column_stats": _list(ctx.get("column_stats")),
              "column_targets": _list(ctx.get("column_targets")), "ext_stats": _list(ctx.get("ext_stats"))}
    _write_json(os.path.join(out_dir, "health.json"), health)
    hints = _list(ctx.get("hints"))
    if qid is not None:
        hints = [h for h in hints if "query_id" not in h or str(h.get("query_id")) == str(qid)]
    extras = {"query_id": qid, "relations": [f"{s}.{t}" for s, t in rels], "hints": hints,
              "has_hint_table": "hints" in ctx, "statements": _list(ctx.get("statements")),
              "context_stderr": se2.strip()[:2000]}
    _write_json(os.path.join(out_dir, "extras.json"), extras)
    _write_json(os.path.join(out_dir, "meta.json"), meta)
    return meta


def _list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


# ------------------------------------------------------------------------------------------------ loading
def load_capture(d: str) -> Dict[str, Any]:
    """Load a directory written by capture()."""
    def rd(name: str, default: Any = None) -> Any:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            return default
        with open(p, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    meta = rd("meta.json")
    if not isinstance(meta, dict):
        raise ValueError(f"{d}: không có meta.json – thư mục này có phải output của 'pgdoctor capture' không?")
    plan_file = "plan_analyze.json" if os.path.isfile(os.path.join(d, "plan_analyze.json")) else "plan.json"
    return {"dir": d, "meta": meta, "settings_path": os.path.join(d, "settings.json"),
            "role_settings": rd("role_settings.json", []), "plan_path": os.path.join(d, plan_file),
            "explain_only_path": os.path.join(d, "plan.json"), "analyzed": plan_file == "plan_analyze.json",
            "health_path": os.path.join(d, "health.json") if os.path.isfile(os.path.join(d, "health.json")) else None,
            "extras": rd("extras.json", {}) or {}}


def major_of(meta: Dict[str, Any]) -> Optional[int]:
    try:
        return int(meta.get("server_version_num")) // 10000
    except (TypeError, ValueError):
        return None
