"use strict";
// SIEVE control room. Renders frames + events pushed over SSE by sieve.dashboard.server.

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const ARMS = [
  { key: "random", label: "random", dash: "1.5 4", cls: "muted" },
  { key: "descriptor", label: "descriptor-ranked", dash: "7 4", cls: "muted" },
  { key: "uniform", label: "uniform tier-2", dash: "8 3 1.5 3", cls: "muted" },
  { key: "sieve", label: "SIEVE", dash: "", cls: "sieve" },
];
const ROLES = [
  { role: "scout", x: 160 }, { role: "allocator", x: 400 }, { role: "critic", x: 640 },
];
const TIMEOUT_HOLD_MS = 2600;  // keep a killed slot red long enough to read

const state = {
  run: null, speed: 1, frame: null, frameAt: 0, lastEventTs: 0,
  filter: "all", slotHold: {}, seenSlots: new Set(), events: [],
};

const params = new URLSearchParams(location.search);
if (params.get("theme") === "light") document.documentElement.dataset.theme = "light";

// ---------------------------------------------------------------------------- formatting
const pad = (n) => String(n).padStart(2, "0");
function hms(s) {
  s = Math.max(0, Math.floor(s));
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}
const clock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const n0 = (x) => (x ?? 0).toLocaleString("en-US");
const f2 = (x) => (x ?? 0).toFixed(2);
const f3 = (x) => (x == null ? "–" : x.toFixed(3));
const short = (s, n = 22) => (!s ? "" : s.length > n ? s.slice(0, n - 1) + "…" : s);
const el = (tag, attrs = {}, parent) => {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
};

// ---------------------------------------------------------------------------- boot
async function boot() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  state.speed = cfg.speed || 1;
  if (cfg.replay) { $("replay").hidden = false; $("replay-speed").textContent = `${cfg.speed}x`; }
  const sel = $("run-select");
  const want = params.get("run") || cfg.default_run;
  sel.innerHTML = "";
  if (!cfg.runs.length) sel.appendChild(new Option("no runs yet", ""));
  for (const r of cfg.runs) sel.appendChild(new Option(`${r.run_id}  [${r.arm}]`, r.run_id, false, r.run_id === want));
  sel.onchange = () => { params.set("run", sel.value); location.search = params.toString(); };
  state.run = want || null;
  connect();
  document.querySelectorAll(".filters button").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("on", x === b));
    state.filter = b.dataset.f; renderEvents(true);
  });
  setInterval(tick, 250);
  window.addEventListener("resize", () => state.frame && renderCurve(state.frame));
  renderTree({ agents: [], run: null });
  renderCurve({ curves: {}, run: null });
}

function connect() {
  const es = new EventSource(`/api/stream?run=${encodeURIComponent(state.run || "")}`);
  es.addEventListener("frame", (m) => { state.frame = JSON.parse(m.data); state.frameAt = performance.now(); render(state.frame); });
  es.addEventListener("events", (m) => addEvents(JSON.parse(m.data)));
  es.onerror = () => { /* EventSource reconnects by itself */ };
}

function tick() {  // keep the elapsed clock and worker bars moving between frames
  const f = state.frame;
  if (!f || !f.run) return;
  const dt = f.run.finished ? 0 : ((performance.now() - state.frameAt) / 1000) * state.speed;
  $("v-elapsed").textContent = hms(f.run.elapsed_s + dt);
  renderPool(f, f.T + dt);
}

// ---------------------------------------------------------------------------- render
function render(f) {
  renderVitals(f);
  renderTree(f);
  renderPool(f, f.T);
  renderCurve(f);
}

