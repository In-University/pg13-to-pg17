"""Stand-in for psql in planviz tests: prints a stored EXPLAIN JSON the way 'psql -A -t' would.

    python fake_psql_plan.py <plan.json> -X -q -A -t -v ON_ERROR_STOP=1 -f - [dsn]    (script on stdin)

The script received on stdin is copied to $PLANVIZ_FAKE_SCRIPT (if set) so tests can inspect it.  If the plan file
does not exist it fails like a statement_timeout (exit 3)."""
import json
import os
import sys


def main() -> int:
    plan = sys.argv[1]
    script = sys.stdin.read()
    dump = os.environ.get("PLANVIZ_FAKE_SCRIPT")
    if dump:
        with open(dump, "w", encoding="utf-8") as fh:
            fh.write(script + "\n-- argv: " + json.dumps(sys.argv[2:]))
    if not os.path.isfile(plan):
        print("psql:<stdin>:3: ERROR:  canceling statement due to statement timeout", file=sys.stderr)
        return 3
    with open(plan, encoding="utf-8") as fh:
        obj = json.load(fh)
    print(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
