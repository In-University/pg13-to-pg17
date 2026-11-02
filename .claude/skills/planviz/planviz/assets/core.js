/* planviz core: parse EXPLAIN output (JSON or text) and analyse it.
 * JavaScript port of planviz/loader.py + textplan.py + analyze.py so the HTML page can analyse plans pasted in the
 * browser.  Keep the two in step: tests/test_planviz.py runs both on the sample plans and compares the results.
 * Works in the browser (window.PlanvizCore) and in node (module.exports). */
(function (root) {
  "use strict";
  var BUF_KEYS = ["Shared Hit Blocks", "Shared Read Blocks", "Shared Dirtied Blocks", "Shared Written Blocks",
    "Local Hit Blocks", "Local Read Blocks", "Local Dirtied Blocks", "Local Written Blocks",
    "Temp Read Blocks", "Temp Written Blocks"];
  var JOIN_TYPES = ["Nested Loop", "Hash Join", "Merge Join"];
  var SCAN_TYPES = ["Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan", "CTE Scan",
    "Subquery Scan", "Function Scan", "Values Scan", "Tid Scan", "Tid Range Scan", "Foreign Scan",
    "WorkTable Scan", "Sample Scan", "Table Function Scan", "Named Tuplestore Scan", "Custom Scan"];
  var UNION_TYPES = ["Append", "Merge Append", "Recursive Union", "BitmapAnd", "BitmapOr"];
  var RESCAN_COSTLY = ["Seq Scan", "CTE Scan", "Materialize", "Subquery Scan", "Function Scan", "Values Scan", "Sort",
    "Aggregate", "Hash Join", "Merge Join", "Nested Loop", "WorkTable Scan", "Bitmap Heap Scan",
    "Append", "Result", "Gather", "Unique", "SetOp", "WindowAgg"];
  var COND_KEYS = ["Hash Cond", "Merge Cond", "Join Filter", "Index Cond", "Recheck Cond", "Filter", "Group Key",
    "TID Cond", "One-Time Filter"];
  var SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
  var BLOCK = 8192;

  function has(a, x) { return a.indexOf(x) >= 0; }
  function num(v) { var f = parseFloat(v); return isFinite(f) ? f : 0; }
  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }

  // ------------------------------------------------------------------ formatting (mirrors analyze.py)
  function pyRound(x) { // Python 3 round(): half to even
    var r = Math.round(x);
    if (Math.abs(x % 1) === 0.5 && r % 2 !== 0) r -= 1;
    return r;
  }
  function fixed(x, d) { return x.toFixed(d); }
  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "–";
    if (ms < 1) return fixed(ms, 3) + " ms";
    if (ms < 10) return fixed(ms, 2) + " ms";
    if (ms < 1000) return fixed(ms, 0) + " ms";
    var s = ms / 1000;
    if (s < 60) return s < 10 ? fixed(s, 1) + " s" : fixed(s, 0) + " s";
    var tot = pyRound(s), m = Math.floor(tot / 60); s = tot % 60;
    if (m < 60) return m + "m " + (s < 10 ? "0" : "") + s + "s";
    var h = Math.floor(m / 60); m = m % 60;
    return h + "h " + (m < 10 ? "0" : "") + m + "m";
  }
  function fmtNum(v) {
    if (v === null || v === undefined) return "–";
    var a = Math.abs(v), units = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) {
        var x = v / units[i][0];
        return (Math.abs(x) < 100 ? fixed(x, 1) : fixed(x, 0)) + units[i][1];
      }
    }
    if (a === Math.floor(a)) return String(v);
    return fixed(v, 2).replace(/0+$/, "").replace(/\.$/, "");
  }
  function fmtBytes(b) {
    if (b === null || b === undefined) return "–";
    var units = [[1073741824, "GB"], [1048576, "MB"], [1024, "kB"]];
    for (var i = 0; i < units.length; i++) {
      if (b >= units[i][0]) { var x = b / units[i][0]; return (x < 100 ? fixed(x, 1) : fixed(x, 0)) + " " + units[i][1]; }
    }
    return fixed(b, 0) + " B";
  }
  function fmtFactor(lq) {
    if (lq === null || lq === undefined) return "–";
    var f = Math.pow(10, Math.abs(lq));
    if (f < 1.5) return "≈1×";
    var txt = fmtNum(f < 10 ? Math.round(f * 10) / 10 : pyRound(f));
    return (lq > 0 ? "↑" : "↓") + txt + "×";
  }
  function parseMemKb(v) {
    if (v === null || v === undefined) return null;
    var m = /^\s*([\d.]+)\s*(B|kB|MB|GB|TB)?\s*$/.exec(String(v));
    if (!m) return null;
    return parseFloat(m[1]) * { B: 1 / 1024, kB: 1, MB: 1024, GB: 1048576, TB: 1073741824 }[m[2] || "kB"];
  }

  // ------------------------------------------------------------------ text EXPLAIN parser (port of textplan.py)
  var NODE_RE = /^(\s*)(?:->\s+)?(.+?)\s+\(cost=([\d.]+)\.\.([\d.]+)\s+rows=([\d.]+)\s+width=(\d+)\)(?:\s+\((?:actual\s+)?(?:time=([\d.]+)\.\.([\d.]+)\s+)?rows=([\d.]+)\s+loops=(\d+)\)|\s+\((never executed)\))?\s*$/;
  var SUBPLAN_RE = /^(\s*)(CTE|InitPlan|SubPlan)\s*(.*?)\s*$/;
  var JOIN_RE = /^(Parallel\s+)?(Hash|Merge)\s+(?:(Left Anti|Left Semi|Right Anti|Right Semi|Left|Right|Full|Semi|Anti)\s+)?Join$/;
  var NL_RE = /^Nested Loop(?:\s+(Left Anti|Left Semi|Right Anti|Right Semi|Left|Right|Full|Semi|Anti)\s+Join)?$/;
  var AGG_RE = /^(?:(Partial|Finalize)\s+)?(HashAggregate|GroupAggregate|MixedAggregate|Aggregate)$/;
  var AGG_STRATEGY = { HashAggregate: "Hashed", GroupAggregate: "Sorted", MixedAggregate: "Mixed", Aggregate: "Plain" };
  var MODIFY_OPS = ["Insert", "Update", "Delete", "Merge"];

  function kb(val) {
    var m = /(-?[\d.]+)\s*(kB|MB|GB|B)?/.exec(val);
    if (!m) return null;
    return parseFloat(m[1]) * { B: 1 / 1024, kB: 1, MB: 1024, GB: 1048576 }[m[2] || "kB"];
  }
  function parseBody(body) {
    var d = {}, rel = null, alias = null, idx = null, head = body, rest = null;
    var m = /^(.*?)\s+using\s+(\S+)(?:\s+on\s+(.+))?$/.exec(body);
    if (m && m[1].indexOf("Index") >= 0) { head = m[1]; idx = m[2]; rest = m[3] || null; }
    else { var m2 = /^(.*?)\s+on\s+(.+)$/.exec(body); if (m2) { head = m2[1]; rest = m2[2]; } }
    if (rest) { var toks = rest.split(/\s+/); rel = toks[0]; alias = toks.length > 1 ? toks[1] : null; }
    head = head.trim();
    if (head.indexOf("Parallel ") === 0 && !JOIN_RE.test(head)) { d["Parallel Aware"] = true; head = head.slice(9); }
    var jm, am;
    if (NL_RE.test(head)) { d["Node Type"] = "Nested Loop"; d["Join Type"] = NL_RE.exec(head)[1] || "Inner"; }
    else if ((jm = JOIN_RE.exec(head))) {
      d["Node Type"] = jm[2] + " Join"; d["Join Type"] = jm[3] || "Inner"; if (jm[1]) d["Parallel Aware"] = true;
    } else if ((am = AGG_RE.exec(head))) {
      d["Node Type"] = "Aggregate"; d["Strategy"] = AGG_STRATEGY[am[2]]; d["Partial Mode"] = am[1] || "Simple";
    } else if (head === "HashSetOp" || head === "SetOp") {
      d["Node Type"] = "SetOp"; d["Strategy"] = head === "HashSetOp" ? "Hashed" : "Sorted";
    } else if (has(MODIFY_OPS, head)) { d["Node Type"] = "ModifyTable"; d["Operation"] = head; }
    else d["Node Type"] = head;
    var nt = d["Node Type"];
    if (idx) d["Index Name"] = idx;
    if (rel) {
      if (nt === "CTE Scan") d["CTE Name"] = rel;
      else if (nt === "Function Scan") d["Function Name"] = rel.split("(")[0];
      else if (has(["Subquery Scan", "Values Scan", "WorkTable Scan", "Named Tuplestore Scan", "Table Function Scan"], nt)) { /* alias only */ }
      else {
        var parts = rel.split(".");
        d["Relation Name"] = parts[parts.length - 1];
        if (rel.indexOf(".") >= 0) d["Schema"] = parts[0];
      }
      var p2 = rel.split(".");
      d["Alias"] = alias || p2[p2.length - 1];
    }
    return d;
  }
  function splitKv(text) {
    var out = [];
    text.trim().split(/\s{2,}/).forEach(function (p) {
      var i = p.indexOf(":");
      if (i >= 0) out.push([p.slice(0, i).trim(), p.slice(i + 1).trim()]);
    });
    return out;
  }
  function splitTopCommas(s) {
    var out = [], depth = 0, cur = "";
    for (var i = 0; i < s.length; i++) {
      var ch = s[i];
      if (ch === "(" || ch === "[") depth++;
      else if (ch === ")" || ch === "]") depth--;
      if (ch === "," && depth === 0) { out.push(cur); cur = ""; } else cur += ch;
    }
    if (cur.trim()) out.push(cur);
    return out;
  }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
  function applyAttr(node, key, val) {
    var nt = node["Node Type"], m, re;
    if (key === "Buffers") {
      re = /(shared|local|temp)\s+([^,]+)/g;
      while ((m = re.exec(val))) {
        var re2 = /(hit|read|dirtied|written)=(\d+)/g, m2;
        while ((m2 = re2.exec(m[2]))) node[cap(m[1]) + " " + cap(m2[1]) + " Blocks"] = parseInt(m2[2], 10);
      }
      return;
    }
    if (key === "I/O Timings") {
      m = /(?:shared\s+)?read=([\d.]+)/.exec(val); if (m) node["I/O Read Time"] = parseFloat(m[1]);
      m = /(?:shared\s+)?write=([\d.]+)/.exec(val); if (m) node["I/O Write Time"] = parseFloat(m[1]);
      return;
    }
    if (key === "Buckets") {
      m = /^(\d+)(?:\s+\(originally\s+(\d+)\))?/.exec(val);
      if (m) { node["Hash Buckets"] = parseInt(m[1], 10); node["Original Hash Buckets"] = parseInt(m[2] || m[1], 10); }
      return;
    }
    if (key === "Batches") {
      m = /^(\d+)(?:\s+\(originally\s+(\d+)\))?/.exec(val);
      if (m) {
        if (nt === "Hash") { node["Hash Batches"] = parseInt(m[1], 10); node["Original Hash Batches"] = parseInt(m[2] || m[1], 10); }
        else node["HashAgg Batches"] = parseInt(m[1], 10);
      }
      return;
    }
    if (key === "Memory Usage" || key === "Memory" || key === "Peak Memory Usage") {
      if (nt === "Sort" || nt === "Incremental Sort") { node["Sort Space Used"] = kb(val); node["Sort Space Type"] = "Memory"; }
      else node["Peak Memory Usage"] = kb(val);
      return;
    }
    if (key === "Disk Usage" || key === "Disk") {
      if (nt === "Sort" || nt === "Incremental Sort") { node["Sort Space Used"] = kb(val); node["Sort Space Type"] = "Disk"; }
      else node["Disk Usage"] = kb(val);
      return;
    }
    if (has(["Hits", "Misses", "Evictions", "Overflows"], key)) { node["Cache " + key] = Math.trunc(parseFloat(val)); return; }
    if (has(["Rows Removed by Filter", "Rows Removed by Join Filter", "Rows Removed by Index Recheck", "Heap Fetches",
      "Workers Planned", "Workers Launched", "Planned Partitions", "Pre-sorted Groups", "Full-sort Groups", "Heap Blocks"], key)) {
      var f = parseFloat(val.split(/\s+/)[0]);
      if (isNaN(f)) node[key] = val;
      else node[key] = key.indexOf("Rows Removed") === 0 ? f : Math.trunc(f);
      return;
    }
    if (has(["Sort Key", "Group Key", "Presorted Key", "Cache Key", "Hash Key"], key)) {
      node[key] = splitTopCommas(val).map(function (x) { return x.trim(); });
      return;
    }
    node[key] = val;
  }
  function indentOf(ln) { return ln.length - ln.replace(/^\s+/, "").length; }

  function parseTextPlan(text) {
    var lines = text.split(/\r?\n/).map(function (l) { return l.replace(/\s+$/, ""); }).filter(function (l) { return l.trim(); });
    var rootNode = null, stack = [], meta = {}, pendingRel = null, section = null, jit = {}, triggers = [];
    var first = null;
    for (var i = 0; i < lines.length; i++) if (NODE_RE.test(lines[i])) { first = lines[i]; break; }
    if (first === null) first = lines.length ? lines[0] : "";
    var baseIndent = indentOf(first);
    lines.forEach(function (ln) {
      var stripped = ln.trim(), indNow = indentOf(ln), top = indNow <= baseIndent, m;
      if ((m = /^Planning Time:\s*([\d.]+)\s*ms/.exec(stripped)) && top) { meta["Planning Time"] = parseFloat(m[1]); section = null; return; }
      if ((m = /^Execution Time:\s*([\d.]+)\s*ms/.exec(stripped)) && top) { meta["Execution Time"] = parseFloat(m[1]); section = null; return; }
      if (stripped.indexOf("Total runtime:") === 0) { meta["Execution Time"] = parseFloat(stripped.match(/[\d.]+/)[0]); return; }
      if (stripped === "JIT:" && top) { section = "JIT"; return; }
      if (section === "JIT" && !top) {
        if (stripped.indexOf("Functions:") === 0) jit["Functions"] = parseInt(stripped.split(":")[1], 10);
        else if (stripped.indexOf("Options:") === 0) {
          var o = {};
          stripped.slice(stripped.indexOf(":") + 1).split(",").forEach(function (p) {
            p = p.trim(); var k = p.lastIndexOf(" "); o[p.slice(0, k).trim()] = p.slice(k + 1).trim() === "true";
          });
          jit["Options"] = o;
        } else if (stripped.indexOf("Timing:") === 0) {
          var t = {}, re = /(Generation|Inlining|Optimization|Emission|Total)\s+([\d.]+)\s*ms/g, mm;
          while ((mm = re.exec(stripped))) t[mm[1]] = parseFloat(mm[2]);
          jit["Timing"] = t;
        }
        return;
      }
      if ((m = /^Trigger\s+(.+?):\s+time=([\d.]+)\s+calls=(\d+)/.exec(stripped)) && top) {
        var cm = /for constraint (\S+)/.exec(m[1]);
        triggers.push({ "Trigger Name": m[1], "Constraint Name": cm ? cm[1] : null, "Time": parseFloat(m[2]), "Calls": parseInt(m[3], 10) });
        return;
      }
      if ((m = /^Query Identifier:\s*(-?\d+)/.exec(stripped)) && top) { meta["Query Identifier"] = m[1]; section = null; return; }
      if (stripped === "Planning:" && top) { section = "Planning"; meta["Planning"] = meta["Planning"] || {}; return; }
      if (section === "Planning" && !top) {
        if (stripped.indexOf("Buffers:") === 0) applyAttr(meta["Planning"], "Buffers", stripped.slice(stripped.indexOf(":") + 1).trim());
        else if (stripped.indexOf("Memory:") === 0) {
          var rm = /(used|allocated)=([\d.]+\s*[kMG]?B)/g, x;
          while ((x = rm.exec(stripped))) meta["Planning"]["Memory " + cap(x[1])] = kb(x[2]);
        }
        return;
      }
      if (stripped.indexOf("Serialization:") === 0 && top) { meta["Serialization"] = stripped.slice(stripped.indexOf(":") + 1).trim(); section = null; return; }
      if (stripped.indexOf("Settings:") === 0 && top) {
        var sett = {}, rs = /(\w+)\s*=\s*'([^']*)'/g, sm;
        while ((sm = rs.exec(stripped))) sett[sm[1]] = sm[2];
        meta["Settings"] = sett;
        return;
      }
      section = null;
      var nm = NODE_RE.exec(ln);
      if (nm) {
        var indent = indNow, node = parseBody(nm[2].trim());
        node["Startup Cost"] = parseFloat(nm[3]); node["Total Cost"] = parseFloat(nm[4]);
        node["Plan Rows"] = parseFloat(nm[5]); node["Plan Width"] = parseInt(nm[6], 10);
        if (nm[10] !== undefined) {
          if (nm[7] !== undefined) { node["Actual Startup Time"] = parseFloat(nm[7]); node["Actual Total Time"] = parseFloat(nm[8]); }
          node["Actual Rows"] = parseFloat(nm[9]); node["Actual Loops"] = parseInt(nm[10], 10);
        } else if (nm[11]) { node["Actual Rows"] = 0; node["Actual Loops"] = 0; node["Actual Total Time"] = 0; }
        node["Plans"] = [];
        if (rootNode === null) { rootNode = node; stack = [[indent, node]]; return; }
        while (stack.length && stack[stack.length - 1][0] >= indent) stack.pop();
        var parent = stack.length ? stack[stack.length - 1][1] : rootNode;
        if (pendingRel && pendingRel[0] < indent) {
          node["Parent Relationship"] = pendingRel[1];
          if (pendingRel[2]) node["Subplan Name"] = pendingRel[2];
          pendingRel = null;
        } else {
          var sibs = parent["Plans"].filter(function (c) { return !has(["InitPlan", "SubPlan"], c["Parent Relationship"]); });
          if (has(JOIN_TYPES, parent["Node Type"])) node["Parent Relationship"] = sibs.length ? "Inner" : "Outer";
          else if (parent["Node Type"] === "Append" || parent["Node Type"] === "Merge Append") node["Parent Relationship"] = "Member";
          else if (parent["Node Type"] === "Subquery Scan") node["Parent Relationship"] = "Subquery";
          else node["Parent Relationship"] = "Outer";
        }
        parent["Plans"].push(node);
        stack.push([indent, node]);
        return;
      }
      var sp = SUBPLAN_RE.exec(ln);
      if (sp && stack.length && !/^\s*(CTE Scan)/.test(ln) && stripped.split(" ")[0].indexOf(":") < 0) {
        var kind = sp[2], name = sp[3], relk = (kind === "CTE" || kind === "InitPlan") ? "InitPlan" : "SubPlan";
        var subName = kind === "CTE" ? "CTE " + name : (kind + " " + name).trim();
        pendingRel = [sp[1].length, relk, subName];
        return;
      }
      if (stack.length && stripped.indexOf(":") >= 0) {
        var ind = indentOf(ln), target = null;
        for (var j = stack.length - 1; j >= 0; j--) if (stack[j][0] < ind) { target = stack[j][1]; break; }
        target = target || stack[stack.length - 1][1];
        var kvs = splitKv(stripped), firstKey = stripped.split(":")[0];
        if (kvs.length > 1 && has(["Buckets", "Hits", "Batches", "Sort Method", "Planned Partitions", "Full-sort Groups", "Pre-sorted Groups"], firstKey)) {
          kvs.forEach(function (kv) { applyAttr(target, kv[0], kv[1]); if (kv[0] === "Sort Method") target["Sort Method"] = kv[1]; });
        } else {
          var ci = stripped.indexOf(":"), k = stripped.slice(0, ci).trim(), v = stripped.slice(ci + 1).trim();
          if (k === "Sort Method") {
            var s2 = /^(.+?)\s{2,}(Memory|Disk):\s*(.+)$/.exec(v);
            if (s2) { target["Sort Method"] = s2[1].trim(); target["Sort Space Type"] = s2[2]; target["Sort Space Used"] = kb(s2[3]); }
            else target["Sort Method"] = v;
          } else if (k === "Output") target["Output"] = splitTopCommas(v).map(function (x) { return x.trim(); });
          else if (k.indexOf("Worker ") === 0) (target["Extra"] = target["Extra"] || []).push(stripped);
          else applyAttr(target, k, v);
        }
        return;
      }
      if (stack.length) { var last = stack[stack.length - 1][1]; (last["Extra"] = last["Extra"] || []).push(stripped); }
    });
    if (rootNode === null) throw new Error("Không tìm thấy dòng node nào của plan – đây có phải output của EXPLAIN (có COSTS) không?");
    if (Object.keys(jit).length) meta["JIT"] = jit;
    if (triggers.length) meta["Triggers"] = triggers;
    var out = { "Plan": rootNode };
    Object.keys(meta).forEach(function (k) { out[k] = meta[k]; });
    return out;
  }

  // ------------------------------------------------------------------ input normalisation (port of loader.py)
  function stripPsql(text) {
    var out = [];
    text.split(/\r?\n/).forEach(function (ln) {
      var s = ln.replace(/\s+$/, "");
      if (/^\s*(SET|BEGIN|ROLLBACK|COMMIT|START TRANSACTION|RESET|DISCARD ALL|EXPLAIN)\s*;?\s*$/.test(s) || /^Time:\s*[\d.]+\s*ms/.test(s.trim())) return;
      if (/^\s*QUERY PLAN\s*$/.test(s) || /^\s*-{5,}\s*$/.test(s) || /^\(\d+ rows?\)$/.test(s.trim())) return;
      if (s.slice(-1) === "+") s = s.slice(0, -1).replace(/\s+$/, "");
      out.push(s);
    });
    return out.join("\n");
  }
  function normaliseJson(obj) {
    if (Array.isArray(obj)) { if (!obj.length) throw new Error("JSON EXPLAIN rỗng"); obj = obj[0]; }
    if (!obj || typeof obj !== "object") throw new Error("Cấu trúc JSON không phải EXPLAIN");
    if (!own(obj, "Plan")) {
      if (own(obj, "Node Type")) return { "Plan": obj };
      if (own(obj, "QUERY PLAN")) return normaliseJson(obj["QUERY PLAN"]);
      throw new Error("JSON không có khoá 'Plan' – có phải EXPLAIN (FORMAT JSON) không?");
    }
    return obj;
  }
  function parseAny(text) {
    var raw = text.trim().replace(/^﻿/, ""), cand = stripPsql(raw), blobs = [raw, cand];
    for (var b = 0; b < blobs.length; b++) {
      var blob = blobs[b], starts = [blob.indexOf("["), blob.indexOf("{")].filter(function (i) { return i >= 0; });
      if (!starts.length) continue;
      var start = Math.min.apply(null, starts), end = Math.max(blob.lastIndexOf("]"), blob.lastIndexOf("}"));
      var chunks = [blob.slice(start)];
      if (end > start) chunks.push(blob.slice(start, end + 1));
      for (var c = 0; c < chunks.length; c++) {
        try {
          var o = normaliseJson(JSON.parse(chunks[c]));
          // 64-bit query ids lose precision as JS numbers: keep the digits as text
          var qm = /"Query Identifier"\s*:\s*(-?\d+)/.exec(chunks[c]);
          if (qm) o["Query Identifier"] = qm[1];
          return { obj: o, fmt: "json" };
        } catch (e) { /* try next */ }
      }
    }
    var lines = cand.split("\n");
    for (var i = 0; i < lines.length; i++) {
      if (/\(cost=[\d.]+\.\.[\d.]+ rows=/.test(lines[i])) { cand = lines.slice(i).join("\n"); break; }
    }
    return { obj: parseTextPlan(cand.replace(/\t/g, "  ")), fmt: "text" };
  }

  // ------------------------------------------------------------------ labels
  function nodeLabel(r) {
    var nt = r["Node Type"] || "?";
    if (nt === "Aggregate") {
      var base = { Plain: "Aggregate", Sorted: "GroupAggregate", Hashed: "HashAggregate", Mixed: "MixedAggregate" }[r["Strategy"] || "Plain"] || "Aggregate";
      var pm = r["Partial Mode"] || "Simple";
      return pm === "Simple" ? base : pm + " " + base;
    }
    if (nt === "SetOp") return r["Strategy"] === "Hashed" ? "HashSetOp" : "SetOp";
    if (has(JOIN_TYPES, nt)) {
      var jt = r["Join Type"] || "Inner", lbl;
      if (nt === "Nested Loop") lbl = jt === "Inner" ? "Nested Loop" : "Nested Loop " + jt + " Join";
      else { var b = nt.replace(" Join", ""); lbl = jt === "Inner" ? b + " Join" : b + " " + jt + " Join"; }
      return r["Parallel Aware"] ? "Parallel " + lbl : lbl;
    }
    if (nt === "ModifyTable") return r["Operation"] || "Modify";
    return r["Parallel Aware"] ? "Parallel " + nt : nt;
  }
  function nodeObject(r) {
    var rel = r["Relation Name"], alias = r["Alias"], cte = r["CTE Name"];
    if (cte) return cte + (alias && alias !== cte ? " " + alias : "");
    if (rel) {
      var name = r["Schema"] && r["Schema"] !== "public" ? r["Schema"] + "." + rel : rel;
      return name + (alias && alias !== rel ? " " + alias : "");
    }
    if (r["Function Name"]) return r["Function Name"] + "()" + (alias && alias !== r["Function Name"] ? " " + alias : "");
    if (alias && has(["Subquery Scan", "Values Scan", "WorkTable Scan", "Table Function Scan"], r["Node Type"])) return alias;
    if (r["Index Name"] && r["Node Type"] === "Bitmap Index Scan") return r["Index Name"];
    return "";
  }
  function target(r) {
    if (r["CTE Name"]) return "cte:" + r["CTE Name"] + "/" + (r["Alias"] || "");
    if (r["Relation Name"]) return r["Relation Name"] + "/" + (r["Alias"] || "");
    if (r["Function Name"]) return "fn:" + r["Function Name"] + "/" + (r["Alias"] || "");
    if (r["Node Type"] === "Bitmap Index Scan") return "idx:" + (r["Index Name"] || "");
    if (has(["Subquery Scan", "Values Scan", "WorkTable Scan"], r["Node Type"]) && r["Alias"]) return "q:" + r["Alias"];
    return "";
  }

  // ------------------------------------------------------------------ analysis (port of analyze.analyze)
  function analyze(obj, label, meta) {
    meta = meta || {};
    var nodes = [];
    function build(raw, parent, depth) {
      var r = {};
      Object.keys(raw).forEach(function (k) { if (k !== "Plans") r[k] = raw[k]; });
      var id = nodes.length, n = {
        id: id, parent: parent, depth: depth, children: [], raw: r, type: r["Node Type"] || "?", label: nodeLabel(r),
        object: nodeObject(r), index: r["Index Name"] || "", rel: r["Parent Relationship"] || "", sub: r["Subplan Name"] || "",
        est: num(r["Plan Rows"]), width: Math.trunc(num(r["Plan Width"])), cost: num(r["Total Cost"]),
        startup_cost: num(r["Startup Cost"]), analyzed: own(r, "Actual Loops")
      };
      nodes.push(n);
      (raw["Plans"] || []).forEach(function (ch) { n.children.push(build(ch, id, depth + 1)); });
      return id;
    }
    build(obj["Plan"], null, 0);
    var root = nodes[0], analyzed = root.analyzed;
    var hasTiming = analyzed && nodes.some(function (n) { return n.analyzed && own(n.raw, "Actual Total Time"); });

    nodes.forEach(function (n) {
      var r = n.raw;
      n.loops = n.analyzed ? num(r["Actual Loops"]) : null;
      n.never = !!(n.analyzed && n.loops === 0);
      n.rows = n.analyzed ? num(r["Actual Rows"]) : null;
      var lp = n.analyzed ? Math.max(n.loops || 0, 1) : 1;
      n.est_total = n.est * lp;
      n.act_total = n.analyzed ? (n.rows || 0) * (n.loops || 0) : null;
      n.lq = (n.analyzed && !n.never && n.type !== "ModifyTable") ? Math.log10(Math.max(n.rows, 1) / Math.max(n.est, 1)) : null;
      var removed = num(r["Rows Removed by Filter"]) + num(r["Rows Removed by Join Filter"]) + num(r["Rows Removed by Index Recheck"]);
      n.removed = n.analyzed ? removed * (n.loops || 0) : null;
    });
    function ancestorGather(n) {
      var p = n.parent;
      while (p !== null) { if (nodes[p].type === "Gather" || nodes[p].type === "Gather Merge") return nodes[p]; p = nodes[p].parent; }
      return null;
    }
    nodes.forEach(function (n) {
      var t = n.raw["Actual Total Time"];
      if (!n.analyzed || t === undefined || t === null) { n.incl = null; return; }
      if (n.never) { n.incl = 0; return; }
      var g = ancestorGather(n), div = 1;
      if (g) {
        var w = num(own(g.raw, "Workers Launched") ? g.raw["Workers Launched"] : g.raw["Workers Planned"]);
        div = Math.max(1, Math.min(n.loops, w + 1));
      }
      n.incl = parseFloat(t) * n.loops / div;
    });
    function isCteInit(c) { return c.rel === "InitPlan" && c.sub.indexOf("CTE ") === 0; }
    var cteRoot = {};
    nodes.forEach(function (c) { if (isCteInit(c)) cteRoot[c.sub.slice(4)] = c; });
    var charged = {}, timeParent = {};
    nodes.forEach(function (x) {
      timeParent[x.id] = x.parent;
      if (x.rel !== "InitPlan" || isCteInit(x) || x.parent === null) return;
      var toks = initTokens(x.sub), u = toks.length ? firstUser(nodes, nodes[x.parent], x, toks) : null;
      if (u && u.id !== x.parent) { charged[x.id] = u.id; timeParent[x.id] = u.id; }
    });
    ["incl"].concat(BUF_KEYS).forEach(function (key) {
      var ek = key === "incl" ? "excl" : "x:" + key;
      function val(n) { return key === "incl" ? n.incl : (own(n.raw, key) ? n.raw[key] : null); }
      nodes.forEach(function (n) {
        var v = val(n);
        if (v === null || v === undefined) { n[ek] = null; return; }
        var sub = 0;
        n.children.forEach(function (cid) { var c = nodes[cid]; if (!isCteInit(c) && !own(charged, c.id)) sub += parseFloat(val(c) || 0); });
        n[ek] = parseFloat(v) - sub;
      });
      Object.keys(charged).forEach(function (xid) {
        var u = nodes[charged[xid]];
        if (u[ek] !== null) u[ek] -= parseFloat(val(nodes[xid]) || 0);
      });
      Object.keys(cteRoot).forEach(function (name) {
        var remaining = parseFloat(val(cteRoot[name]) || 0);
        for (var i = 0; i < nodes.length && remaining > 0; i++) {
          var s = nodes[i];
          if (s.type === "CTE Scan" && s.raw["CTE Name"] === name && s[ek] !== null) {
            var take = Math.min(remaining, Math.max(s[ek], 0));
            s[ek] -= take; remaining -= take;
            if (key === "incl" && take > 0 && timeParent[cteRoot[name].id] === cteRoot[name].parent) timeParent[cteRoot[name].id] = s.id;
          }
        }
      });
      nodes.forEach(function (n) { if (n[ek] !== null) n[ek] = Math.max(n[ek], 0); });
    });
    nodes.forEach(function (n) {
      n.buf = {}; n.buf_excl = {};
      BUF_KEYS.forEach(function (k) {
        if (own(n.raw, k)) n.buf[k] = num(n.raw[k]);
        if (n["x:" + k] !== null && n["x:" + k] !== undefined) n.buf_excl[k] = n["x:" + k];
        delete n["x:" + k];
      });
    });
    var execMs = own(obj, "Execution Time") ? obj["Execution Time"] : null;
    var runtime = execMs !== null ? parseFloat(execMs) : root.incl;
    nodes.forEach(function (n) { n.pct = (n.excl !== null && n.excl !== undefined && runtime) ? n.excl / runtime * 100 : null; });

    function lq0(c) { return c.lq !== null ? c.lq : 0; }
    nodes.forEach(function (n) {
      n.base_lq = null; n.local_lq = null;
      if (n.lq === null) return;
      var data = n.children.map(function (c) { return nodes[c]; }).filter(function (c) { return c.rel !== "InitPlan" && c.rel !== "SubPlan"; });
      var t = n.type, base;
      if (t === "CTE Scan") { var src = cteRoot[n.raw["CTE Name"] || ""]; base = src ? lq0(src) : 0; }
      else if (!data.length) base = 0;
      else if (has(JOIN_TYPES, t) && data.length >= 2) {
        var outer = data.filter(function (c) { return c.rel === "Outer"; })[0] || data[0];
        var inner = data.filter(function (c) { return c.rel === "Inner"; })[0] || data[1];
        var jt = n.raw["Join Type"] || "Inner";
        if (jt === "Semi" || jt === "Anti") base = lq0(outer);
        else if (jt === "Right Semi" || jt === "Right Anti") base = lq0(inner);
        else base = lq0(outer) + lq0(inner);
      } else if (has(UNION_TYPES, t)) {
        var act = 0, est = 0;
        data.forEach(function (c) { if (c.lq !== null) { act += c.rows || 0; est += c.est; } });
        base = Math.log10(Math.max(act, 1) / Math.max(est, 1));
      } else base = lq0(data[0]);
      n.base_lq = base; n.local_lq = n.lq - base;
    });

    nodes.forEach(function (n) {
      var scope = "main", p = n;
      while (p) {
        if ((p.rel === "InitPlan" || p.rel === "SubPlan") && p.sub) {
          scope = p.sub.indexOf("CTE ") === 0 ? "cte:" + p.sub.slice(4) : "sub"; break;
        }
        p = p.parent !== null ? nodes[p.parent] : null;
      }
      n.scope = scope; n.hot = false;
    });
    nodes.forEach(function (n) { n.time_parent = timeParent[n.id]; });
    if (hasTiming) {
      var cur = root, seen = {};
      while (cur && !seen[cur.id]) {
        cur.hot = true; seen[cur.id] = true;
        var cid = cur.id;
        var kids = nodes.filter(function (k) { return k.time_parent === cid && k.incl !== null && k.incl !== undefined; });
        if (!kids.length) break;
        var nxt = kids.reduce(function (a, b) { return b.incl > a.incl ? b : a; });
        if (runtime && nxt.incl < runtime * 0.05) break;
        cur = nxt;
      }
    }
    var summary = makeSummary(obj, nodes, runtime, analyzed, hasTiming, meta);
    var insights = makeInsights(obj, nodes, summary);
    nodes.forEach(function (n) {
      n.insights = [];
      insights.forEach(function (ins, i) { if (ins.node === n.id) n.insights.push(i); });
    });
    return { label: label || "", summary: summary, nodes: nodes, insights: insights };
  }

  function initTokens(sub) {
    var toks = [], m = /^InitPlan (\d+)/.exec(sub || ""), re = /\$\d+/g, x;
    if (m) toks.push("(InitPlan " + m[1] + ")");
    while ((x = re.exec(sub || ""))) toks.push(x[0]);
    return toks;
  }
  function mentions(raw, toks) {
    var o = {};
    Object.keys(raw).forEach(function (k) { if (k !== "Subplan Name" && k !== "Plans") o[k] = raw[k]; });
    var text = JSON.stringify(o);
    return toks.some(function (t) {
      if (t.charAt(0) === "$") return new RegExp(t.replace("$", "\\$") + "(?!\\d)").test(text);
      return text.indexOf(t) >= 0;
    });
  }
  function firstUser(nodes, top, skip, toks) {
    var stack = [top];
    while (stack.length) {
      var m = stack.pop();
      if (m === skip) continue;
      if (mentions(m.raw, toks)) return m;
      for (var i = m.children.length - 1; i >= 0; i--) stack.push(nodes[m.children[i]]);
    }
    return null;
  }
  var PG17_PARAM = /\((?:hashed )?(?:SubPlan|InitPlan) \d+\)\.col\d+/;
  function inferMajor(obj, nodes, meta) {
    if (meta.server_version_num) { var v = parseInt(meta.server_version_num, 10); if (!isNaN(v)) return Math.floor(v / 10000); }
    for (var i = 0; i < nodes.length; i++) {
      var raw = nodes[i].raw;
      for (var k in raw) {
        var vals = Array.isArray(raw[k]) ? raw[k] : [raw[k]];
        for (var j = 0; j < vals.length; j++) if (typeof vals[j] === "string" && PG17_PARAM.test(vals[j])) return 17;
      }
    }
    return obj["Query Identifier"] !== undefined && obj["Query Identifier"] !== null ? 14 : null;
  }
  function makeSummary(obj, nodes, runtime, analyzed, hasTiming, meta) {
    var root = nodes[0], jit = obj["JIT"] || {}, jt = (jit["Timing"] || {})["Total"];
    if (jt && typeof jt === "object") jt = jt["Total"];
    var triggers = obj["Triggers"] || [], worst = null, maxAbs = null;
    nodes.forEach(function (n) {
      if (n.lq === null) return;
      if (worst === null || Math.abs(n.lq) > Math.abs(worst.lq)) worst = n;
      maxAbs = maxAbs === null ? Math.abs(n.lq) : Math.max(maxAbs, Math.abs(n.lq));
    });
    var hasTemp = own(root.raw, "Temp Read Blocks") || own(root.raw, "Temp Written Blocks");
    return {
      analyzed: analyzed, has_timing: hasTiming, nodes: nodes.length,
      execution_ms: own(obj, "Execution Time") ? obj["Execution Time"] : null,
      planning_ms: own(obj, "Planning Time") ? obj["Planning Time"] : null, runtime_ms: runtime === undefined ? null : runtime,
      jit_ms: (jt !== undefined && jt !== null) ? parseFloat(jt) : null,
      trigger_ms: triggers.length ? triggers.reduce(function (a, t) { return a + num(t["Time"]); }, 0) : null,
      triggers: triggers.map(function (t) { return { name: t["Trigger Name"], relation: t["Relation"], ms: num(t["Time"]), calls: num(t["Calls"]) }; }),
      settings: obj["Settings"] || {}, query_id: own(obj, "Query Identifier") ? obj["Query Identifier"] : null,
      max_abs_lq: maxAbs, worst_node: worst ? worst.id : null,
      shared_hit: own(root.raw, "Shared Hit Blocks") ? num(root.raw["Shared Hit Blocks"]) : null,
      shared_read: own(root.raw, "Shared Read Blocks") ? num(root.raw["Shared Read Blocks"]) : null,
      temp_blocks: hasTemp ? num(root.raw["Temp Read Blocks"]) + num(root.raw["Temp Written Blocks"]) : null,
      cost: root.cost, major: inferMajor(obj, nodes, meta)
    };
  }
  function conds(r) {
    var out = [];
    COND_KEYS.forEach(function (k) { var v = r[k]; if (v && (!Array.isArray(v) || v.length)) out.push(k + ": " + (Array.isArray(v) ? v.join(", ") : v)); });
    return out.join("; ");
  }
  function hashBudgetKb(settings, major) {
    var wm = parseMemKb(settings["work_mem"]) || 4096, hm, src;
    if (settings["hash_mem_multiplier"] !== undefined && settings["hash_mem_multiplier"] !== null) { hm = parseFloat(settings["hash_mem_multiplier"]); src = "Settings"; }
    else if (major !== null && major <= 14) { hm = 1; src = "mặc định PG13/14"; }
    else { hm = 2; src = "mặc định PG15+"; }
    return { work_mem_kb: wm, hash_mem_multiplier: hm, budget_kb: wm * hm, hm_source: src };
  }
  function pyG(x) { return String(parseFloat(x.toPrecision(6))); }

  function makeInsights(obj, nodes, s) {
    var out = [], R = s.runtime_ms;
    function add(rule, sev, nid, p) { out.push({ rule: rule, sev: sev, node: nid, p: p || {} }); }
    function share(v) { return (v !== null && v !== undefined && R) ? v / R : 0; }
    function name(n) { return (n.label + (n.object ? " " + n.object : "")).trim(); }
    function rnd(x) { return pyRound(x); }

    if (!s.analyzed) add("NO_ANALYZE", "info", null);
    else if (!s.has_timing) add("NO_TIMING", "info", null);

    var hot = nodes.filter(function (n) { return n.excl !== null && n.excl !== undefined; })
      .slice().sort(function (a, b) { return b.excl - a.excl || a.id - b.id; });
    hot.slice(0, 3).forEach(function (n) {
      var sh = share(n.excl);
      if (sh >= 0.25) add("HOTSPOT", sh >= 0.5 ? "high" : "medium", n.id, { node: name(n), pct: rnd(sh * 100), excl: fmtMs(n.excl), runtime: fmtMs(R) });
    });

    var budget = hashBudgetKb(s.settings, s.major), flagged = {};
    nodes.forEach(function (n) {
      if (n.rel !== "SubPlan" || !n.analyzed || (n.loops || 0) < 10) return;
      var parent = n.parent !== null ? nodes[n.parent] : null;
      var ptxt = parent ? JSON.stringify(parent.raw) : "", numS = n.sub.replace(/\D/g, "");
      if (new RegExp("hashed (?:\\()?SubPlan " + numS + "\\b").test(ptxt)) return;
      var sh = share(n.incl);
      var sev = sh >= 0.5 ? "critical" : (sh >= 0.2 ? "high" : ((sh >= 0.05 || !R) ? "medium" : "low"));
      var anyAll = /\b(?:ANY|ALL) \(/.test(ptxt) || ptxt.indexOf("NOT (ANY") >= 0;
      var needKb = n.est * (Math.floor((n.width + 7) / 8) * 8 + 24) / 1024;
      add("SUBPLAN_RESCAN", sev, n.id, {
        sub: n.sub, loops: fmtNum(n.loops), rows: fmtNum(n.rows), read: fmtNum(n.act_total), pct: rnd(sh * 100),
        notin: anyAll, est: fmtNum(n.est), width: n.width, need: fmtBytes(needKb * 1024),
        budget: fmtBytes(budget.budget_kb * 1024), work_mem: fmtBytes(budget.work_mem_kb * 1024),
        hm: pyG(budget.hash_mem_multiplier), hm_source: budget.hm_source, fits: needKb <= budget.budget_kb,
        need_work_mem: fmtBytes(Math.ceil(needKb / budget.hash_mem_multiplier) * 1024)
      });
      flagged[n.id] = true;
    });
    function underFlagged(n) {
      var p = n;
      while (p) { if (flagged[p.id]) return true; p = p.parent !== null ? nodes[p.parent] : null; }
      return false;
    }
    nodes.forEach(function (n) {
      if (!has(RESCAN_COSTLY, n.type) || !n.analyzed || (n.loops || 0) < 1000 || n.rel === "SubPlan") return;
      if (underFlagged(n)) return;
      var sh = share(n.incl);
      if (sh < 0.1 && (n.act_total || 0) < 1e6) return;
      add("RESCAN", sh >= 0.5 ? "high" : "medium", n.id, { node: name(n), loops: fmtNum(n.loops), rows: fmtNum(n.rows), pct: rnd(sh * 100) });
    });

    var origins = nodes.filter(function (n) { return n.local_lq !== null && Math.abs(n.local_lq) >= 1; })
      .sort(function (a, b) { return Math.abs(b.local_lq) - Math.abs(a.local_lq) || a.id - b.id; });
    origins.slice(0, 6).forEach(function (n) {
      var a = Math.abs(n.local_lq), sh = share(n.incl);
      var sev = (a >= 3 || (a >= 2 && sh >= 0.2)) ? "high" : ((a >= 2 || sh >= 0.2) ? "medium" : "low");
      add("MISEST", sev, n.id, {
        node: name(n), dir: n.local_lq > 0 ? "thiếu" : "thừa", local: fmtFactor(n.local_lq), total: fmtFactor(n.lq),
        base: fmtFactor(n.base_lq), est: fmtNum(n.est), rows: fmtNum(n.rows), cond: conds(n.raw) || "–", cte: n.type === "CTE Scan"
      });
    });

    nodes.forEach(function (n) {
      var r = n.raw, sh = share(n.incl), small = !!R && sh < 0.05;
      if (r["Sort Space Type"] === "Disk") {
        var k = num(r["Sort Space Used"]);
        add("SORT_SPILL", small ? "low" : "medium", n.id, { node: name(n), disk: fmtBytes(k * 1024), need: fmtBytes(k * 1024 * 2) });
      }
      var hb = num(r["Hash Batches"]), ob = num(r["Original Hash Batches"]);
      if (n.type === "Hash" && hb > 1) {
        var peak = num(r["Peak Memory Usage"]);
        add("HASH_SPILL", small ? "low" : "medium", n.id, { batches: fmtNum(hb), orig: fmtNum(ob || hb), grew: !!(ob && ob < hb), peak: fmtBytes(peak * 1024), need: fmtBytes(peak * hb * 1024) });
      }
      if (n.type === "Aggregate" && (num(r["HashAgg Batches"]) > 1 || num(r["Disk Usage"]) > 0)) {
        add("HASHAGG_SPILL", small ? "low" : "medium", n.id, { batches: fmtNum(num(r["HashAgg Batches"])), disk: fmtBytes(num(r["Disk Usage"]) * 1024) });
      }
    });
    var spilled = {};
    out.forEach(function (i) { if (has(["SORT_SPILL", "HASH_SPILL", "HASHAGG_SPILL"], i.rule)) spilled[i.node] = true; });
    nodes.forEach(function (n) {
      var tw = (n.buf_excl || {})["Temp Written Blocks"] || 0;
      if (tw >= 12800 && !spilled[n.id]) add("TEMP_IO", tw >= 131072 ? "medium" : "low", n.id, { node: name(n), size: fmtBytes(tw * BLOCK) });
    });
    nodes.forEach(function (n) {
      var r = n.raw;
      if (!n.analyzed || !has(SCAN_TYPES, n.type)) return;
      var removed = n.removed || 0, act = n.act_total || 0, sh = share(n.incl);
      if (removed >= 100000 && removed >= 9 * act && (sh >= 0.05 || !R)) {
        add("FILTER_WASTE", sh >= 0.2 ? "medium" : "low", n.id, {
          node: name(n), removed: fmtNum(removed), kept: fmtNum(act), pct: rnd(removed / Math.max(removed + act, 1) * 100),
          seq: n.type === "Seq Scan", cte: n.type === "CTE Scan", idx: n.type !== "Seq Scan" && n.type !== "CTE Scan", cond: conds(r) || "–"
        });
      }
      if (num(r["Lossy Heap Blocks"]) > 0 && num(r["Rows Removed by Index Recheck"]) * (n.loops || 0) >= 10000) {
        add("LOSSY_BITMAP", "low", n.id, { lossy: fmtNum(num(r["Lossy Heap Blocks"])), recheck: fmtNum(num(r["Rows Removed by Index Recheck"]) * (n.loops || 0)) });
      }
      var hf = num(r["Heap Fetches"]) * (n.loops || 0);
      if (n.type === "Index Only Scan" && hf >= 10000 && hf >= 0.5 * Math.max(act, 1)) add("HEAP_FETCHES", "low", n.id, { fetches: fmtNum(hf), rows: fmtNum(act) });
    });
    nodes.forEach(function (n) {
      var r = n.raw;
      if ((n.type === "Gather" || n.type === "Gather Merge") && own(r, "Workers Launched") && num(r["Workers Launched"]) < num(r["Workers Planned"])) {
        add("WORKERS", "low", n.id, { planned: fmtNum(num(r["Workers Planned"])), launched: fmtNum(num(r["Workers Launched"])) });
      }
      if (n.type === "Memoize" && num(r["Cache Evictions"]) > 0) {
        var hits = num(r["Cache Hits"]), misses = num(r["Cache Misses"]);
        if (hits < misses) add("MEMOIZE", "low", n.id, { hits: fmtNum(hits), misses: fmtNum(misses), evictions: fmtNum(num(r["Cache Evictions"])) });
      }
    });
    var E = s.execution_ms;
    if (s.jit_ms !== null && E && s.jit_ms >= 0.1 * E && s.jit_ms >= 100) add("JIT", "medium", null, { jit: fmtMs(s.jit_ms), pct: rnd(s.jit_ms / E * 100) });
    if (s.trigger_ms && E && s.trigger_ms >= 0.1 * E) {
      var top = s.triggers.reduce(function (a, b) { return b.ms > a.ms ? b : a; });
      add("TRIGGERS", s.trigger_ms >= 0.5 * E ? "high" : "medium", null, { ms: fmtMs(s.trigger_ms), pct: rnd(s.trigger_ms / E * 100), top: top.name || "?", calls: fmtNum(top.calls) });
    }
    var P = s.planning_ms;
    if (P !== null && P >= 50 && (E === null || P > E)) add("PLANNING", "low", null, { planning: fmtMs(P), exec: fmtMs(E) });
    var never = nodes.filter(function (n) { return n.never; }).length;
    if (never) add("NEVER_EXECUTED", "info", null, { count: never });

    out = out.map(function (o, i) { o._i = i; return o; });
    out.sort(function (a, b) {
      return (SEV_ORDER[a.sev] - SEV_ORDER[b.sev]) || ((a.node === null ? -1 : a.node) - (b.node === null ? -1 : b.node)) || (a._i - b._i);
    });
    out.forEach(function (o) { delete o._i; });
    return out;
  }

  // ------------------------------------------------------------------ matching (port of analyze.match)
  function signature(nodes, n) {
    var leaves = [];
    (function walk(m, top) {
      if (!top && (m.rel === "InitPlan" || m.rel === "SubPlan")) return;
      var t = target(m.raw);
      if (t) leaves.push(t);
      m.children.forEach(function (c) { walk(nodes[c], false); });
    })(n, true);
    leaves.sort();
    return n.scope + "|" + leaves.join(",");
  }
  function category(n) {
    if (has(SCAN_TYPES, n.type)) return "scan";
    if (has(JOIN_TYPES, n.type)) return "join";
    return n.type;
  }
  function match(a, b) {
    var na = a.nodes, nb = b.nodes;
    var sa = na.map(function (n) { return signature(na, n); }), sb = nb.map(function (n) { return signature(nb, n); });
    var pairs = { 0: 0 }, used = { 0: true };
    [function (n, s) { return s + "#" + n.type; }, function (n, s) { return s + "#" + category(n); }].forEach(function (keyf) {
      var buckets = {};
      nb.forEach(function (n) { if (!used[n.id]) { var k = keyf(n, sb[n.id]); (buckets[k] = buckets[k] || []).push(n.id); } });
      na.forEach(function (n) {
        if (own(pairs, n.id)) return;
        var lst = buckets[keyf(n, sa[n.id])];
        while (lst && lst.length && used[lst[0]]) lst.shift();
        if (lst && lst.length) { var j = lst.shift(); pairs[n.id] = j; used[j] = true; }
      });
    });
    return Object.keys(pairs).map(Number).sort(function (x, y) { return x - y; }).map(function (i) { return [i, pairs[i]]; });
  }

  function renderInsight(ins, rules) {
    var tpl = rules[ins.rule] || {}, p = ins.p || {};
    function fill(t) {
      t = t.replace(/\{\?(\w+)\}([\s\S]*?)\{\/\1\}/g, function (m, k, body) { return p[k] ? body : ""; });
      t = t.replace(/\{!(\w+)\}([\s\S]*?)\{\/\1\}/g, function (m, k, body) { return p[k] ? "" : body; });
      return t.replace(/\{(\w+)\}/g, function (m, k) { return own(p, k) ? String(p[k]) : m; });
    }
    return { title: fill(tpl.title || ""), detail: fill(tpl.detail || ""), fix: fill(tpl.fix || "") };
  }

  var api = {
    parseAny: parseAny, parseTextPlan: parseTextPlan, analyze: analyze, match: match, renderInsight: renderInsight,
    fmtMs: fmtMs, fmtNum: fmtNum, fmtBytes: fmtBytes, fmtFactor: fmtFactor, nodeLabel: nodeLabel,
    BUF_KEYS: BUF_KEYS, SEV_ORDER: SEV_ORDER, BLOCK: BLOCK, conds: conds
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PlanvizCore = api;
})(typeof window !== "undefined" ? window : this);