function renderVitals(f) {
  const v = f.vitals, r = f.run;
  if (!r || !v) return;
  const budgetH = r.budget_cpu_s / 3600, spentH = v.spent_cpu_s / 3600;
  $("v-spent").textContent = f2(spentH);
  $("v-budget").textContent = f2(budgetH);
  const pct = Math.min(100, (100 * v.spent_cpu_s) / r.budget_cpu_s);
  $("v-budget-bar").style.width = `${pct}%`;
  $("v-inflight-bar").style.left = `${pct}%`;
  $("v-inflight-bar").style.width = `${Math.min(100 - pct, (100 * v.inflight_cpu_s) / r.budget_cpu_s)}%`;
  $("v-interventions").textContent = v.interventions;
  const b = v.best;
  $("v-best").textContent = b ? f3(b.uptake_molkg) : "–";
  const chip = $("v-best-tier");
  chip.textContent = b ? `TIER ${b.tier}${b.tier === 1 ? " · ±18%" : ""}` : "";
  chip.classList.toggle("t2", !!b && b.tier === 2);
  $("v-best-name").textContent = b ? `${b.structure} · ±${f3(b.uptake_err)} reported` : " ";
  $("v-best-t2").textContent = v.best_t2 ? `best tier-2 confirmed: ${f3(v.best_t2.uptake_molkg)} ${short(v.best_t2.structure, 26)}` : "no tier-2 confirmation yet";
  const c = v.counts;
  $("v-counts").textContent = `${n0(c.t0)} / ${n0(c.t1)} / ${n0(c.t2)}`;
  $("v-timeouts").textContent = n0(v.timeouts);
  const rec = [];
  if (v.worker_restarts) rec.push(`${v.worker_restarts} worker restart${v.worker_restarts > 1 ? "s" : ""}`);
  if (v.resumes) rec.push(`${v.resumes} resume${v.resumes > 1 ? "s" : ""}`);
  if (v.lost) rec.push(`${v.lost} lost in crash, re-run`);
  $("v-recovery").textContent = rec.join(" · ") || " ";
  const usd = v.spend.reduce((a, s) => a + (s.usd || 0), 0);
  $("v-spend").textContent = `$${usd.toFixed(2)}`;
  $("v-spend-split").textContent = v.spend.length
    ? v.spend.map((s) => `${short(s.model || "?", 14)} $${(s.usd || 0).toFixed(2)}`).join(" · ")
    : r.arm === "sieve" ? " " : "baseline arm: no model calls";
  renderSpark((f.curves[r.arm] || {}).points || []);
}

function renderSpark(pts) {
  const svg = $("v-spark");
  svg.innerHTML = "";
  if (pts.length < 2) return;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = 0, x1 = Math.max(...xs) || 1, y0 = Math.min(...ys), y1 = Math.max(...ys);
  const X = (x) => ((x - x0) / (x1 - x0 || 1)) * 276 + 2, Y = (y) => 38 - ((y - y0) / (y1 - y0 || 1)) * 34;
  let d = `M${X(xs[0])},${Y(ys[0])}`;
  for (let i = 1; i < pts.length; i++) d += `H${X(xs[i])}V${Y(ys[i])}`;
  el("path", { d, fill: "none", stroke: "var(--accent)", "stroke-width": 1.5 }, svg);
  el("circle", { cx: X(xs.at(-1)), cy: Y(ys.at(-1)), r: 3, fill: "var(--accent)" }, svg);
}

// agent tree: fixed layout, orchestrator above, three specialists below
function renderTree(f) {
  const svg = $("tree");
  const agents = f.agents || [];
  const latest = {};
  for (const a of agents) latest[a.role] = a;  // newest instance per role wins
  const orch = latest.orchestrator;
  const key = JSON.stringify([f.run && f.run.run_id, agents.map((a) => [a.agent_id, a.state])]);
  if (svg.dataset.key === key) return;
  svg.dataset.key = key;
  svg.innerHTML = "";
  if (!agents.length) {
    const arm = f.run ? f.run.arm : null;
    const t = el("text", { x: 400, y: 150, "text-anchor": "middle", class: "placeholder" }, svg);
    t.textContent = !f.run ? "No run selected." : arm === "sieve" ? "Agents not started yet."
      : `Baseline arm "${arm}": a fixed policy, no agents.`;
    return;
  }
  const top = { x: 400, y: 70 };
  for (const r of ROLES) {
    if (!latest[r.role]) continue;
    el("path", { class: `edge e-${r.role}`, d: `M${top.x},${top.y + 30} C${top.x},${top.y + 90} ${r.x},${150} ${r.x},${200}` }, svg);
  }
  node(svg, orch || { role: "orchestrator", state: "idle" }, top.x, top.y);
  for (const r of ROLES) if (latest[r.role]) node(svg, latest[r.role], r.x, 230);
}

