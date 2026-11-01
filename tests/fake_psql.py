"""Stand-in for psql in tests: replays a directory written by 'pgdoctor capture' in psql's marker format.

    python fake_psql.py <capture_dir> -X -q -v ON_ERROR_STOP=1 -f - <dsn>      (script on stdin)

If the script asks for EXPLAIN ANALYZE and the directory has no plan_analyze.json, it behaves like a statement_timeout:
the error goes to stderr and the exit code is 3, as with psql -v ON_ERROR_STOP=1."""
import json
import os
import sys

MARK = "@@PGDOCTOR "


def rd(d, name, default=None):
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def sec(name, obj):
    print(f"{MARK}{name}@@")
    print(json.dumps(obj, ensure_ascii=False))


def main():
    d = sys.argv[1]
    script = sys.stdin.read()
    if f"{MARK}meta@@" in script:
        meta = rd(d, "meta.json")
        sec("meta", {k: meta.get(k) for k in ("server_version_num", "server_version", "current_user", "session_user",
                                              "database", "application_name", "collected_at", "extensions")})
        sec("settings", rd(d, "settings.json")["settings"])
        sec("role_settings", rd(d, "role_settings.json", []))
        sec("explain", rd(d, "plan.json"))
        if f"{MARK}explain_analyze@@" in script:
            pa = rd(d, "plan_analyze.json")
            if pa is None:
                print(f"{MARK}explain_analyze@@")
                sys.stdout.flush()
                print("psql:<stdin>:45: ERROR:  canceling statement due to statement timeout", file=sys.stderr)
                return 3
            sec("explain_analyze", pa)
        print(f"{MARK}end@@")
        return 0
    h = rd(d, "health.json", {})
    ex = rd(d, "extras.json", {})
    for k in ("tables", "column_stats", "column_targets", "ext_stats"):
        if f"{MARK}{k}@@" in script:
            sec(k, h.get(k) or [])
    if f"{MARK}hints@@" in script and ex.get("has_hint_table"):
        sec("hints", ex.get("hints") or [])
    if f"{MARK}statements@@" in script:
        sec("statements", ex.get("statements") or [])
    print(f"{MARK}end@@")
    return 0


if __name__ == "__main__":
    sys.exit(main())
