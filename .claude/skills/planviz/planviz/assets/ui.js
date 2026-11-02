/* planviz UI: renders the analyses produced by core.js. All text from plans goes through textContent. */
(function () {
  "use strict";
  var C = window.PlanvizCore;
  var DATA = JSON.parse(document.getElementById("planviz-data").textContent || "{}");
  var RULES = DATA.rules || {};
  var SEVL = RULES._severity || {};
  var SVGNS = "http://www.w3.org/2000/svg";
  var state = { plans: [], cur: 0, sel: null, view: "graph", color: "time", sort: null, cmpA: 0, cmpB: 1, search: "",
    tf: {}, icicleColor: "time", showAllFindings: false, matchCache: {} };

  // ------------------------------------------------------------------ DOM helpers
  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    setAttrs(el, attrs);
    append(el, kids);
    return el;
  }
  function s(tag, attrs, kids) {
    var el = document.createElementNS(SVGNS, tag);
    setAttrs(el, attrs);
    append(el, kids);
    return el;
  }
  function setAttrs(el, attrs) {
    if (!attrs) return;
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (k === "class") el.setAttribute("class", v);
      else if (k === "style" && typeof v === "object") Object.keys(v).forEach(function (p) { el.style.setProperty(p, v[p]); });
      else el.setAttribute(k, v === true ? "" : v);
    });
  }
  function append(el, kids) {
    if (kids === null || kids === undefined) return;
    (Array.isArray(kids) ? kids : [kids]).forEach(function (k) {
      if (k === null || k === undefined || k === false) return;
      el.appendChild(typeof k === "string" || typeof k === "number" ? document.createTextNode(String(k)) : k);
    });
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function $(id) { return document.getElementById(id); }
  function trunc(t, n) { t = t || ""; return t.length > n ? t.slice(0, Math.max(1, n - 1)) + "…" : t; }

  // ------------------------------------------------------------------ data
  function plan() { return state.plans[state.cur]; }
  function addPlan(p) {
    p.a = C.analyze(p.plan, p.label, p.meta || {});
    state.plans.push(p);
    return state.plans.length - 1;
  }
  function nodeName(n) { return (n.label + (n.object ? " " + n.object : "")).trim(); }
  function bufSum(o, keys) { var t = 0; keys.forEach(function (k) { t += (o && o[k]) || 0; }); return t; }
  var READ_KEYS = ["Shared Hit Blocks", "Shared Read Blocks", "Local Hit Blocks", "Local Read Blocks"];
  var TEMP_KEYS = ["Temp Read Blocks", "Temp Written Blocks"];
  function magBin(f) {
    if (f === null || f === undefined || isNaN(f)) return "na";
    return f < 0.01 ? "m0" : f < 0.05 ? "m1" : f < 0.15 ? "m2" : f < 0.35 ? "m3" : f < 0.65 ? "m4" : "m5";
  }
  function qBin(lq) {
    if (lq === null || lq === undefined) return "na";
    var a = Math.abs(lq);
    if (a < 1) return "q0";
    return (lq > 0 ? "u" : "o") + (a < 2 ? 1 : a < 3 ? 2 : 3);
  }
  function runtime(a) { return a.summary.runtime_ms; }
  function metric(a, n, mode) {
    var R = runtime(a);
    if (mode === "lq") return { bin: qBin(n.lq), text: n.lq === null ? (n.never ? "never executed" : "–") : "sai số " + C.fmtFactor(n.lq) };
    if (mode === "local") return { bin: qBin(n.local_lq), text: n.local_lq === null ? "–" : "tại chỗ " + C.fmtFactor(n.local_lq) };
    if (mode === "reads" || mode === "temp") {
      var keys = mode === "reads" ? READ_KEYS : TEMP_KEYS;
      var v = bufSum(n.buf_excl, keys), tot = 0;
      a.nodes.forEach(function (m) { tot += bufSum(m.buf_excl, keys); });
      if (!Object.keys(n.buf || {}).length) return { bin: "na", text: "không có BUFFERS" };
      return { bin: tot ? magBin(v / tot) : "m0", text: C.fmtBytes(v * C.BLOCK) + (tot ? " · " + Math.round(v / tot * 100) + "%" : "") };
    }
    if (n.excl === null || n.excl === undefined) return { bin: "na", text: a.summary.analyzed ? "không có thời gian" : "chỉ ước lượng" };
    return { bin: R ? magBin(n.excl / R) : "na", text: (R ? Math.round(n.excl / R * 100) + "% · " : "") + C.fmtMs(n.excl) };
  }
  var MODES = [["time", "Thời gian riêng"], ["lq", "Sai số ước lượng"], ["local", "Sai số tại chỗ"], ["reads", "Buffers đọc (riêng)"], ["temp", "Ghi/đọc file tạm"]];
  function legendFor(mode) {
    var items;
    if (mode === "lq" || mode === "local") {
      items = [["o3", "thừa ≥1000×"], ["o2", "thừa 100–1000×"], ["o1", "thừa 10–100×"], ["q0", "trong 10×"], ["u1", "thiếu 10–100×"], ["u2", "thiếu 100–1000×"], ["u3", "thiếu ≥1000×"]];
    } else {
      items = [["m0", "<1%"], ["m1", "1–5%"], ["m2", "5–15%"], ["m3", "15–35%"], ["m4", "35–65%"], ["m5", "≥65%"]];
    }
    var lead = mode === "lq" ? "Sai số tích luỹ (thực tế ÷ ước lượng, mỗi loop):" : mode === "local" ? "Sai số sinh ra tại node (đã trừ phần kế thừa từ đầu vào):" :
      mode === "time" ? "Phần trăm thời gian chạy:" : "Phần trăm tổng của cả plan:";
    return h("div", { class: "legend" }, [h("span", { text: lead })].concat(items.map(function (it) {
      return h("span", null, [h("span", { class: "sw f-" + it[0] }), it[1]]);
    })));
  }
  function getMatch(i, j) {
    var key = i + ":" + j;
    if (!state.matchCache[key]) state.matchCache[key] = C.match(state.plans[i].a, state.plans[j].a);
    return state.matchCache[key];
  }
  function counterpart(i, nid, j) {
    var pairs = getMatch(Math.min(i, j), Math.max(i, j));
    for (var k = 0; k < pairs.length; k++) {
      if (i < j && pairs[k][0] === nid) return pairs[k][1];
      if (i > j && pairs[k][1] === nid) return pairs[k][0];
    }
    return null;
  }

  // ------------------------------------------------------------------ header, KPIs, findings
  function renderHeader() {
    var box = $("plans");
    clear(box);
    state.plans.forEach(function (p, i) {
      var t = p.a.summary.execution_ms !== null ? C.fmtMs(p.a.summary.execution_ms) : "EXPLAIN";
      var chip = h("span", { class: "chip-plan", "aria-pressed": String(i === state.cur) }, [
        h("button", { class: "btn small", style: { border: "0", background: "none", padding: "0" }, title: p.source || "", onclick: function () { switchPlan(i); } },
          [h("b", { text: p.label }), " ", h("span", { class: "muted num", text: t })]),
        h("button", { class: "x", title: "Bỏ plan này", "aria-label": "Bỏ plan " + p.label, text: "×", onclick: function () { removePlan(i); } })
      ]);
      box.appendChild(chip);
    });
  }
  function kpi(label, value, sub) { return h("div", { class: "kpi" }, [h("span", { text: label }), h("b", { text: value, title: value }), sub ? h("em", { text: sub }) : null]); }
  function renderKpis() {
    var box = $("kpis");
    clear(box);
    if (!plan()) return;
    var a = plan().a, S = a.summary;
    box.appendChild(kpi("Thời gian thực thi", S.execution_ms !== null ? C.fmtMs(S.execution_ms) : "–", S.analyzed ? (S.trigger_ms ? "trigger " + C.fmtMs(S.trigger_ms) : null) : "chỉ EXPLAIN, không ANALYZE"));
    if (state.plans.length > 1) {
      var base = state.cur === 0 ? 1 : 0, b = state.plans[base].a.summary;
      if (S.execution_ms && b.execution_ms) {
        var r = S.execution_ms / b.execution_ms, same = r > 0.9 && r < 1.1;
        box.appendChild(kpi("So với " + state.plans[base].label, same ? "≈" : (r >= 1 ? "×" + C.fmtNum(Math.round(r * 10) / 10) : "÷" + C.fmtNum(Math.round(10 / r) / 10)),
          same ? "gần như nhau (" + C.fmtMs(b.execution_ms) + ")" : r >= 1 ? "chậm hơn" : "nhanh hơn"));
      }
    }
    box.appendChild(kpi("Lập kế hoạch", S.planning_ms !== null ? C.fmtMs(S.planning_ms) : "–", null));
    var worst = S.worst_node !== null ? a.nodes[S.worst_node] : null;
    box.appendChild(kpi("Sai số lớn nhất", worst ? C.fmtFactor(worst.lq) : "–", worst ? "#" + worst.id + " " + trunc(nodeName(worst), 34) : null));
    var hot = a.nodes.filter(function (n) { return n.excl !== null; }).sort(function (x, y) { return y.excl - x.excl; })[0];
    if (hot && S.runtime_ms) box.appendChild(kpi("Điểm nóng", Math.round(hot.excl / S.runtime_ms * 100) + "%", "#" + hot.id + " " + trunc(nodeName(hot), 34)));
    if (S.shared_hit !== null || S.shared_read !== null) {
      var hit = S.shared_hit || 0, rd = S.shared_read || 0;
      box.appendChild(kpi("Buffers shared", C.fmtBytes((hit + rd) * C.BLOCK), (hit + rd) ? Math.round(hit / (hit + rd) * 100) + "% từ cache" : null));
    }
    if (S.temp_blocks !== null) box.appendChild(kpi("File tạm", C.fmtBytes(S.temp_blocks * C.BLOCK), "đọc + ghi"));
    box.appendChild(kpi("Số node", String(S.nodes), S.query_id !== null ? "query_id " + S.query_id : null));
  }
  function sevChip(sev) { return h("span", { class: "sev " + sev, text: SEVL[sev] || sev }); }
  function renderFindings() {
    var box = $("findings");
    clear(box);
    if (!plan()) return;
    var a = plan().a, list = a.insights;
    var head = h("div", { class: "findings-head" }, [h("h2", { text: "Phát hiện" }),
      h("span", { class: "muted", text: list.length ? list.length + " mục, nặng nhất trước" : "Không có phát hiện nào đáng chú ý" })]);
    box.appendChild(head);
    var shown = state.showAllFindings ? list : list.slice(0, 5);
    shown.forEach(function (ins, idx) {
      var r = C.renderInsight(ins, RULES), n = ins.node !== null ? a.nodes[ins.node] : null;
      var det = h("details", { class: "find" }, [
        h("summary", null, [sevChip(ins.sev), h("span", { text: r.title }), n ? h("span", { class: "rule", text: "#" + n.id }) : h("span", { class: "rule", text: "plan" })]),
        h("div", { class: "body" }, [
          h("div", null, [h("b", { text: "Vì sao" }), r.detail]),
          r.fix ? h("div", null, [h("b", { text: "Hướng xử lý" }), r.fix]) : null,
          n ? h("div", null, [h("button", { class: "btn small go", text: "Xem node #" + n.id, onclick: function () { select(n.id, true); } }),
            " ", h("span", { class: "rule", text: ins.rule })]) : h("span", { class: "rule", text: ins.rule })
        ])
      ]);
      if (idx === 0 && ins.sev !== "info") det.open = true;
      box.appendChild(det);
    });
    if (list.length > 5) {
      box.appendChild(h("div", null, h("button", { class: "btn small", text: state.showAllFindings ? "Thu gọn" : "Xem tất cả " + list.length + " phát hiện",
        onclick: function () { state.showAllFindings = !state.showAllFindings; renderFindings(); } })));
    }
  }

  // ------------------------------------------------------------------ views
  var VIEWS = [["graph", "Sơ đồ"], ["table", "Bảng cây"], ["scatter", "Ước lượng ↔ thực tế"], ["time", "Thời gian"], ["compare", "So sánh"]];
  function renderTabs() {
    var box = $("tabs");
    clear(box);
    VIEWS.forEach(function (v) {
      box.appendChild(h("button", { class: "tab", role: "tab", "aria-selected": String(state.view === v[0]), text: v[1] + (v[0] === "compare" && state.plans.length > 1 ? " (" + state.plans.length + ")" : ""),
        onclick: function () { state.view = v[0]; renderTabs(); renderView(); } }));
    });
  }
  function renderView() {
    var box = $("view");
    clear(box);
    hideTip();
    if (!plan()) {
      box.appendChild(h("div", { class: "empty" }, ["Chưa có plan nào. ", h("button", { class: "btn primary", text: "Mở plan…", onclick: openDialog }),
        " hoặc kéo-thả file EXPLAIN vào trang."]));
      return;
    }
    ({ graph: viewGraph, table: viewTable, scatter: viewScatter, time: viewTime, compare: viewCompare })[state.view](box);
  }

  // ---- diagram
  var NW = 232, NH = 92, GX = 22, GY = 74, graphEls = {};
  function layout(nodes) {
    var w = {}, pos = {}, maxDepth = 0;
    function sw(n) {
      var kids = n.children.map(function (c) { return nodes[c]; });
      if (!kids.length) return (w[n.id] = NW);
      var t = 0;
      kids.forEach(function (k) { t += sw(k); });
      t += GX * (kids.length - 1);
      return (w[n.id] = Math.max(NW, t));
    }
    function place(n, left, depth) {
      maxDepth = Math.max(maxDepth, depth);
      var cx = left + w[n.id] / 2;
      pos[n.id] = { x: cx - NW / 2, y: depth * (NH + GY) };
      var kids = n.children.map(function (c) { return nodes[c]; });
      var tot = GX * (kids.length - 1);
      kids.forEach(function (k) { tot += w[k.id]; });
      var x = cx - tot / 2;
      kids.forEach(function (k) { place(k, x, depth + 1); x += w[k.id] + GX; });
    }
    sw(nodes[0]);
    place(nodes[0], 0, 0);
    return { pos: pos, width: w[0], height: (maxDepth + 1) * (NH + GY) - GY };
  }
  function edgeWidth(n) {
    var r = n.analyzed ? (n.act_total || 0) : n.est_total;
    return Math.min(16, 1.5 + 2.4 * Math.log10(Math.max(r, 1)));
  }
  function searchHit(n, q) {
    if (!q) return true;
    var hay = (n.label + " " + n.object + " " + n.sub + " " + n.index + " " + C.conds(n.raw)).toLowerCase();
    return hay.indexOf(q) >= 0;
  }
  function viewGraph(box) {
    var a = plan().a, nodes = a.nodes;
    var sel = h("select", { id: "colorby", "aria-label": "Tô màu node theo", onchange: function (e) { state.color = e.target.value; renderView(); } },
      MODES.map(function (m) { var o = h("option", { value: m[0], text: m[1] }); if (m[0] === state.color) o.selected = true; return o; }));
    var search = h("input", { type: "search", id: "nodesearch", placeholder: "Tìm bảng, CTE, điều kiện…", value: state.search, "aria-label": "Tìm node",
      oninput: function (e) { state.search = e.target.value; applyGraphClasses(); } });
    var panel = h("div", { class: "panel" }, [
      h("div", { class: "tools" }, [h("label", null, ["Tô màu theo", sel]), h("label", null, [search]), h("span", { class: "sp" }),
        h("span", { class: "muted", style: { "font-size": "12px" }, text: "Độ dày cạnh = số dòng đi lên node cha. Kéo để di chuyển, cuộn để phóng to." })]),
      legendFor(state.color)
    ]);
    var L = layout(nodes);
    var vp = s("g", { id: "vp" });
    var svg = s("svg", { role: "img", "aria-label": "Sơ đồ cây plan" }, vp);
    var canvas = h("div", { class: "canvas", id: "canvas" }, [svg]);
    var zoom = h("div", { class: "zoom" }, [
      h("button", { title: "Thu nhỏ", "aria-label": "Thu nhỏ", text: "−", onclick: function () { zoomBy(1 / 1.25); } }),
      h("button", { title: "Vừa khung", "aria-label": "Vừa khung", text: "⤢", onclick: function () { fit(L); } }),
      h("button", { title: "Phóng to", "aria-label": "Phóng to", text: "+", onclick: function () { zoomBy(1.25); } })
    ]);
    canvas.appendChild(zoom);
    panel.appendChild(canvas);
    box.appendChild(panel);
    graphEls = {};
    var eg = s("g"), ng = s("g"), lg = s("g");
    vp.appendChild(eg); vp.appendChild(lg); vp.appendChild(ng);
    nodes.forEach(function (n) {
      var p = L.pos[n.id];
      if (n.parent !== null) {
        var pp = L.pos[n.parent], x1 = p.x + NW / 2, y1 = p.y, x2 = pp.x + NW / 2, y2 = pp.y + NH;
        var cls = "edge" + (n.hot && a.nodes[n.parent].hot ? " hot" : "") + (n.rel === "InitPlan" || n.rel === "SubPlan" ? " sub" : "");
        eg.appendChild(s("path", { class: cls, d: "M" + x1 + "," + y1 + " C" + x1 + "," + (y1 - GY / 2) + " " + x2 + "," + (y2 + GY / 2) + " " + x2 + "," + y2,
          "stroke-width": edgeWidth(n).toFixed(1) }));
        var rows = n.analyzed ? (n.never ? "0" : C.fmtNum(n.act_total)) : "~" + C.fmtNum(n.est_total);
        lg.appendChild(s("text", { class: "elabel", x: x1 + 9, y: y1 - 8, text: rows + (n.analyzed && n.loops > 1 ? " (" + C.fmtNum(n.loops) + " loop)" : "") }));
        if (n.sub) lg.appendChild(s("text", { class: "slabel", x: p.x, y: y1 - 26, text: trunc(n.sub, 34) }));
      }
      var g = s("g", { class: "nd", transform: "translate(" + p.x + "," + p.y + ")", tabindex: "0", role: "button", "aria-label": "#" + n.id + " " + nodeName(n),
        onclick: function (ev) { if (!canvas._moved) select(n.id, false); }, onkeydown: function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(n.id, false); } } });
      g.appendChild(s("title", { text: "#" + n.id + " " + nodeName(n) + (n.sub ? " [" + n.sub + "]" : "") }));
      g.appendChild(s("rect", { class: "box", width: NW, height: NH, rx: 6 }));
      var m = metric(a, n, state.color);
      g.appendChild(s("rect", { class: "f-" + m.bin, width: NW, height: 24, rx: 6 }));
      g.appendChild(s("rect", { class: "f-" + m.bin, y: 14, width: NW, height: 10 }));
      g.appendChild(s("text", { class: "tb o-" + m.bin, x: 9, y: 16.5, text: trunc(m.text, 30) }));
      if (n.insights.length) {
        var top = a.insights[n.insights[0]];
        g.appendChild(s("text", { class: "tb o-" + m.bin, x: NW - 9, y: 16.5, "text-anchor": "end", text: (SEVL[top.sev] || top.sev) + (n.insights.length > 1 ? " +" + (n.insights.length - 1) : "") }));
      }
      var loops = n.analyzed && n.loops > 1 ? "×" + C.fmtNum(n.loops) : "";
      g.appendChild(s("text", { class: "t1", x: 9, y: 42, text: trunc("#" + n.id + " " + n.label, Math.floor((NW - 18 - loops.length * 7) / 7.4)) }));
      if (loops) g.appendChild(s("text", { class: "loops", x: NW - 9, y: 42, "text-anchor": "end", text: loops }));
      var sub2 = n.object || (n.index ? "idx " + n.index : "") || (C.conds(n.raw) || "");
      g.appendChild(s("text", { class: "t2", x: 9, y: 60, text: trunc(sub2, 31) }));
      var rowTxt;
      if (!n.analyzed) rowTxt = "ước lượng " + C.fmtNum(n.est) + " dòng";
      else if (n.never) rowTxt = "never executed";
      else rowTxt = C.fmtNum(n.est) + " → " + C.fmtNum(n.rows) + " dòng";
      g.appendChild(s("text", { class: "t3", x: 9, y: 80, text: rowTxt }));
      if (n.lq !== null) {
        var qb = qBin(n.lq), qt = C.fmtFactor(n.lq), qw = 10 + qt.length * 7;
        g.appendChild(s("rect", { class: "f-" + qb, x: NW - 9 - qw, y: 67, width: qw, height: 18, rx: 3 }));
        g.appendChild(s("text", { class: "tb o-" + qb, x: NW - 9 - qw / 2, y: 80, "text-anchor": "middle", text: qt }));
      }
      if (n.hot) g.setAttribute("class", "nd hot");
      ng.appendChild(g);
      graphEls[n.id] = g;
    });
    enablePanZoom(canvas, vp, L);
    applyGraphClasses();
    var tf = state.tf[state.cur];
    if (tf) applyTf(vp, tf); else requestAnimationFrame(function () { fit(L); if (state.sel !== null) centerOn(state.sel, true); });
  }
  function applyGraphClasses() {
    var a = plan() && plan().a;
    if (!a) return;
    var q = (state.search || "").trim().toLowerCase();
    a.nodes.forEach(function (n) {
      var g = graphEls[n.id];
      if (!g) return;
      var cls = "nd" + (n.hot ? " hot" : "") + (n.id === state.sel ? " sel" : "") + (q && !searchHit(n, q) ? " dim" : "");
      g.setAttribute("class", cls);
    });
  }
  function applyTf(vp, tf) { vp.setAttribute("transform", "translate(" + tf.x.toFixed(1) + "," + tf.y.toFixed(1) + ") scale(" + tf.k.toFixed(4) + ")"); }
  function curTf() { return state.tf[state.cur] || (state.tf[state.cur] = { k: 1, x: 20, y: 20 }); }
  function fit(L) {
    var c = $("canvas");
    if (!c) return;
    var cw = c.clientWidth || 900, ch = c.clientHeight || 600;
    var k = Math.max(0.2, Math.min(1.1, (cw - 40) / L.width, (ch - 40) / (L.height + 40)));
    state.tf[state.cur] = { k: k, x: (cw - L.width * k) / 2, y: 36 };
    applyTf($("vp"), state.tf[state.cur]);
  }
  function zoomBy(f, cx, cy) {
    var c = $("canvas"), tf = curTf();
    if (!c) return;
    if (cx === undefined) { cx = c.clientWidth / 2; cy = c.clientHeight / 2; }
    var k = Math.max(0.1, Math.min(3, tf.k * f));
    tf.x = cx - (cx - tf.x) * (k / tf.k);
    tf.y = cy - (cy - tf.y) * (k / tf.k);
    tf.k = k;
    applyTf($("vp"), tf);
  }
  function enablePanZoom(canvas, vp, L) {
    var start = null;
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      var r = canvas.getBoundingClientRect();
      zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    canvas.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      var tf = curTf();
      start = { x: e.clientX, y: e.clientY, tx: tf.x, ty: tf.y, id: e.pointerId };
      canvas._moved = false;
    });
    canvas.addEventListener("pointermove", function (e) {
      if (!start) return;
      var dx = e.clientX - start.x, dy = e.clientY - start.y;
      if (!canvas._moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      if (!canvas._moved) { canvas._moved = true; canvas.classList.add("dragging"); try { canvas.setPointerCapture(start.id); } catch (err) { /* ignore */ } }
      var tf = curTf();
      tf.x = start.tx + dx; tf.y = start.ty + dy;
      applyTf(vp, tf);
    });
    function end() { start = null; canvas.classList.remove("dragging"); setTimeout(function () { canvas._moved = false; }, 0); }
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
  }
  function centerOn(id, onlyIfHidden) {
    var c = $("canvas"), g = graphEls[id];
    if (!c || !g) return;
    var L = layout(plan().a.nodes), p = L.pos[id], tf = curTf();
    var sx = p.x * tf.k + tf.x, sy = p.y * tf.k + tf.y, cw = c.clientWidth, ch = c.clientHeight;
    if (onlyIfHidden && sx > 10 && sy > 10 && sx + NW * tf.k < cw - 10 && sy + NH * tf.k < ch - 10) return;
    tf.x = cw / 2 - (p.x + NW / 2) * tf.k;
    tf.y = ch / 2 - (p.y + NH / 2) * tf.k;
    applyTf($("vp"), tf);
  }

  // ---- tree table
  var COLS = [
    { k: "id", t: "#", v: function (n) { return n.id; } },
    { k: "node", t: "Node", l: true },
    { k: "excl", t: "Riêng", v: function (n) { return n.excl; } },
    { k: "incl", t: "Tổng", v: function (n) { return n.incl; } },
    { k: "est", t: "Ước lượng", v: function (n) { return n.est; } },
    { k: "rows", t: "Thực tế", v: function (n) { return n.rows; } },
    { k: "lq", t: "Sai số", v: function (n) { return n.lq === null ? null : Math.abs(n.lq); } },
    { k: "local", t: "Tại chỗ", v: function (n) { return n.local_lq === null ? null : Math.abs(n.local_lq); } },
    { k: "loops", t: "Loops", v: function (n) { return n.loops; } },
    { k: "reads", t: "Buffers", v: function (n) { return bufSum(n.buf_excl, READ_KEYS); } },
    { k: "temp", t: "Tạm", v: function (n) { return bufSum(n.buf_excl, TEMP_KEYS); } }
  ];
  function viewTable(box) {
    var a = plan().a, R = runtime(a);
    var panel = h("div", { class: "panel" }, [h("div", { class: "tools" }, [
      h("span", { class: "muted", style: { "font-size": "12.5px" }, text: "Số dòng là mỗi loop, như EXPLAIN in ra. Buffers và Tạm là phần riêng của node (đã trừ node con). Bấm tiêu đề cột để sắp xếp." }),
      h("span", { class: "sp" }),
      state.sort ? h("button", { class: "btn small", text: "Về thứ tự cây", onclick: function () { state.sort = null; renderView(); } }) : null
    ])]);
    var order = a.nodes.slice();
    if (state.sort) {
      var col = COLS.filter(function (c) { return c.k === state.sort.k; })[0];
      order.sort(function (x, y) {
        var vx = col.v(x), vy = col.v(y);
        if (vx === null || vx === undefined) return 1;
        if (vy === null || vy === undefined) return -1;
        return state.sort.dir * (vy - vx) || x.id - y.id;
      });
    }
    var thead = h("tr", null, COLS.map(function (c) {
      var th = h("th", { class: c.l ? "l" : null, scope: "col" });
      if (c.v) {
        if (state.sort && state.sort.k === c.k) th.setAttribute("aria-sort", state.sort.dir === 1 ? "descending" : "ascending");
        th.appendChild(h("button", { text: c.t, onclick: function () {
          state.sort = state.sort && state.sort.k === c.k ? (state.sort.dir === 1 ? { k: c.k, dir: -1 } : null) : { k: c.k, dir: 1 };
          renderView();
        } }));
      } else th.textContent = c.t;
      return th;
    }));
    var tb = h("tbody");
    order.forEach(function (n) {
      var m = metric(a, n, "time");
      var nodeCell = h("td", { class: "l nodecell", title: nodeName(n) + (C.conds(n.raw) ? "\n" + C.conds(n.raw) : "") }, [
        state.sort ? null : h("span", { style: { "padding-left": (n.depth * 14) + "px" } }),
        n.parent !== null ? h("span", { class: "arrow", text: "→ " }) : null,
        n.sub ? h("span", { class: "subn", text: "[" + n.sub + "] " }) : null,
        h("b", { text: n.label }), n.object ? " " + n.object : (n.index ? " " + n.index : "")
      ]);
      var tr = h("tr", { class: (n.id === state.sel ? "sel" : ""), "data-id": n.id, onclick: function () { select(n.id, false); } }, [
        h("td", { class: "muted", text: "#" + n.id }),
        nodeCell,
        h("td", null, h("span", { class: "cell f-" + m.bin, text: n.excl === null ? "–" : C.fmtMs(n.excl) + (R ? " · " + Math.round(n.excl / R * 100) + "%" : "") })),
        h("td", { text: C.fmtMs(n.incl) }),
        h("td", { text: C.fmtNum(n.est), title: "tổng " + C.fmtNum(n.est_total) }),
        h("td", { text: n.analyzed ? (n.never ? "never" : C.fmtNum(n.rows)) : "–", title: n.analyzed ? "tổng " + C.fmtNum(n.act_total) : "" }),
        h("td", null, h("span", { class: "cell f-" + qBin(n.lq), text: C.fmtFactor(n.lq) })),
        h("td", null, h("span", { class: "cell f-" + qBin(n.local_lq), text: C.fmtFactor(n.local_lq) })),
        h("td", { text: n.analyzed ? C.fmtNum(n.loops) : "–" }),
        h("td", { text: Object.keys(n.buf).length ? C.fmtBytes(bufSum(n.buf_excl, READ_KEYS) * C.BLOCK) : "–" }),
        h("td", { text: bufSum(n.buf_excl, TEMP_KEYS) ? C.fmtBytes(bufSum(n.buf_excl, TEMP_KEYS) * C.BLOCK) : "" })
      ]);
      tb.appendChild(tr);
    });
    panel.appendChild(h("div", { class: "tablewrap" }, h("table", { class: "grid" }, [h("thead", null, thead), tb])));
    box.appendChild(panel);
  }

  // ---- tooltip
  var tipEl = null;
  function showTip(host, x, y, kids) {
    hideTip();
    tipEl = h("div", { class: "tip" }, kids);
    host.appendChild(tipEl);
    var hw = host.clientWidth, tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
    var left = x + 14 + tw > hw ? x - tw - 14 : x + 14;
    tipEl.style.left = Math.max(4, left) + "px";
    tipEl.style.top = Math.max(4, y - th - 10) + "px";
  }
  function hideTip() { if (tipEl && tipEl.parentNode) tipEl.parentNode.removeChild(tipEl); tipEl = null; }
  function nodeTip(n, a) {
    return [h("b", { text: n.lq === null ? C.fmtMs(n.excl) : C.fmtFactor(n.lq) }), h("div", { text: "#" + n.id + " " + nodeName(n) }),
      h("div", null, [h("span", { class: "k", text: "mỗi loop: ước lượng " }), C.fmtNum(n.est), h("span", { class: "k", text: " → thực tế " }), n.never ? "never" : C.fmtNum(n.rows)]),
      n.loops > 1 ? h("div", null, [h("span", { class: "k", text: "loops " }), C.fmtNum(n.loops), h("span", { class: "k", text: " · tổng " }), C.fmtNum(n.est_total) + " → " + C.fmtNum(n.act_total)]) : null,
      n.local_lq !== null && Math.abs(n.local_lq) >= 0.18 ? h("div", null, [h("span", { class: "k", text: "sinh ra tại chỗ " }), C.fmtFactor(n.local_lq)]) : null,
      n.excl !== null ? h("div", null, [h("span", { class: "k", text: "thời gian riêng " }), C.fmtMs(n.excl) + (runtime(a) ? " (" + Math.round(n.excl / runtime(a) * 100) + "%)" : "")]) : null];
  }

  // ---- estimate vs actual scatter (log-log)
  function logTicks(D) { var t = []; for (var d = 0; d <= D; d++) t.push(d); return t; }
  function scatterFrame(svg, D, W, H, M) {
    var pw = W - M.l - M.r, ph = H - M.t - M.b;
    var X = function (v) { return M.l + v / D * pw; }, Y = function (v) { return M.t + ph - v / D * ph; };
    var band = [[0, 0], [1, 0], [D, D - 1], [D, D], [D - 1, D], [0, 1]].map(function (p) { return X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1); }).join(" ");
    svg.appendChild(s("polygon", { class: "band", points: band }));
    logTicks(D).forEach(function (d) {
      svg.appendChild(s("line", { class: "gridl", x1: X(d), x2: X(d), y1: M.t, y2: M.t + ph }));
      svg.appendChild(s("line", { class: "gridl", x1: M.l, x2: M.l + pw, y1: Y(d), y2: Y(d) }));
      svg.appendChild(s("text", { class: "tick", x: X(d), y: M.t + ph + 18, "text-anchor": "middle", text: C.fmtNum(Math.pow(10, d)) }));
      svg.appendChild(s("text", { class: "tick", x: M.l - 8, y: Y(d) + 4, "text-anchor": "end", text: C.fmtNum(Math.pow(10, d)) }));
    });
    svg.appendChild(s("line", { class: "axisl", x1: M.l, x2: M.l + pw, y1: M.t + ph, y2: M.t + ph }));
    svg.appendChild(s("line", { class: "axisl", x1: M.l, x2: M.l, y1: M.t, y2: M.t + ph }));
    svg.appendChild(s("line", { class: "diag", x1: X(0), y1: Y(0), x2: X(D), y2: Y(D) }));
    svg.appendChild(s("text", { class: "atitle", x: M.l + pw / 2, y: H - 8, "text-anchor": "middle", text: "Số dòng planner ước lượng mỗi loop (thang log)" }));
    svg.appendChild(s("text", { class: "atitle", transform: "translate(16," + (M.t + ph / 2) + ") rotate(-90)", "text-anchor": "middle", text: "Số dòng thực tế mỗi loop (thang log)" }));
    svg.appendChild(s("text", { class: "tick", x: X(0.15), y: Y(D - 0.25), text: "↑ thực tế nhiều hơn: ước lượng thiếu" }));
    svg.appendChild(s("text", { class: "tick", x: X(D - 0.15), y: Y(0.3), "text-anchor": "end", text: "ước lượng thừa ↓" }));
    var ang = Math.atan2(-(ph / D), pw / D) * 180 / Math.PI, lx = X(D * 0.5), ly = Y(D * 0.5) - 7;
    svg.appendChild(s("text", { class: "tick", x: lx, y: ly, "text-anchor": "middle", transform: "rotate(" + ang.toFixed(1) + " " + lx.toFixed(1) + " " + ly.toFixed(1) + ")", text: "ước lượng đúng" }));
    return { X: X, Y: Y, W: W, H: H };
  }
  // direct labels without collisions: try right, left, above, below of the mark; skip if all overlap
  function placeLabels(svg, sc, items) {
    var boxes = [];
    items.forEach(function (it) {
      var w = it.text.length * 6.6 + 4, hgt = 15, r = it.r || 6, cands = [
        [it.x + r + 5, it.y + 4, "start"], [it.x - r - 5, it.y + 4, "end"], [it.x, it.y - r - 6, "middle"], [it.x, it.y + r + 14, "middle"]];
      for (var i = 0; i < cands.length; i++) {
        var c = cands[i], x0 = c[2] === "start" ? c[0] : c[2] === "end" ? c[0] - w : c[0] - w / 2, y0 = c[1] - 11;
        if (x0 < 2 || x0 + w > sc.W - 2 || y0 < 2 || y0 + hgt > sc.H - 2) continue;
        var hit = boxes.some(function (b) { return x0 < b[0] + b[2] && b[0] < x0 + w && y0 < b[1] + b[3] && b[1] < y0 + hgt; });
        if (hit) continue;
        boxes.push([x0, y0, w, hgt]);
        svg.appendChild(s("text", { class: "dlabel", x: c[0], y: c[1], "text-anchor": c[2], text: it.text }));
        return;
      }
    });
  }
  function viewScatter(box) {
    var a = plan().a;
    var pts = a.nodes.filter(function (n) { return n.lq !== null; });
    var panel = h("div", { class: "panel" }, [
      h("div", { class: "tools" }, [h("span", { class: "muted", style: { "font-size": "12.5px" },
        text: "Mỗi chấm là một node. Đường chéo là ước lượng đúng; vùng xám là sai trong vòng 10×. Diện tích chấm tỉ lệ thời gian riêng. Bấm chấm để xem node." })]),
      legendFor("lq")
    ]);
    if (!pts.length) {
      panel.appendChild(h("div", { class: "empty", text: "Plan không có số dòng thực tế (chưa ANALYZE), nên không vẽ được biểu đồ này." }));
      box.appendChild(panel);
      return;
    }
    var maxV = 1;
    pts.forEach(function (n) { maxV = Math.max(maxV, n.est, n.rows || 0); });
    var D = Math.max(2, Math.ceil(Math.log10(maxV) + 0.05));
    var W = 760, H = 600, M = { l: 70, r: 24, t: 18, b: 56 };
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Biểu đồ ước lượng so với thực tế" });
    var sc = scatterFrame(svg, D, W, H, M);
    var maxEx = 0;
    pts.forEach(function (n) { maxEx = Math.max(maxEx, n.excl || 0); });
    var host = h("div", { class: "chart" });
    var items = pts.map(function (n) {
      var r = maxEx > 0 && n.excl !== null ? 4 + 14 * Math.sqrt(n.excl / maxEx) : 6;
      return { n: n, r: r, x: sc.X(Math.log10(Math.max(n.est, 1))), y: sc.Y(Math.log10(Math.max(n.rows, 1))) };
    }).sort(function (p, q) { return q.r - p.r; });
    items.forEach(function (it) {
      var n = it.n;
      var g = s("g", { tabindex: "0", role: "button", "aria-label": "#" + n.id + " " + nodeName(n) + " " + C.fmtFactor(n.lq) }, [
        s("circle", { cx: it.x, cy: it.y, r: Math.max(it.r, 12), fill: "transparent" }),
        s("circle", { class: "dot f-" + qBin(n.lq) + (n.id === state.sel ? " sel" : ""), cx: it.x, cy: it.y, r: it.r })
      ]);
      function tip() {
        var rect = svg.getBoundingClientRect(), k = rect.width / W;
        showTip(host, it.x * k + 12, it.y * k + 8, nodeTip(n, a));
      }
      g.addEventListener("pointerenter", tip);
      g.addEventListener("focus", tip);
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("blur", hideTip);
      g.addEventListener("click", function () { select(n.id, false); });
      g.addEventListener("keydown", function (e) { if (e.key === "Enter") select(n.id, false); });
      svg.appendChild(g);
    });
    placeLabels(svg, sc, pts.filter(function (n) { return Math.abs(n.lq) >= 1; }).sort(function (p, q) { return Math.abs(q.lq) - Math.abs(p.lq); }).slice(0, 4).map(function (n) {
      var it = items.filter(function (i) { return i.n === n; })[0];
      return { x: it.x, y: it.y, r: it.r, text: "#" + n.id + " " + trunc(n.label + (n.object ? " " + n.object.split(" ")[0] : ""), 28) + " " + C.fmtFactor(n.lq) };
    }));
    host.appendChild(svg);
    panel.appendChild(host);
    var skipped = a.nodes.length - pts.length;
    if (skipped) panel.appendChild(h("div", { class: "note", text: skipped + " node không có trên biểu đồ (node ghi dữ liệu như Update/Insert, node không chạy, hoặc thiếu số thực tế)." }));
    box.appendChild(panel);
  }

  // ---- time icicle
  function viewTime(box) {
    var a = plan().a, R = runtime(a), nodes = a.nodes;
    var sel = h("select", { "aria-label": "Tô màu ô theo", onchange: function (e) { state.icicleColor = e.target.value; renderView(); } },
      [["time", "Thời gian riêng"], ["local", "Sai số tại chỗ"], ["lq", "Sai số ước lượng"]].map(function (m) {
        var o = h("option", { value: m[0], text: m[1] }); if (m[0] === state.icicleColor) o.selected = true; return o; }));
    var panel = h("div", { class: "panel" }, [
      h("div", { class: "tools" }, [h("label", null, ["Tô màu theo", sel]), h("span", { class: "sp" }),
        h("span", { class: "muted", style: { "font-size": "12px" }, text: "Bề rộng = thời gian gồm cả node con; phần trống bên phải mỗi ô là thời gian riêng. Plan của CTE nằm dưới CTE Scan đã kích hoạt nó." })]),
      legendFor(state.icicleColor)
    ]);
    if (!a.summary.has_timing) {
      panel.appendChild(h("div", { class: "empty", text: "Plan không có thời gian từng node (thiếu ANALYZE hoặc TIMING OFF)." }));
      box.appendChild(panel);
      return;
    }
    var host = h("div", { class: "chart icicle" });
    panel.appendChild(host);
    box.appendChild(panel);
    var W = Math.max(320, host.clientWidth - 24), RH = 28;
    var kids = {};
    nodes.forEach(function (n) { kids[n.id] = []; });
    nodes.forEach(function (n) { if (n.time_parent !== null && n.time_parent !== undefined) kids[n.time_parent].push(n.id); });
    var cells = [], maxDepth = 0, total = R || nodes[0].incl || 1;
    (function place(id, x, w, d) {
      var n = nodes[id];
      cells.push({ n: n, x: x, w: w, d: d });
      maxDepth = Math.max(maxDepth, d);
      var ks = kids[id].map(function (c) { return nodes[c]; }).filter(function (k) { return k.incl > 0; });
      var sum = 0;
      ks.forEach(function (k) { sum += k.incl; });
      var scale = n.incl > 0 ? w / Math.max(sum, n.incl) : 0, cx = x;
      ks.forEach(function (k) { var kw = k.incl * scale; place(k.id, cx, kw, d + 1); cx += kw; });
    })(0, 0, W * Math.min(1, (nodes[0].incl || 0) / total), 0);
    var H = (maxDepth + 1) * RH + 4;
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": "Biểu đồ thời gian dạng icicle" });
    cells.forEach(function (c) {
      if (c.w < 0.5) return;
      var n = c.n, bin = state.icicleColor === "time" ? (R ? magBin((n.excl || 0) / R) : "na") : qBin(state.icicleColor === "local" ? n.local_lq : n.lq);
      var g = s("g", { tabindex: "0", role: "button", "aria-label": "#" + n.id + " " + nodeName(n) + " " + C.fmtMs(n.incl) });
      g.appendChild(s("rect", { class: "cellr f-" + bin + (n.id === state.sel ? " sel" : ""), x: c.x, y: c.d * RH, width: Math.max(c.w, 1), height: RH - 2, rx: 3 }));
      if (c.w > 46) {
        var txt = "#" + n.id + " " + n.label + (n.object ? " " + n.object.split(" ")[0] : "") + " · " + C.fmtMs(n.incl);
        g.appendChild(s("text", { class: "o-" + bin, x: c.x + 6, y: c.d * RH + 18, text: trunc(txt, Math.floor((c.w - 10) / 6.6)) }));
      }
      function tip() {
        var rect = svg.getBoundingClientRect(), k = rect.width / W;
        showTip(host, (c.x + Math.min(c.w, 200) / 2) * k + 12, c.d * RH * k + 10, nodeTip(n, a).concat([h("div", null, [h("span", { class: "k", text: "gồm node con " }), C.fmtMs(n.incl)])]));
      }
      g.addEventListener("pointerenter", tip);
      g.addEventListener("focus", tip);
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("blur", hideTip);
      g.addEventListener("click", function () { select(n.id, false); });
      g.addEventListener("keydown", function (e) { if (e.key === "Enter") select(n.id, false); });
      svg.appendChild(g);
    });
    host.appendChild(svg);
    var S = a.summary, other = [];
    if (S.trigger_ms) other.push("trigger " + C.fmtMs(S.trigger_ms));
    if (S.jit_ms) other.push("JIT " + C.fmtMs(S.jit_ms));
    if (R && nodes[0].incl !== null && R - nodes[0].incl > 1) other.push("ngoài cây plan " + C.fmtMs(R - nodes[0].incl));
    if (other.length) panel.appendChild(h("div", { class: "note", text: "Không nằm trong các ô: " + other.join(", ") + "." }));
  }

  // ---- compare
  function viewCompare(box) {
    if (state.plans.length < 2) {
      box.appendChild(h("div", { class: "panel" }, h("div", { class: "empty" }, ["Cần ít nhất hai plan của cùng một câu lệnh (ví dụ PG13 và PG17). ",
        h("button", { class: "btn primary", text: "Mở thêm plan…", onclick: openDialog })])));
      return;
    }
    if (state.cmpA >= state.plans.length) state.cmpA = 0;
    if (state.cmpB >= state.plans.length || state.cmpB === state.cmpA) state.cmpB = state.cmpA === 0 ? 1 : 0;
    var A = state.plans[state.cmpA], B = state.plans[state.cmpB];
    function pick(id, val, cb) {
      return h("select", { id: id, onchange: function (e) { cb(parseInt(e.target.value, 10)); renderView(); } }, state.plans.map(function (p, i) {
        var o = h("option", { value: String(i), text: p.label }); if (i === val) o.selected = true; return o; }));
    }
    var panel = h("div", { class: "panel" });
    panel.appendChild(h("div", { class: "cmp-head" }, [
      h("label", null, ["A (gốc) ", pick("cmpA", state.cmpA, function (v) { state.cmpA = v; })]),
      h("label", null, ["B ", pick("cmpB", state.cmpB, function (v) { state.cmpB = v; })]),
      h("span", { class: "muted", style: { "font-size": "12.5px" }, text: "Node được ghép theo bảng/CTE mà nó đọc và loại node, không phụ thuộc số thứ tự SubPlan." })
    ]));
    var lo = Math.min(state.cmpA, state.cmpB), hi = Math.max(state.cmpA, state.cmpB);
    var raw = getMatch(lo, hi), pairs = raw.map(function (p) { return state.cmpA === lo ? p : [p[1], p[0]]; });
    var SA = A.a.summary, SB = B.a.summary;
    function ratioCell(x, y) {
      if (!x || !y) return h("td", { text: "–" });
      var r = y / x;
      if (Math.abs(Math.log10(r)) < 0.02) return h("td", { text: "≈" });
      return h("td", { class: r > 1 ? "delta-up" : "delta-down", text: (r > 1 ? "▲ ×" + C.fmtNum(Math.round(r * 10) / 10) : "▼ ÷" + C.fmtNum(Math.round(10 / r) / 10)) });
    }
    var bA = (SA.shared_hit || 0) + (SA.shared_read || 0), bB = (SB.shared_hit || 0) + (SB.shared_read || 0);
    var rows = [
      ["Thời gian thực thi", C.fmtMs(SA.execution_ms), C.fmtMs(SB.execution_ms), SA.execution_ms, SB.execution_ms],
      ["Lập kế hoạch", C.fmtMs(SA.planning_ms), C.fmtMs(SB.planning_ms), SA.planning_ms, SB.planning_ms],
      ["Sai số lớn nhất", SA.max_abs_lq === null ? "–" : "×" + C.fmtNum(Math.round(Math.pow(10, SA.max_abs_lq))), SB.max_abs_lq === null ? "–" : "×" + C.fmtNum(Math.round(Math.pow(10, SB.max_abs_lq))),
        SA.max_abs_lq === null ? null : Math.pow(10, SA.max_abs_lq), SB.max_abs_lq === null ? null : Math.pow(10, SB.max_abs_lq)],
      ["Buffers shared", C.fmtBytes(bA * C.BLOCK), C.fmtBytes(bB * C.BLOCK), bA, bB],
      ["File tạm", C.fmtBytes((SA.temp_blocks || 0) * C.BLOCK), C.fmtBytes((SB.temp_blocks || 0) * C.BLOCK), SA.temp_blocks, SB.temp_blocks],
      ["Số node", String(SA.nodes), String(SB.nodes), SA.nodes, SB.nodes]
    ];
    panel.appendChild(h("div", { class: "tablewrap", style: { "max-height": "none" } }, h("table", { class: "grid" }, [
      h("thead", null, h("tr", null, [h("th", { class: "l", text: "Chỉ số" }), h("th", { text: A.label }), h("th", { text: B.label }), h("th", { text: "B so với A" })])),
      h("tbody", null, rows.map(function (r) { return h("tr", { style: { cursor: "default" } }, [h("td", { class: "l", text: r[0] }), h("td", { text: r[1] }), h("td", { text: r[2] }), ratioCell(r[3], r[4])]); }))
    ])));
    // matched nodes, biggest slowdown first
    var mt = pairs.map(function (p) { return { a: A.a.nodes[p[0]], b: B.a.nodes[p[1]] }; });
    mt.sort(function (x, y) { return ((y.b.excl || 0) - (y.a.excl || 0)) - ((x.b.excl || 0) - (x.a.excl || 0)) || x.b.id - y.b.id; });
    var maxD = 0;
    mt.forEach(function (m) { maxD = Math.max(maxD, Math.abs((m.b.excl || 0) - (m.a.excl || 0))); });
    function side(n) {
      return [h("td", { text: n.analyzed ? C.fmtNum(n.est) + " → " + (n.never ? "never" : C.fmtNum(n.rows)) : C.fmtNum(n.est) }),
        h("td", null, h("span", { class: "cell f-" + qBin(n.lq), text: C.fmtFactor(n.lq) })),
        h("td", { text: C.fmtMs(n.excl) + (n.analyzed && n.loops > 1 ? " ×" + C.fmtNum(n.loops) : "") })];
    }
    var tb = h("tbody");
    mt.forEach(function (m) {
      var d = (m.b.excl || 0) - (m.a.excl || 0);
      tb.appendChild(h("tr", { onclick: function () { state.cur = state.cmpB; state.view = "graph"; state.sel = m.b.id; renderAll(); centerOn(m.b.id, false); } }, [
        h("td", { class: "l nodecell", title: nodeName(m.b) }, [m.b.sub ? h("span", { class: "subn", text: "[" + m.b.sub + "] " }) : null, h("b", { text: m.b.label }), m.b.object ? " " + m.b.object : "",
          m.a.label !== m.b.label ? h("span", { class: "muted", text: " (A: " + m.a.label + ")" }) : null]),
      ].concat(side(m.a), side(m.b), [h("td", { class: d > 0 ? "delta-up" : d < 0 ? "delta-down" : "", text: (A.a.summary.has_timing && B.a.summary.has_timing) ? (d > 0 ? "+" : d < 0 ? "−" : "") + C.fmtMs(Math.abs(d)) : "–" })])));
    });
    var head2 = h("tr", null, [h("th", { class: "l", text: "Node (theo B)" }), h("th", { text: A.label + " dòng" }), h("th", { text: "sai số" }), h("th", { text: "riêng" }),
      h("th", { text: B.label + " dòng" }), h("th", { text: "sai số" }), h("th", { text: "riêng" }), h("th", { text: "Δ riêng" })]);
    panel.appendChild(h("div", { class: "note", text: "Node ghép được giữa hai plan, chênh lệch thời gian riêng lớn nhất trước. Số dòng: ước lượng → thực tế (mỗi loop). Bấm một dòng để mở node bên " + B.label + "." }));
    panel.appendChild(h("div", { class: "tablewrap" }, h("table", { class: "grid" }, [h("thead", null, head2), tb])));
    var usedA = {}, usedB = {};
    pairs.forEach(function (p) { usedA[p[0]] = true; usedB[p[1]] = true; });
    var onlyA = A.a.nodes.filter(function (n) { return !usedA[n.id]; }), onlyB = B.a.nodes.filter(function (n) { return !usedB[n.id]; });
    if (onlyA.length || onlyB.length) {
      panel.appendChild(h("div", { class: "only" }, [h("b", { text: "Khác biệt về hình dạng plan. " }),
        onlyA.length ? h("div", null, ["Chỉ có ở " + A.label + ": ", h("code", { text: onlyA.map(function (n) { return "#" + n.id + " " + nodeName(n); }).join(" · ") })]) : null,
        onlyB.length ? h("div", null, ["Chỉ có ở " + B.label + ": ", h("code", { text: onlyB.map(function (n) { return "#" + n.id + " " + nodeName(n); }).join(" · ") })]) : null]));
    }
    // dumbbell: how each matched node's estimate/actual moved from A to B
    var pts = mt.filter(function (m) { return m.a.lq !== null && m.b.lq !== null; });
    if (pts.length) {
      var maxV = 1;
      pts.forEach(function (m) { maxV = Math.max(maxV, m.a.est, m.a.rows, m.b.est, m.b.rows); });
      var D = Math.max(2, Math.ceil(Math.log10(maxV) + 0.05)), W = 760, H = 600, M = { l: 70, r: 24, t: 18, b: 56 };
      var svg = s("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Ước lượng và thực tế của các node ghép, A và B" });
      var sc = scatterFrame(svg, D, W, H, M), host = h("div", { class: "chart" });
      function P(n) { return [sc.X(Math.log10(Math.max(n.est, 1))), sc.Y(Math.log10(Math.max(n.rows, 1)))]; }
      pts.forEach(function (m) { var pa = P(m.a), pb = P(m.b); svg.appendChild(s("line", { class: "link", x1: pa[0], y1: pa[1], x2: pb[0], y2: pb[1] })); });
      pts.forEach(function (m) {
        [["a", "sA", A, m.a], ["b", "sB", B, m.b]].forEach(function (z) {
          var p = P(z[3]), g = s("g", { tabindex: "0", role: "button", "aria-label": z[2].label + " #" + z[3].id + " " + nodeName(z[3]) });
          g.appendChild(s("circle", { cx: p[0], cy: p[1], r: 12, fill: "transparent" }));
          g.appendChild(s("circle", { class: z[1], cx: p[0], cy: p[1], r: 6 }));
          function tip() { var rect = svg.getBoundingClientRect(), k = rect.width / W; showTip(host, p[0] * k + 12, p[1] * k + 8, [h("div", { class: "k", text: z[2].label })].concat(nodeTip(z[3], z[2].a))); }
          g.addEventListener("pointerenter", tip); g.addEventListener("focus", tip);
          g.addEventListener("pointerleave", hideTip); g.addEventListener("blur", hideTip);
          svg.appendChild(g);
        });
      });
      var big = pts.slice().sort(function (x, y) { return Math.abs(y.b.lq - y.a.lq) - Math.abs(x.b.lq - x.a.lq); }).slice(0, 3);
      placeLabels(svg, sc, big.filter(function (m) { return Math.abs(m.b.lq - m.a.lq) >= 0.5; }).map(function (m) {
        var pb = P(m.b);
        return { x: pb[0], y: pb[1], r: 7, text: "#" + m.b.id + " " + trunc(m.b.label + (m.b.object ? " " + m.b.object.split(" ")[0] : ""), 26) };
      }));
      host.appendChild(svg);
      panel.appendChild(h("div", { class: "legend" }, [h("span", { text: "Mỗi đoạn nối cùng một node ở hai plan:" }),
        h("span", null, [s("svg", { width: 14, height: 14, style: "vertical-align:-2px;margin-right:4px", "aria-hidden": "true" }, s("circle", { cx: 7, cy: 7, r: 5, fill: "none", stroke: "#2a78d6", "stroke-width": 2.5 })), A.label]),
        h("span", null, [s("svg", { width: 14, height: 14, style: "vertical-align:-2px;margin-right:4px", "aria-hidden": "true" }, s("circle", { cx: 7, cy: 7, r: 6, fill: "#eb6834" })), B.label]),
        h("span", { class: "muted", text: "Chấm nằm xa đường chéo = ước lượng sai; đoạn dài = ước lượng hoặc thực tế đổi giữa hai plan." })]));
      panel.appendChild(host);
    }
    box.appendChild(panel);
  }

  // ------------------------------------------------------------------ inspector
  function kv(pairs) {
    var dl = h("dl", { class: "kv" });
    pairs.forEach(function (p) { if (p) { dl.appendChild(h("dt", { text: p[0] })); dl.appendChild(h("dd", { text: p[1], title: p[2] || null })); } });
    return dl;
  }
  function sect(title, kids) { return h("div", { class: "sect" }, [h("h3", { text: title })].concat(kids)); }
  function renderInspector() {
    var box = $("inspector");
    clear(box);
    var p = plan();
    if (!p || state.sel === null || !p.a.nodes[state.sel]) {
      box.appendChild(h("div", { class: "empty", text: "Chọn một node trên sơ đồ, bảng hoặc biểu đồ để xem chi tiết: thời gian, số dòng, sai số sinh ra ở đâu, bộ nhớ và điều kiện." }));
      return;
    }
    var a = p.a, n = a.nodes[state.sel], R = runtime(a), r = n.raw;
    var tags = [h("span", { class: "tag", text: "#" + n.id })];
    if (n.sub) tags.push(h("span", { class: "tag", text: n.sub }));
    else if (n.rel) tags.push(h("span", { class: "tag", text: n.rel }));
    if (n.scope !== "main") tags.push(h("span", { class: "tag", text: n.scope.indexOf("cte:") === 0 ? "trong CTE " + n.scope.slice(4) : "trong sub-plan" }));
    if (n.hot) tags.push(h("span", { class: "tag hot", text: "đường nóng" }));
    if (n.never) tags.push(h("span", { class: "tag", text: "never executed" }));
    if (r["Parallel Aware"]) tags.push(h("span", { class: "tag", text: "song song" }));
    box.appendChild(h("div", { class: "insp-head" }, [h("div", { class: "ttl", text: n.label }), n.object || n.index ? h("div", { class: "obj", text: (n.object || "") + (n.index && n.object ? " · " : "") + (n.index ? "index " + n.index : "") }) : null,
      h("div", { class: "tags" }, tags)]));
    // findings on this node
    if (n.insights.length) {
      box.appendChild(sect("Phát hiện", n.insights.map(function (i) {
        var ins = a.insights[i], rr = C.renderInsight(ins, RULES);
        return h("details", { class: "find", open: true }, [h("summary", null, [sevChip(ins.sev), h("span", { text: rr.title }), h("span")]),
          h("div", { class: "body" }, [h("div", { text: rr.detail }), rr.fix ? h("div", null, [h("b", { text: "Hướng xử lý" }), rr.fix]) : null])]);
      })));
    }
    if (n.analyzed) {
      box.appendChild(sect("Thời gian", [kv([
        ["Riêng", n.excl === null ? "–" : C.fmtMs(n.excl) + (R ? " (" + (n.excl / R * 100).toFixed(1) + "%)" : "")],
        ["Gồm node con", C.fmtMs(n.incl)],
        r["Actual Total Time"] !== undefined ? ["Mỗi loop", C.fmtMs(r["Actual Total Time"]) + (r["Actual Startup Time"] !== undefined ? " (dòng đầu sau " + C.fmtMs(r["Actual Startup Time"]) + ")" : "")] : null
      ])]));
    }
    // rows & estimation error
    var rowKids = [kv([
      ["Ước lượng mỗi loop", C.fmtNum(n.est), String(n.est)],
      n.analyzed ? ["Thực tế mỗi loop", n.never ? "never executed" : C.fmtNum(n.rows), String(n.rows)] : null,
      n.analyzed ? ["Loops", C.fmtNum(n.loops)] : null,
      n.analyzed && n.loops > 1 ? ["Tổng: ước lượng → thực tế", C.fmtNum(n.est_total) + " → " + C.fmtNum(n.act_total)] : null,
      n.removed ? ["Bị lọc bỏ (tổng)", C.fmtNum(n.removed)] : null,
      ["Độ rộng dòng", n.width + " B"]
    ])];
    if (n.lq !== null) {
      rowKids.push(h("div", { class: "decomp" }, ["Sai số tích luỹ ", h("span", { class: "cell f-" + qBin(n.lq), text: C.fmtFactor(n.lq) }),
        " = đầu vào ", h("span", { class: "cell f-" + qBin(n.base_lq), text: C.fmtFactor(n.base_lq) }),
        " × sinh ra tại node ", h("span", { class: "cell f-" + qBin(n.local_lq), text: C.fmtFactor(n.local_lq) })]));
      rowKids.push(h("div", { class: "muted", style: { "font-size": "12px" }, text: explainBase(n, a) }));
    }
    box.appendChild(sect("Số dòng và sai số ước lượng", rowKids));
    // propagation chain: this node and its ancestors
    var chain = [], c = n;
    while (c) { chain.push(c); c = c.parent !== null ? a.nodes[c.parent] : null; }
    var withQ = chain.filter(function (x) { return x.lq !== null; });
    if (withQ.length > 1) {
      var mx = 3;
      withQ.forEach(function (x) { mx = Math.max(mx, Math.abs(x.lq)); });
      var grid = h("div", { class: "prop" });
      withQ.forEach(function (x) {
        var w = Math.abs(x.lq) / mx * 50, bar = h("i", { class: "f-" + qBin(x.lq), style: x.lq >= 0 ? { left: "50%", width: w + "%" } : { left: (50 - w) + "%", width: w + "%" } });
        grid.appendChild(h("span", { class: "who" + (x.id === n.id ? " me" : ""), text: "#" + x.id + " " + x.label, title: nodeName(x) }));
        grid.appendChild(h("span", { class: "pbar" }, bar));
        grid.appendChild(h("span", { class: "num", style: { "text-align": "right" }, text: C.fmtFactor(x.lq) }));
      });
      box.appendChild(sect("Sai số lan lên các node cha", [h("div", { class: "muted", style: { "font-size": "12px", "margin-bottom": "6px" }, text: "Trái: ước lượng thừa · phải: ước lượng thiếu (thang log)." }), grid]));
    }
    // buffers / memory
    var mem = [];
    if (r["Sort Method"]) mem.push(["Sort", r["Sort Method"] + (r["Sort Space Used"] !== undefined ? " · " + (r["Sort Space Type"] || "") + " " + C.fmtBytes(r["Sort Space Used"] * 1024) : "")]);
    if (r["Hash Batches"] !== undefined) mem.push(["Hash", "Buckets " + C.fmtNum(r["Hash Buckets"]) + " · Batches " + r["Hash Batches"] + (r["Original Hash Batches"] && r["Original Hash Batches"] !== r["Hash Batches"] ? " (dự kiến " + r["Original Hash Batches"] + ")" : "") + " · " + C.fmtBytes((r["Peak Memory Usage"] || 0) * 1024)]);
    if (r["HashAgg Batches"] !== undefined) mem.push(["HashAgg", "Batches " + r["HashAgg Batches"] + (r["Peak Memory Usage"] ? " · " + C.fmtBytes(r["Peak Memory Usage"] * 1024) : "") + (r["Disk Usage"] ? " · đĩa " + C.fmtBytes(r["Disk Usage"] * 1024) : "")]);
    if (r["Cache Hits"] !== undefined) mem.push(["Memoize", "trúng " + C.fmtNum(r["Cache Hits"]) + " · trượt " + C.fmtNum(r["Cache Misses"]) + " · đẩy ra " + C.fmtNum(r["Cache Evictions"] || 0)]);
    if (r["Heap Fetches"] !== undefined) mem.push(["Heap Fetches", C.fmtNum(r["Heap Fetches"])]);
    if (r["Workers Planned"] !== undefined) mem.push(["Worker", (r["Workers Launched"] !== undefined ? r["Workers Launched"] : "?") + " / " + r["Workers Planned"] + " dự kiến"]);
    var bufRows = C.BUF_KEYS.filter(function (k) { return n.buf[k]; });
    var memKids = [];
    if (mem.length) memKids.push(kv(mem));
    if (bufRows.length) {
      memKids.push(h("table", { class: "grid", style: { "margin-top": "6px" } }, [h("thead", null, h("tr", null, [h("th", { class: "l", text: "Buffers" }), h("th", { text: "Gồm con" }), h("th", { text: "Riêng" })])),
        h("tbody", null, bufRows.map(function (k) {
          return h("tr", { style: { cursor: "default" } }, [h("td", { class: "l", text: k.replace(" Blocks", "").toLowerCase() }), h("td", { text: C.fmtBytes(n.buf[k] * C.BLOCK), title: n.buf[k] + " block" }),
            h("td", { text: C.fmtBytes((n.buf_excl[k] || 0) * C.BLOCK), title: (n.buf_excl[k] || 0) + " block" })]);
        }))]));
    }
    if (memKids.length) box.appendChild(sect("Bộ nhớ và I/O", memKids));
    // counterparts in other plans
    if (state.plans.length > 1) {
      var mk = [];
      state.plans.forEach(function (o, j) {
        if (j === state.cur) return;
        var cid = counterpart(state.cur, n.id, j);
        if (cid === null) { mk.push(h("div", { class: "muted", text: o.label + ": không có node tương ứng (plan khác hình dạng)." })); return; }
        var m = o.a.nodes[cid];
        mk.push(h("div", { style: { display: "flex", "justify-content": "space-between", gap: "8px", "align-items": "baseline", "flex-wrap": "wrap" } }, [
          h("span", null, [h("b", { text: o.label }), " #" + m.id + " " + m.label + " · ", C.fmtNum(m.est) + " → " + (m.analyzed ? C.fmtNum(m.rows) : "?") + " ",
            h("span", { class: "cell f-" + qBin(m.lq), text: C.fmtFactor(m.lq) }), " · riêng " + C.fmtMs(m.excl) + (m.analyzed && m.loops > 1 ? " ×" + C.fmtNum(m.loops) + " loop" : "")]),
          h("button", { class: "btn small", text: "Mở", onclick: function () { switchPlan(j, m.id); } })]));
      });
      box.appendChild(sect("Cùng node ở plan khác", mk));
    }
    // planner details
    var det = [kv([["Cost", r["Startup Cost"] + " .. " + r["Total Cost"]], r["Join Type"] ? ["Join", r["Join Type"]] : null, r["Strategy"] ? ["Strategy", r["Strategy"]] : null])];
    var shown = { "Node Type": 1, "Parent Relationship": 1, "Subplan Name": 1, "Plans": 1, "Startup Cost": 1, "Total Cost": 1, "Plan Rows": 1, "Plan Width": 1, "Actual Startup Time": 1,
      "Actual Total Time": 1, "Actual Rows": 1, "Actual Loops": 1, "Relation Name": 1, "Alias": 1, "Schema": 1, "Index Name": 1, "CTE Name": 1, "Join Type": 1, "Strategy": 1,
      "Sort Method": 1, "Sort Space Used": 1, "Sort Space Type": 1, "Hash Buckets": 1, "Original Hash Buckets": 1, "Hash Batches": 1, "Original Hash Batches": 1, "Peak Memory Usage": 1,
      "Parallel Aware": 1, "Async Capable": 1, "Partial Mode": 1, "Operation": 1 };
    C.BUF_KEYS.forEach(function (k) { shown[k] = 1; });
    var raw = h("div", { class: "raw" });
    Object.keys(r).forEach(function (k) {
      if (shown[k]) return;
      var v = r[k];
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return;
      raw.appendChild(h("div", null, [h("b", { text: k }), h("code", { text: Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v) })]));
    });
    det.push(raw);
    box.appendChild(sect("Chi tiết planner", det));
  }
  function explainBase(n, a) {
    if (n.type === "CTE Scan") return "Đầu vào của CTE Scan là ước lượng của chính CTE; phần tại chỗ đến từ điều kiện lọc trên CTE (hoặc việc dừng sớm).";
    var data = n.children.map(function (c) { return a.nodes[c]; }).filter(function (c) { return c.rel !== "InitPlan" && c.rel !== "SubPlan"; });
    if (!data.length) return "Node lá: toàn bộ sai số đến từ điều kiện lọc / thống kê của bảng.";
    if (["Nested Loop", "Hash Join", "Merge Join"].indexOf(n.type) >= 0) {
      var jt = n.raw["Join Type"] || "Inner";
      if (jt === "Semi" || jt === "Anti") return "Semi/Anti join: đầu vào là phía ngoài; phần tại chỗ là sai số độ chọn lọc của điều kiện join.";
      return "Join: đầu vào = sai số phía ngoài × sai số phía trong (mỗi loop); phần tại chỗ là sai số độ chọn lọc của điều kiện join.";
    }
    return "Đầu vào là sai số của node con; phần tại chỗ do chính node này (lọc, gom nhóm, giới hạn…).";
  }

  // ------------------------------------------------------------------ selection & plan switching
  function select(id, fromFinding) {
    state.sel = id;
    renderInspector();
    if (state.view === "graph") { applyGraphClasses(); centerOn(id, !fromFinding); }
    else if (fromFinding && state.view !== "table") { state.view = "graph"; renderTabs(); renderView(); centerOn(id, false); }
    else renderView();
    if (fromFinding) { var w = $("view"); if (w && w.scrollIntoView) w.scrollIntoView({ block: "start", behavior: "smooth" }); }
  }
  function defaultSel(a) {
    var withNode = a.insights.filter(function (i) { return i.node !== null; });
    if (withNode.length) return withNode[0].node;
    return a.summary.worst_node !== null ? a.summary.worst_node : 0;
  }
  function switchPlan(i, nodeId) {
    state.cur = i;
    state.sel = nodeId !== undefined ? nodeId : defaultSel(state.plans[i].a);
    state.search = "";
    renderAll();
    if (state.view === "graph" && nodeId !== undefined) requestAnimationFrame(function () { centerOn(nodeId, false); });
  }
  function removePlan(i) {
    state.plans.splice(i, 1);
    state.matchCache = {};
    state.tf = {};
    if (state.cur >= state.plans.length) state.cur = Math.max(0, state.plans.length - 1);
    state.sel = plan() ? defaultSel(plan().a) : null;
    renderAll();
  }
  function renderAll() {
    renderHeader(); renderKpis(); renderFindings(); renderTabs(); renderView(); renderInspector();
    var ex = $("example");
    if (ex) ex.hidden = !(DATA.example && state.plans.some(function (p) { return p.example; }));
  }

  // ------------------------------------------------------------------ open / paste / drop
  function openDialog() { $("dlg-err").textContent = ""; var d = $("dlg"); if (d.showModal) d.showModal(); else d.setAttribute("open", ""); $("dlg-text").focus(); }
  function closeDialog() { var d = $("dlg"); if (d.close) d.close(); else d.removeAttribute("open"); }
  function ingest(text, label, source) {
    var res = C.parseAny(text);
    var i = addPlan({ label: label || ("Plan " + (state.plans.length + 1)), source: source || "dán vào", format: res.fmt, meta: {}, plan: res.obj });
    state.matchCache = {};
    return i;
  }
  function readFiles(files, replace) {
    var list = Array.prototype.slice.call(files), done = 0, errs = [], last = null;
    if (replace && list.length) { state.plans = []; state.matchCache = {}; state.tf = {}; }
    list.forEach(function (f) {
      var rd = new FileReader();
      rd.onload = function () {
        try {
          var m = /(?:^|[._-])pg(\d{1,2})(?=[._-]|$)/i.exec(f.name);
          last = ingest(String(rd.result), m ? "PG" + m[1] : f.name.replace(/\.(json|txt|log|sql|explain)$/i, ""), f.name);
        } catch (e) { errs.push(f.name + ": " + e.message); }
        if (++done === list.length) finishLoad(last, errs);
      };
      rd.onerror = function () { errs.push(f.name + ": không đọc được file"); if (++done === list.length) finishLoad(last, errs); };
      rd.readAsText(f);
    });
  }
  function finishLoad(idx, errs) {
    if (errs.length) { $("dlg-err").textContent = errs.join(" · "); if (!$("dlg").open) openDialog(); }
    else closeDialog();
    if (idx !== null) { state.plans.forEach(function (p) { p.example = false; }); switchPlan(idx); }
  }
  function setupDialog() {
    $("open").addEventListener("click", openDialog);
    $("dlg-cancel").addEventListener("click", closeDialog);
    function go(replace) {
      var text = $("dlg-text").value;
      if (!text.trim()) { $("dlg-err").textContent = "Dán output của EXPLAIN vào ô trên, hoặc chọn file."; return; }
      try {
        if (replace) { state.plans = []; state.matchCache = {}; state.tf = {}; }
        var i = ingest(text, $("dlg-label").value.trim());
        state.plans.forEach(function (p) { p.example = false; });
        $("dlg-text").value = ""; $("dlg-label").value = "";
        closeDialog();
        switchPlan(i);
      } catch (e) { $("dlg-err").textContent = "Không đọc được plan: " + e.message; }
    }
    $("dlg-add").addEventListener("click", function () { go(false); });
    $("dlg-replace").addEventListener("click", function () { go(true); });
    $("dlg-file").addEventListener("change", function (e) { if (e.target.files.length) readFiles(e.target.files, false); e.target.value = ""; });
    var depth = 0, overlay = $("drop");
    window.addEventListener("dragenter", function (e) { if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0) { depth++; overlay.hidden = false; } });
    window.addEventListener("dragleave", function () { depth = Math.max(0, depth - 1); if (!depth) overlay.hidden = true; });
    window.addEventListener("dragover", function (e) { e.preventDefault(); });
    window.addEventListener("drop", function (e) { e.preventDefault(); depth = 0; overlay.hidden = true; if (e.dataTransfer && e.dataTransfer.files.length) readFiles(e.dataTransfer.files, false); });
  }
  function setupTheme() {
    $("theme").addEventListener("click", function () {
      var r = document.documentElement, dark = r.dataset.theme === "dark" || (!r.dataset.theme && window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
      r.dataset.theme = dark ? "light" : "dark";
      if (state.view === "time") renderView();
    });
  }

  // ------------------------------------------------------------------ boot
  (DATA.plans || []).forEach(function (p) {
    try { addPlan(p); } catch (e) { console.error("planviz: cannot analyse " + p.label, e); }
  });
  if (state.plans.length) state.sel = defaultSel(state.plans[0].a);
  setupDialog();
  setupTheme();
  renderAll();
  var rz = null;
  window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(function () { if (state.view === "time") renderView(); }, 150); });
})();