function node(svg, a, x, y) {
  const g = el("g", { class: `node ${a.state || "idle"}`, transform: `translate(${x - 90},${y - 30})` }, svg);
  el("rect", { width: 180, height: 60, rx: 6 }, g);
  el("text", { x: 14, y: 24, class: "role" }, g).textContent = a.role;
  el("text", { x: 14, y: 44, class: "model" }, g).textContent = short(a.model || "no model", 20);
  el("text", { x: 166, y: 24, "text-anchor": "end", class: "state" }, g).textContent = (a.state || "idle").toUpperCase();
  if (a.state === "failed") el("path", { class: "glyph", d: "M158,36 l8,14 h-16 z M157.2,41 h1.6 v5 h-1.6z" }, g);
}

function flashEdge(role) {
  const e = document.querySelector(`.tree .e-${role}`);
  if (!e) return;
  e.classList.add("hot");
  setTimeout(() => e.classList.remove("hot"), 400);
}

// worker pool: one slot per core
function renderPool(f, T) {
  const pool = $("pool");
  const ws = f.workers || [];
  $("pool-note").textContent = ws.length ? `· ${ws.length} workers` : "";
  if (!ws.length) {
    pool.innerHTML = `<div class="empty">${f.run ? "No workers have started." : "Select a run."}</div>`;
    return;
  }
  const now = performance.now();
  pool.innerHTML = "";
  for (const w of ws) {
    let st = w.state || "idle";
    const hold = state.slotHold[w.slot];
    if (st === "timeout" && !hold) state.slotHold[w.slot] = { until: now + TIMEOUT_HOLD_MS, w: { ...w } };
    let shown = w;
    if (hold && now < hold.until) { st = "timeout"; shown = hold.w; }
    else if (hold && now >= hold.until && st !== "timeout") delete state.slotHold[w.slot];
    const d = document.createElement("div");
    d.className = `slot ${st}`;
    if (!state.seenSlots.has(w.worker_id)) { d.classList.add("fresh"); state.seenSlots.add(w.worker_id); }
    const limit = shown.limit_cpu_s || (shown.est_cpu_s ? shown.est_cpu_s * 4 : null);
    const cpu = st === "running" ? (shown.cpu_now ?? 0) : 0;
    const pct = limit ? Math.min(100, (100 * cpu) / limit) : 0;
    let name, meta;
    if (st === "running") { name = shown.structure; meta = `T${shown.tier} ${shown.mode} · ${Math.round(cpu)} / ${limit ? Math.round(limit) : "?"}s CPU`; }
    else if (st === "timeout") { name = shown.last_structure || shown.structure; meta = `<span class="skill">KILLED</span> at ${Math.round(shown.limit_cpu_s || limit || 0)}s CPU`; }
    else if (st === "dead") { name = "worker died"; meta = "restarting…"; }
    else { name = "idle"; meta = shown.last_structure ? `last: ${short(shown.last_structure, 16)}` : ""; }
    d.innerHTML = `<div class="sid"><span>${w.worker_id}</span><span>${st === "running" ? "RUN" : st.toUpperCase()}</span></div>
      <div class="sname" title="${name || ""}">${short(name, 24)}</div>
      <div class="smeta">${meta}</div>
      <div class="sbar"><div style="width:${pct}%"></div></div>`;
    pool.appendChild(d);
  }
}

// ---------------------------------------------------------------------------- events
const VERBS = {
  run_start: ["start", "live"], run_end: ["finish", "good"], run_resume: ["resume", "good"],
  round_start: ["round", ""], round_end: ["round", ""], pool_start: ["pool", ""],
  worker_start: ["up", ""], task_start: ["run", ""], task_done: ["done", ""],
  timeout: ["TIMEOUT", "timeout"], task_error: ["error", "bad"], budget_refused: ["skip", ""],
  worker_dead: ["DEAD", "bad"], worker_restart: ["restart", "good"], decision: ["decide", "decision"],
  veto: ["veto", "veto"], agent_start: ["start", ""], agent_finish: ["finish", ""],
  agent_fail: ["fail", "bad"], agent_restart: ["restart", "good"], agent_tool: ["tool", ""],
  llm_usage: ["tokens", ""], promote: ["promote", "decision"], intervention: ["HUMAN", "bad"],
};
const FILTERS = {
  all: () => true,
  timeouts: (e) => e.type === "timeout" || e.type === "worker_dead",
  vetoes: (e) => e.type === "veto",
  decisions: (e) => ["decision", "veto", "promote", "round_end"].includes(e.type),
};

