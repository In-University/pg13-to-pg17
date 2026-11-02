// Helper for tests/test_planviz.py: run planviz/assets/core.js on plan files and print the analysis as JSON.
//   node tests/planviz_node.js analyze <file> [<file2>]      -> {"plans": [...], "pairs": [[a,b],...] | null}
//   node tests/planviz_node.js parse <file>                  -> normalised plan object
const fs = require("fs");
const path = require("path");
const core = require(path.join(__dirname, "..", ".claude", "skills", "planviz", "planviz", "assets", "core.js"));
const [cmd, ...files] = process.argv.slice(2);
const parsed = files.map((f) => core.parseAny(fs.readFileSync(f, "utf8")));
if (cmd === "parse") {
  process.stdout.write(JSON.stringify(parsed[0].obj));
} else {
  const plans = parsed.map((p, i) => core.analyze(p.obj, "p" + i, {}));
  const pairs = plans.length === 2 ? core.match(plans[0], plans[1]) : null;
  process.stdout.write(JSON.stringify({ plans, pairs }));
}