function describe(e) {
  const p = e.p || {};
  switch (e.type) {
    case "task_start": return `${short(p.structure, 26)} T${p.tier} ${p.mode} est ${Math.round(p.est_cpu_s)}s${p.build_grid ? " +grid" : ""}`;
    case "task_done": return `${short(p.structure, 26)} T${p.tier} ${f3(p.uptake)} ±${f3(p.err)} · ${Math.round(p.cpu_s)}s`;
    case "timeout": return `${short(p.structure, 26)} exceeded ${Math.round(p.limit_cpu_s)}s CPU in ${p.phase}, killed`;
    case "task_error": return `${short(p.structure, 26)} ${short(p.error || "", 40)}`;
    case "budget_refused": return `${short(p.structure, 26)} T${p.tier}: does not fit remaining budget`;
    case "round_start": return `#${p.round} ${p.phase || `tier ${p.tier ?? ""}`} · ${p.n ?? ""} jobs`;
    case "round_end": return `#${p.round} committed · ${f2((p.spent_cpu_s || 0) / 3600)} CPU-h${p.best ? ` · best ${f3(p.best.uptake)} T${p.best.tier}` : ""}`;
    case "run_start": return `${p.arm} · budget ${f2(p.budget_cpu_s / 3600)} CPU-h`;
    case "run_end": return `${p.status} · ${p.n_structures ?? ""} structures`;
    case "run_resume": return `${p.lost_evaluations} in-flight lost (${f2(p.lost_cpu_s)} CPU-s charged), ${p.requeued_tasks} re-queued`;
    case "worker_dead": return `exit ${p.exitcode}; ${p.requeued_tasks} task re-queued`;
    case "worker_restart": return `replaces ${p.replaces}`;
    case "pool_start": return `${p.workers} workers`;
    case "worker_start": return `pid ${p.pid}`;
    case "decision": case "veto": case "promote": return p.text || p.what || JSON.stringify(p).slice(0, 80);
    default: return p.text || p.summary || "";
  }
}

function addEvents(evs) {
  for (const e of evs) {
    state.events.push(e);
    if (e.type === "agent_start" || e.type === "agent_finish" || e.type === "agent_tool") {
      const role = (e.p && e.p.role) || "";
      flashEdge(role);
    }
  }
  if (state.events.length > 1200) state.events = state.events.slice(-800);
  renderEvents(false, evs);
}

function line(e) {
  const [verb, kind] = VERBS[e.type] || [e.type, ""];
  const li = document.createElement("li");
  if (kind) li.className = `k-${kind}`;
  li.innerHTML = `<span class="t">${clock(e.ts)}</span><span class="who"></span><span class="verb"></span><span class="msg"></span>`;
  li.children[1].textContent = short(e.agent || "system", 12);
  li.children[2].textContent = verb;
  li.children[3].textContent = describe(e);
  li.title = li.children[3].textContent;
  return li;
}

function renderEvents(full, fresh) {
  const ol = $("events");
  const keep = FILTERS[state.filter];
  if (full) {
    ol.innerHTML = "";
    for (const e of state.events.filter(keep).slice(-300).reverse()) ol.appendChild(line(e));
  } else {
    for (const e of fresh.filter(keep)) ol.insertBefore(line(e), ol.firstChild);
    while (ol.children.length > 300) ol.removeChild(ol.lastChild);
  }
  $("events-empty").hidden = ol.children.length > 0;
}

// ---------------------------------------------------------------------------- curve
function renderCurve(f) {
  const svg = $("curve");
  const W = svg.clientWidth || 1000, H = svg.clientHeight || 140;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  const m = { l: 56, r: 150, t: 8, b: 22 };
  const curves = f.curves || {};
  const all = ARMS.flatMap((a) => (curves[a.key] ? curves[a.key].points : []));
  const budgetH = f.run ? f.run.budget_cpu_s / 3600 : 4;
  const x1 = Math.max(budgetH, ...all.map((p) => p[0]), 0.01);
  let y1 = Math.max(0.1, ...all.map((p) => p[1]));
  if (f.reference) y1 = Math.max(y1, f.reference.value);
  y1 = niceCeil(y1 * 1.08);
  const X = (x) => m.l + (x / x1) * (W - m.l - m.r), Y = (y) => H - m.b - (y / y1) * (H - m.t - m.b);
  const ax = el("g", { class: "axis" }, svg);
  const yts = ticks(0, y1, 4), xts = ticks(0, x1, 8);
  for (const yt of yts) {
    el("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(yt), y2: Y(yt) }, svg);
    el("text", { x: m.l - 8, y: Y(yt) + 4, "text-anchor": "end" }, ax).textContent = yt.toFixed(decimals(yts));
  }
  for (const xt of xts) {
    el("text", { x: X(xt), y: H - 6, "text-anchor": "middle" }, ax).textContent = `${xt.toFixed(decimals(xts))}h`;
  }
  el("line", { class: "budgetline", x1: X(budgetH), x2: X(budgetH), y1: m.t, y2: H - m.b }, svg);
  const legend = $("legend");
  legend.innerHTML = "";
  const present = ARMS.filter((a) => curves[a.key] && curves[a.key].points.length);
  if (!present.length) {
    el("text", { x: W / 2 - m.r / 2, y: H / 2, "text-anchor": "middle", class: "empty-msg" }, svg).textContent = "No GCMC results yet.";
  }
  const ends = [];
  for (const a of present) {
    const pts = curves[a.key].points;
    let d = `M${X(pts[0][0])},${Y(pts[0][1])}`;
    for (let i = 1; i < pts.length; i++) d += `H${X(pts[i][0])}V${Y(pts[i][1])}`;
    el("path", { d, class: `line ${a.cls}`, "stroke-dasharray": a.dash }, svg);
    ends.push({ a, x: X(pts.at(-1)[0]), y: Y(pts.at(-1)[1]), v: pts.at(-1)[1] });
    const s = document.createElement("span");
    s.innerHTML = `<svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="${a.cls === "sieve" ? "var(--accent)" : "var(--idle)"}" stroke-width="${a.cls === "sieve" ? 2 : 1.5}" stroke-dasharray="${a.dash}"/></svg>${a.label}`;
    legend.appendChild(s);
  }
  // direct end labels only if they do not collide; otherwise the legend carries identity
  ends.sort((p, q) => p.y - q.y);
  const collide = ends.some((e, i) => i && e.y - ends[i - 1].y < 13);
  if (!collide) for (const e of ends) {
    el("text", { x: W - m.r + 8, y: e.y + 4, class: "endlabel" }, svg).textContent = `${e.a.label} ${e.v.toFixed(3)}`;
    el("line", { x1: e.x, x2: W - m.r + 4, y1: e.y, y2: e.y, stroke: "var(--border)", "stroke-width": 1 }, svg);
  }
  if (f.reference) {
    el("line", { class: "ref", x1: m.l, x2: W - m.r, y1: Y(f.reference.value), y2: Y(f.reference.value) }, svg);
    el("text", { class: "reftext", x: m.l + 6, y: Y(f.reference.value) - 4 }, svg).textContent = `held-out best ${f.reference.value.toFixed(3)} (${f.reference.source})`;
  }
  // hover crosshair + tooltip
  const cross = el("line", { class: "cross", y1: m.t, y2: H - m.b, visibility: "hidden" }, svg);
  const hit = el("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
  const tip = $("tip");
  hit.onmousemove = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ev.clientX - r.left, xv = ((px - m.l) / (W - m.l - m.r)) * x1;
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    const rows = present.map((a) => {
      const pts = curves[a.key].points.filter((p) => p[0] <= xv);
      return `${a.label.padEnd(18)} ${pts.length ? pts.at(-1)[1].toFixed(3) : "  –  "}`;
    });
    tip.textContent = `${xv.toFixed(2)} CPU-h\n${rows.join("\n")}`;
    tip.hidden = false;
    tip.style.left = `${Math.min(px + 12, r.width - 220)}px`;
    tip.style.top = `40px`;
  };
  hit.onmouseleave = () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; };
}

function decimals(ts) { const st = ts.length > 1 ? ts[1] - ts[0] : 1; return Math.max(0, Math.min(3, -Math.floor(Math.log10(st) + 1e-9))); }
function niceCeil(v) { const p = Math.pow(10, Math.floor(Math.log10(v))); return Math.ceil(v / p * 2) / 2 * p; }
function ticks(a, b, n) {
  const step = niceStep((b - a) / n), out = [];
  for (let v = Math.ceil(a / step) * step; v <= b + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p; return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p; }

boot();
