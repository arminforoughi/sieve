"use strict";
// SIEVE control room. Server pushes `frame` (state as of T) and `events` over SSE.

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const ACCENT = "#0B8A98";
const ARMS = [
  { key: "random", label: "random", end: "random", dash: "1.5 4", cls: "muted" },
  { key: "descriptor", label: "descriptor", end: "descriptor", dash: "7 4", cls: "muted" },
  { key: "uniform", label: "uniform tier-2", end: "uniform", dash: "8 3 1.5 3", cls: "muted" },
  { key: "sieve", label: "SIEVE", end: "SIEVE", dash: null, cls: "sieve" },
];
const KILL_HOLD_MS = +(params.get("hold") || 400);  // a killed job stays red this long, then the next slides in
const BEST_DWELL_MS = 2500;    // the 3D view switches structure at most this often (replay is 20x)
const TRAIL_MIN_GAP_MS = 1500; // and the comet trail restarts at most this often
const TRAIL_LIFE_MS = 4500;

const S = { run: null, speed: 1, frame: null, frameAt: 0, filter: "all", events: [] };

// ------------------------------------------------------------------------------- formatting
const pad = (n) => String(n).padStart(2, "0");
const hms = (s) => { s = Math.max(0, Math.floor(s)); return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s % 3600 / 60))}:${pad(s % 60)}`; };
const clock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const n0 = (x) => (x ?? 0).toLocaleString("en-US");
const f2 = (x) => (x ?? 0).toFixed(2);
const f3 = (x) => (x == null ? "–" : Number(x).toFixed(3));
const secs = (x) => (x == null ? "?" : `${Math.round(x)}s`);
const cpuh = (x) => `${((x || 0) / 3600).toFixed(2)} CPU-h`;
const short = (s, n = 24) => (!s ? "" : s.length > n ? s.slice(0, n - 1) + "…" : s);
const name = (s) => (s || "").replace(/_clean$/, "");

// --------------------------------------------------------------- event -> sentence, one place
const SENTENCES = {
  run_start: (p) => `run started — ${p.arm}, budget ${cpuh(p.budget_cpu_s)}`,
  run_end: (p) => `finished — ${n0(p.n_structures)} structures measured`,
  run_resume: (p) => `resumed after a crash — ${p.lost_evaluations} in-flight lost (${f2(p.lost_cpu_s)} CPU-s charged), ${p.requeued_tasks} re-queued`,
  pool_start: (p) => `${p.workers} worker${p.workers === 1 ? "" : "s"} up${p.inline ? " (inline replay)" : ""}`,
  round_start: (p) => p.phase === "tier0" ? `round ${p.round} — tier 0 on ${n0(p.n)} structures`
    : `round ${p.round} — ${n0(p.n)} job${p.n === 1 ? "" : "s"}${p.tier != null ? `, tier ${p.tier}` : ""}${p.split ? `, ${p.split}` : ""}`,
  round_end: (p) => `round ${p.round} committed — ${cpuh(p.spent_cpu_s)} spent${p.best ? `, best ${name(p.best.structure)} ${f3(p.best.uptake)} (T${p.best.tier})` : ""}`,
  decision: (p) => p.text || (p.what === "stop screening" ? `budget exhausted at ${cpuh(p.spent_cpu_s)} — stop screening`
    : p.what === "descriptor ranking" ? `ranked ${n0(p.n)} by descriptor score — top: ${name((p.top5 || [])[0])}` : p.what),
  veto: (p) => p.text || `veto — ${name(p.structure)} ${p.reason || ""}`,
  promote: (p) => p.text || `promoting ${n0(p.n)} candidate${p.n === 1 ? "" : "s"} to tier ${p.tier}`,
  recommend: (p) => `recommends ${name(p.structure)} — ${f3(p.uptake)} mol/kg, tier ${p.tier}`,
  worker_start: (p) => `started, pid ${p.pid}`,
  worker_dead: (p) => `died (exit ${p.exitcode}) — ${p.requeued_tasks} task re-queued`,
  worker_restart: (p) => `restarted, replacing ${p.replaces}`,
  task_start: (p) => `simulating ${name(p.structure)} — tier ${p.tier}, ${p.mode}${p.build_grid ? " + grid" : ""}, est ${secs(p.est_cpu_s)}`,
  task_done: (p) => `${name(p.structure)} — ${f3(p.uptake)} ± ${f3(p.err)} mol/kg (T${p.tier}, ${secs(p.cpu_s)})`,
  timeout: (p) => `TIMEOUT ${name(p.structure)} at ${secs(p.limit_cpu_s)} CPU, killed`,
  task_error: (p) => `error on ${name(p.structure)}: ${short(p.error || "", 48)}`,
  budget_refused: (p) => `skipped ${name(p.structure)} — does not fit the remaining budget`,
  agent_start: (p) => p.text || `started${p.task ? ` — ${p.task}` : ""}`,
  agent_tool: (p) => p.text || `${p.tool}(${short(JSON.stringify(p.args || {}), 40)})`,
  agent_finish: (p) => p.text || p.summary || "finished",
  agent_fail: (p) => `failed — ${short(p.error || "", 60)}${p.retry ? `, retry ${p.retry}` : ""}`,
  agent_restart: (p) => p.text || "restarted",
  intervention: (p) => `HUMAN: ${p.text || "manual action"}`,
};
function sentence(e) {
  const f = SENTENCES[e.type];
  try { const t = f && f(e.p || {}); if (t) return t; } catch (_) { /* fall through */ }
  return e.type.replace(/_/g, " ");
}
const VERBS = {
  run_start: ["start", "live"], run_end: ["finish", "good"], run_resume: ["resume", "good"],
  round_start: ["round", ""], round_end: ["commit", ""], pool_start: ["pool", ""], worker_start: ["up", ""],
  task_start: ["run", ""], task_done: ["done", ""], timeout: ["TIMEOUT", "timeout"], task_error: ["error", "bad"],
  budget_refused: ["skip", ""], worker_dead: ["DEAD", "bad"], worker_restart: ["restart", "good"],
  decision: ["decide", "decision"], veto: ["veto", "veto"], promote: ["promote", "decision"], recommend: ["pick", "decision"],
  agent_start: ["start", ""], agent_finish: ["finish", ""], agent_fail: ["fail", "bad"], agent_restart: ["restart", "good"],
  agent_tool: ["tool", ""], intervention: ["HUMAN", "bad"],
};
const FILTERS = {
  all: (e) => e.type !== "task_start" && e.type !== "worker_start",  // starts are visible in the worker strip
  timeouts: (e) => e.type === "timeout" || e.type === "worker_dead",
  vetoes: (e) => e.type === "veto",
  decisions: (e) => ["decision", "veto", "promote", "recommend", "round_end"].includes(e.type),
};
const who = (e) => { const a = e.agent || "orchestrator"; return a.endsWith("-driver") ? "orchestrator" : a; };

// --------------------------------------------------------------------------------------- boot
async function boot() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  S.speed = cfg.speed || 1;
  $("replay").hidden = !cfg.replay;
  const sel = $("run-select");
  const want = params.get("run") || cfg.default_run;
  if (!cfg.runs.length) sel.appendChild(new Option("no runs yet", ""));
  for (const r of cfg.runs) sel.appendChild(new Option(`${r.run_id} · ${r.arm}`, r.run_id, false, r.run_id === want));
  sel.onchange = () => { params.set("run", sel.value); location.search = params.toString(); };
  S.run = want || null;
  $("nav-agents").href = `/agents?run=${encodeURIComponent(S.run || "")}`;
  $("nav-disc").href = `/discoveries?run=${encodeURIComponent(S.run || "")}`;
  render({ run: null, curves: {} });
  document.querySelectorAll(".filters button").forEach((b) => (b.onclick = () => {
    document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("on", x === b));
    S.filter = b.dataset.f; renderEvents(true);
  }));
  mol.init();
  pool.init();
  fetch("/api/pool").then((r) => r.json()).then((d) => pool.setPoints(d)).catch(() => {});
  if (params.get("snapshot")) {  // one-shot render over plain HTTP: static exports, headless screenshots
    const q = encodeURIComponent(S.run || "");
    S.frame = await fetch(`/api/frame?run=${q}`).then((r) => r.json()); S.frameAt = performance.now();
    if (S.run) addEvents(await fetch(`/api/events?run=${q}`).then((r) => r.json()));
    render(S.frame);
  } else {
    const es = new EventSource(`/api/stream?run=${encodeURIComponent(S.run || "")}`);
    es.addEventListener("frame", (m) => { S.frame = JSON.parse(m.data); S.frameAt = performance.now(); render(S.frame); });
    es.addEventListener("events", (m) => addEvents(JSON.parse(m.data)));
  }
  setInterval(tick, 250);
  window.addEventListener("resize", () => { if (S.frame) chart.draw(S.frame); pool.resize(); });
  requestAnimationFrame(loop);
}

function loop(now) {
  mol.draw(now);
  pool.draw(now);
  requestAnimationFrame(loop);
}

function tick() {
  const f = S.frame;
  if (!f || !f.run) return;
  const dt = f.run.finished ? 0 : ((performance.now() - S.frameAt) / 1000) * S.speed;
  $("s-elapsed").textContent = hms(f.run.elapsed_s + dt);
}

function render(f) {
  renderHero(f);
  workers.render(f);
  renderAgents(f);
  chart.draw(f);
  pool.update(f);
  mol.update(f);
  const st = $("stale");
  if (f.run && f.stale_s != null && f.stale_s > 180 && S.speed === 1) {
    st.hidden = false;
    st.textContent = `No activity for ${Math.round(f.stale_s / 60)} min — the run process may have died. Nothing is lost: python -m sieve resume ${f.run.run_id}`;
  } else st.hidden = true;
}

// --------------------------------------------------------------------------------------- hero
const POLICY = {
  random: "Random arm — a fixed policy: tier 1 on structures in random order until the budget runs out.",
  descriptor: "Descriptor arm — a fixed policy: rank every structure by pore geometry, then tier 1 from the top down.",
  uniform: "Uniform arm — a fixed policy: tier 2 (converged) on structures in random order until the budget runs out.",
  sieve: "SIEVE — agents choose each round which structures to screen, promote to tier 2, or veto.",
};
function renderHero(f) {
  const v = f.vitals, r = f.run;
  $("headline").textContent = r ? `${POLICY[r.arm] || r.arm}  Budget ${f2(r.budget_cpu_s / 3600)} CPU-h; ranking CO₂ uptake, not predicting it.` : "No run selected.";
  if (!r || !v) return;
  $("h-int").textContent = v.interventions;
  $("s-spent").textContent = f2(v.spent_cpu_s / 3600);
  $("s-budget").textContent = f2(r.budget_cpu_s / 3600);
  const pct = Math.min(100, (100 * v.spent_cpu_s) / r.budget_cpu_s);
  $("s-bar").style.width = `${pct}%`;
  $("s-inflight").style.left = `${pct}%`;
  $("s-inflight").style.width = `${Math.min(100 - pct, (100 * v.inflight_cpu_s) / r.budget_cpu_s)}%`;
  $("s-counts").textContent = `${n0(v.counts.t0)} / ${n0(v.counts.t1)} / ${n0(v.counts.t2)}`;
  $("s-timeouts").textContent = n0(v.timeouts + (v.worker_restarts || 0));
  const b = v.best_t2 ? { ...v.best_t2, tier: 2 } : v.best;
  $("s-best").textContent = b ? f3(b.uptake_molkg) : "–";
  const chip = $("s-best-chip");
  chip.textContent = !b ? "" : b.tier === 2 ? "TIER-2 CONFIRMED" : "TIER 1 · ±18%";
  chip.className = `chip${b && b.tier === 2 ? " ok" : ""}`;
  $("s-best-sub").textContent = b ? `${name(b.structure)} · ±${f3(b.uptake_err)} reported` : " ";
}

// ------------------------------------------------------------------------------------- agents
const agentSeen = {};
function renderAgents(f) {
  const ol = $("agents");
  const rows = f.agent_rows || [];
  if (!rows.length) { ol.innerHTML = `<li class="empty">${f.run ? "No agent activity yet." : "Select a run."}</li>`; return; }
  ol.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    const text = sentence({ type: r.type, p: r.p });
    if (agentSeen[r.agent] !== r.ts) { li.classList.add("fresh"); agentSeen[r.agent] = r.ts; }
    li.innerHTML = `<span class="sdot ${r.state}"></span><span class="id"></span><span class="line"></span>`;
    li.children[1].textContent = r.agent;
    li.children[1].title = r.model ? `${r.role} · ${r.model}` : r.role || r.agent;
    li.children[2].textContent = text;
    li.children[2].title = text;
    ol.appendChild(li);
  }
}

// ------------------------------------------------------------------------------------ workers
const workers = {
  slots: {}, latest: {},  // slot -> {el, job, shown: {task, kind}, holdUntil, lastKilled}
  render(f) {
    const box = $("slots");
    if (f.verdict) { renderVerdict(f); this.slots = {}; return; }
    $("w-title").textContent = "Worker pool";
    const ws = f.workers || [];
    $("w-note").textContent = ws.length ? `· ${ws.length} slot${ws.length === 1 ? "" : "s"}, one per core` : "";
    if (!ws.length) { box.innerHTML = `<div class="empty">${f.run ? "No workers yet." : "Select a run."}</div>`; this.slots = {}; return; }
    if (box.querySelector(".empty")) box.innerHTML = "";
    this.latest = Object.fromEntries(ws.map((w) => [w.slot, w]));
    const now = performance.now();
    for (const w of ws) {
      let s = this.slots[w.slot];
      if (!s) {
        const el = document.createElement("div"); el.className = "slot"; box.appendChild(el);
        s = this.slots[w.slot] = { el, job: null, shown: null, holdUntil: 0, lastKilled: null };
      }
      const want = this.target(w);
      // A job that ended in a timeout is shown killed (red) for KILL_HOLD_MS, then the next job
      // slides in -- even when the kill and the next start arrive in the same one-second frame.
      let killed = null;
      if (w.state === "timeout") killed = { task: w.task, kind: "killed", w };
      else if (w.prev && w.prev.outcome === "timeout" && s.shown && s.shown.task !== want.task)
        killed = { task: w.prev.task, kind: "killed", w: { ...w, structure: w.prev.structure, limit_cpu_s: w.prev.limit_cpu_s } };
      if (killed && s.lastKilled !== killed.task) {
        s.lastKilled = killed.task;
        this.swap(s, killed, false);
        s.holdUntil = now + KILL_HOLD_MS;
        setTimeout(() => {
          const cur = this.target(this.latest[w.slot] || w);
          if (cur.kind !== "killed") this.swap(s, cur, true);
        }, KILL_HOLD_MS);
        continue;
      }
      if (now < s.holdUntil) continue;
      if (!s.shown || s.shown.task !== want.task || s.shown.kind !== want.kind) this.swap(s, want, !!s.shown && want.kind === "running");
      else this.fill(s.job, want);
    }
  },
  target(w) {
    if (w.state === "running" && w.stalled_s) return { task: `${w.task}-stalled`, kind: "stalled", w };
    if (w.state === "running") return { task: w.task, kind: "running", w };
    if (w.state === "dead") return { task: `dead-${w.ended}`, kind: "dead", w };
    if (w.state === "timeout") return { task: w.task, kind: "killed", w };
    return { task: `idle-${w.worker_id}`, kind: "idle", w };
  },
  swap(s, want, slide) {
    const old = s.job;
    const job = document.createElement("div");
    job.className = `job ${want.kind}${slide ? " enter" : ""}`;
    job.innerHTML = `<div class="sid"><span></span><span></span></div><div class="sname"></div><div class="smeta"></div><div class="sbar"><i></i></div>`;
    s.el.appendChild(job);
    this.fill(job, want);
    s.job = job; s.shown = { task: want.task, kind: want.kind };
    if (slide) {
      requestAnimationFrame(() => requestAnimationFrame(() => job.classList.remove("enter")));
      if (old) { old.classList.add("leave"); setTimeout(() => old.remove(), 320); }
    } else if (old) old.remove();
  },
  fill(job, want) {
    const w = want.w;
    const [idEl, stEl] = job.querySelector(".sid").children;
    idEl.textContent = w.worker_id;
    const nm = job.querySelector(".sname"), meta = job.querySelector(".smeta"), bar = job.querySelector(".sbar i");
    if (want.kind === "running") {
      const lim = w.limit_cpu_s || (w.est_cpu_s ? 4 * w.est_cpu_s : null), cpu = w.cpu_now || 0;
      stEl.textContent = `TIER ${w.tier}`;
      nm.textContent = name(w.structure); nm.title = w.structure;
      meta.textContent = `${w.mode} · ${Math.round(cpu)} / ${lim ? Math.round(lim) : "?"}s CPU`;
      bar.style.width = `${lim ? Math.min(100, (100 * cpu) / lim) : 0}%`;
    } else if (want.kind === "killed") {
      stEl.textContent = "TIMEOUT";
      nm.textContent = name(w.structure || w.last_structure);
      meta.textContent = `killed at ${secs(w.limit_cpu_s)} CPU`;
    } else if (want.kind === "stalled") {
      stEl.textContent = "NO HEARTBEAT";
      nm.textContent = name(w.structure);
      meta.textContent = `silent ${Math.round(w.stalled_s / 60) || "<1"} min · in-flight, re-queued on resume`;
      bar.style.width = "0%";
    } else if (want.kind === "dead") {
      stEl.textContent = "DEAD"; nm.textContent = "worker died"; meta.textContent = "restarting…";
    } else {
      stEl.textContent = "IDLE"; nm.textContent = "idle";
      meta.textContent = w.last_structure ? `last ${name(w.last_structure)}` : "";
    }
  },
};

// ------------------------------------------------------------------------------------ verdict
// At run end the worker strip becomes the verdict: what each arm would name as its winner, what
// it claimed, and what that structure really is once measured independently.
const ARM_LABEL = Object.fromEntries(ARMS.map((a) => [a.key, a.label]));
function renderVerdict(f) {
  const v = f.verdict, box = $("slots");
  $("w-title").textContent = "Verdict";
  $("w-note").textContent = v.source ? `· true values: ${v.source}` : "· no independent re-measurement yet";
  const rows = v.rows.map((r) => {
    const rank = r.true_rank, inflated = r.claimed != null && r.true != null ? r.claimed - r.true : null;
    const call = rank == null ? "not re-measured" : rank === 1 ? "true best" : rank <= 3 ? `true top 3 (#${rank})` : rank <= 10 ? `true top 10 (#${rank})` : `#${rank}`;
    const cls = rank === 1 ? "good" : rank != null && rank <= 3 ? "ok" : "";
    const sd = v.seeds && v.seeds[r.arm];
    return `<tr class="${r.arm === f.run.arm ? "me" : ""}">
      <td class="arm">${ARM_LABEL[r.arm] || r.arm}</td>
      <td class="mono">${name(r.structure)}</td>
      <td class="mono num">${f3(r.claimed)} <span class="dim">T${r.tier ?? "?"}</span></td>
      <td class="mono num">${r.true == null ? "–" : f3(r.true)}</td>
      <td class="mono num ${inflated > 0.05 ? "bad" : ""}">${inflated == null ? "" : (inflated >= 0 ? "+" : "") + inflated.toFixed(2)}</td>
      <td class="call ${cls}">${call}</td>
      <td class="mono num dim">${sd ? `${Math.round(100 * sd.winner_top10)}% · ${Math.round(100 * sd.reliability_found)}%` : ""}</td></tr>`;
  }).join("");
  box.innerHTML = `<table class="verdict">
    <thead><tr><th>arm</th><th>recommended winner</th><th class="num">arm said</th><th class="num">true</th><th class="num">over-claim</th><th>verdict</th>
    <th class="num">${v.seeds ? "20 seeds: winner in top 10 · found best" : ""}</th></tr></thead>
    <tbody>${rows}</tbody></table>
    ${v.best ? `<div class="verdict-foot">True best among ${v.n_truth} measured: <span class="mono">${name(v.best.structure)} ${f3(v.best.value)} mol/kg</span></div>` : ""}`;
}

// -------------------------------------------------------------------------------- 3D structure
// 3Dmol.js renders the actual framework from the same P1 CIF the simulation used: ball-and-stick,
// Jmol element colours, unit-cell box, pore void as faint accent spheres. Two viewers cross-fade.
const JMOL = { H: "#FFFFFF", C: "#909090", N: "#3050F8", O: "#FF0D0D", F: "#90E050", S: "#FFFF30", Cl: "#1FF01F",
  Br: "#A62929", P: "#FF8000", Zn: "#7D80B0", Cu: "#C88033", Co: "#F090A0", Ni: "#50D050", Fe: "#E06633",
  Mn: "#9C7AC7", Mg: "#8AFF00", Zr: "#94E0E0", Cd: "#FFD98F", Al: "#BFA6A6", Ag: "#C0C0C0" };
const mol = {
  views: [], active: 0, curName: null, want: null, shownAt: -1e9, loading: false,
  init() {
    if (!window.$3Dmol) { $("m-empty").textContent = "3D viewer unavailable (3Dmol.js did not load)."; return; }
    try {  // no WebGL (some projector laptops, headless) must not take the rest of the page down
      for (const id of ["mv-a", "mv-b"]) this.views.push($3Dmol.createViewer($(id), { backgroundColor: "white", antialias: true }));
    } catch (e) {
      this.views = [];
      $("m-empty").textContent = "3D view needs WebGL, which this browser does not provide.";
    }
  },
  update(f) {
    const v = f.vitals;
    const b = v && (v.best_t2 ? { ...v.best_t2, tier: 2 } : v.best);
    $("m-empty").hidden = !!this.curName;
    $("m-empty").textContent = b ? `Loading ${name(b.structure)}…` : f.run ? "No structure measured yet." : "Select a run.";
    if (!b) return;
    this.want = b;
    if (this.curName === b.structure) this.label(b);
    this.maybeSwitch();
  },
  async maybeSwitch() {
    const b = this.want, now = performance.now();
    if (!b || !this.views.length || this.curName === b.structure) return;
    if (now - this.shownAt < BEST_DWELL_MS || this.loading) { setTimeout(() => this.maybeSwitch(), 500); return; }
    this.loading = true;
    try {
      const P = await fetch(`/api/cif?name=${encodeURIComponent(b.structure)}`).then((r) => r.json());
      if (P.error) return;
      const next = 1 - this.active, v = this.views[next];
      v.clear(); v.removeAllShapes();
      const m = v.addModel(P.cif, "cif", { doAssembly: true, duplicateAssemblyAtoms: true, normalizeAssembly: true });
      if (P.reps > 1 && v.replicateUnitCell) v.replicateUnitCell(P.reps, P.reps, P.reps, m, true);
      v.setStyle({}, { stick: { radius: 0.14, colorscheme: "Jmol" }, sphere: { scale: 0.24, colorscheme: "Jmol" } });
      v.setStyle({ elem: "H" }, { stick: { radius: 0.08, colorscheme: "Jmol" }, sphere: { scale: 0.12, colorscheme: "Jmol" } });
      v.addUnitCell(m, { box: { color: "#9097A3" }, alabel: "", blabel: "", clabel: "" });
      const L = P.lattice, R = P.reps;
      for (let a = 0; a < R; a++) for (let bb = 0; bb < R; bb++) for (let c = 0; c < R; c++)
        for (const p of P.void) v.addSphere({ center: { x: p[0] + a * L[0][0] + bb * L[1][0] + c * L[2][0],
          y: p[1] + a * L[0][1] + bb * L[1][1] + c * L[2][1], z: p[2] + a * L[0][2] + bb * L[1][2] + c * L[2][2] },
          radius: 0.9, color: ACCENT, opacity: 0.12 });
      v.resize(); v.zoomTo(); v.zoom(1.05); v.render(); if (!params.get("snapshot")) v.spin("y", 0.4);
      $(next ? "mv-b" : "mv-a").classList.add("on");
      $(next ? "mv-a" : "mv-b").classList.remove("on");
      this.views[this.active].spin(false);
      this.active = next; this.curName = P.name; this.shownAt = performance.now(); this.cell = P;
      $("m-empty").hidden = true;
      $("m-legend").innerHTML = P.elements.filter((e) => JMOL[e]).map((e) => `<span class="el" style="background:${JMOL[e]};border:1px solid #C9CED6"></span>${e}`).join("");
      this.label(this.want && this.want.structure === P.name ? this.want : b);
    } catch (_) { /* retry on the next frame */ } finally { this.loading = false; }
  },
  label(b) {
    $("m-name").textContent = name(b.structure);
    $("m-meta").textContent = `${f3(b.uptake_molkg)} ± ${f3(b.uptake_err)} mol/kg · ${b.tier === 2 ? "tier-2 confirmed" : "tier 1, not converged (±18%)"}`
      + (this.cell ? ` · ${this.cell.n_cell} atoms/cell${this.cell.reps > 1 ? `, ${this.cell.reps}×${this.cell.reps}×${this.cell.reps} cells shown` : ""}` : "");
  },
  draw() {},
};

// ------------------------------------------------------------------------------- pool scatter
const pool = {
  pts: [], byName: {}, measured: {}, firstSeen: {}, best: null, trail: null, trailAt: -1e9, dirty: true, primed: false,
  init() {
    this.cv = $("p-canvas"); this.ctx = this.cv.getContext("2d");
    const plot = $("p-plot"), tip = $("p-tip");
    plot.addEventListener("mousemove", (e) => {
      if (!this.qt) return;
      const r = plot.getBoundingClientRect();
      const hit = this.qt.find(e.clientX - r.left, e.clientY - r.top, 10);
      if (!hit) { tip.hidden = true; return; }
      const m = this.measured[hit.name];
      tip.textContent = `${hit.name}\nLCD ${hit.lcd.toFixed(2)} Å · void ${hit.vf.toFixed(3)}\n${m ? `${f3(m[0])} ± ${f3(m[1])} mol/kg (T${m[2]})` : "not measured"}`;
      tip.hidden = false;
      tip.style.left = `${Math.min(e.clientX - r.left + 12, r.width - 250)}px`;
      tip.style.top = `${Math.max(4, e.clientY - r.top - 64)}px`;
    });
    plot.addEventListener("mouseleave", () => (tip.hidden = true));
  },
  setPoints(d) {
    this.pts = d.points.map(([n, lcd, vf]) => ({ name: n, lcd, vf }));
    this.byName = Object.fromEntries(this.pts.map((p) => [p.name, p]));
    $("p-count").textContent = `· ${n0(this.pts.length)} structures`;
    this.resize();
  },
  resize() {
    if (!window.d3 || !this.pts.length) return;
    const plot = $("p-plot"), W = plot.clientWidth, H = plot.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!W || !H) return;
    this.cv.width = Math.round(W * dpr); this.cv.height = Math.round(H * dpr);
    const m = { l: 48, r: 14, t: 28, b: 34 };
    this.W = W; this.H = H;
    this.x = d3.scaleLog().domain([2.5, 40]).range([m.l, W - m.r]).clamp(true);
    this.y = d3.scaleLinear().domain([0, 1]).range([H - m.b, m.t]).clamp(true);
    for (const p of this.pts) { p.px = this.x(p.lcd); p.py = this.y(p.vf); }
    this.qt = d3.quadtree().x((p) => p.px).y((p) => p.py).addAll(this.pts);
    const svg = d3.select("#p-axes").attr("viewBox", `0 0 ${W} ${H}`);
    svg.selectAll("*").remove();
    svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`)
      .call(d3.axisBottom(this.x).tickValues([3, 4, 5, 7, 10, 15, 20, 30, 40]).tickFormat(d3.format("~g")).tickSizeOuter(0));
    svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(this.y).ticks(5).tickSizeOuter(0));
    svg.append("text").attr("class", "axis-label").attr("x", W - m.r).attr("y", H - 4).attr("text-anchor", "end").text("largest cavity diameter, Å (log)");
    svg.append("text").attr("class", "axis-label").attr("x", m.l + 4).attr("y", m.t - 10).text("void fraction");
    this.dirty = true;
  },
  update(f) {
    const meas = f.measured || {};
    const now = performance.now();
    // on first load everything already measured appears settled; later arrivals animate in
    for (const s of Object.keys(meas)) if (!(s in this.firstSeen)) this.firstSeen[s] = this.primed ? now : -1e9;
    this.primed = true;
    this.measured = meas;
    const top = Math.max(1, ...Object.values(meas).map((m) => m[0]));
    this.color = window.d3 ? d3.scaleSequential(d3.interpolateRgb("#BFE5EA", "#05505A")).domain([0, top]) : null;
    this.renderLegend(top);
    const v = f.vitals;
    const b = v && (v.best_t2 || v.best);
    this.best = b ? b.structure : null;
    const lr = f.last_round;
    if (lr && lr.picks && lr.picks.length && (!this.trail || this.trail.round !== lr.round) && now - this.trailAt > TRAIL_MIN_GAP_MS) {
      this.trail = { round: lr.round, picks: lr.picks.filter((p) => this.byName[p]) };
      this.trailAt = now;
    }
    this.dirty = true;
  },
  renderLegend(top) {
    if (this.legendTop === top) return;
    this.legendTop = top;
    $("p-legend").innerHTML = `<span><span class="dot"></span>not measured</span><span><span class="dot" style="background:#fff;box-shadow:inset 0 0 0 1.5px #15171C"></span>tier-2 confirmed</span><span>0</span><span class="ramp" style="background:linear-gradient(90deg,#BFE5EA,#05505A)"></span><span>${top.toFixed(1)} mol/kg</span>`;
  },
  draw(now) {
    if (!this.x || !this.pts.length) return;
    let animating = now - this.trailAt < TRAIL_LIFE_MS;
    for (const t of Object.values(this.firstSeen)) if (now - t < 700) { animating = true; break; }
    if (!this.dirty && !animating) return;
    this.dirty = false;
    const ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.W, this.H);
    ctx.fillStyle = "#C3C8D0"; ctx.globalAlpha = 0.6;
    for (const p of this.pts) if (!this.measured[p.name]) ctx.fillRect(p.px - 1, p.py - 1, 2, 2);
    ctx.globalAlpha = 1;
    // measured structures lift (grow) and take their uptake colour; highest drawn last
    const ms = Object.entries(this.measured).filter(([n]) => this.byName[n]).sort((a, b) => a[1][0] - b[1][0]);
    for (const [n, m] of ms) {
      const p = this.byName[n], g = Math.min(1, (now - (this.firstSeen[n] ?? -1e9)) / 600);
      const r = 1.5 + 2.6 * (1 - Math.pow(1 - g, 3)) + (m[2] === 2 ? 0.8 : 0);
      ctx.fillStyle = this.color ? this.color(m[0]) : ACCENT;
      ctx.beginPath(); ctx.arc(p.px, p.py, r, 0, 6.283); ctx.fill();
      ctx.lineWidth = 1.5; ctx.strokeStyle = m[2] === 2 ? "#15171C" : "#FFFFFF"; ctx.stroke();
    }
    // the last round's picks: a comet trail, head = most recent pick, fading out
    if (this.trail && this.trail.picks.length) {
      const life = 1 - (now - this.trailAt) / TRAIL_LIFE_MS;
      if (life > 0) {
        const pts = this.trail.picks.map((n) => this.byName[n]);
        ctx.strokeStyle = ACCENT;
        for (let i = 1; i < pts.length; i++) {
          ctx.globalAlpha = life * (0.15 + 0.6 * (i / pts.length)); ctx.lineWidth = 1 + 1.5 * (i / pts.length);
          ctx.beginPath(); ctx.moveTo(pts[i - 1].px, pts[i - 1].py); ctx.lineTo(pts[i].px, pts[i].py); ctx.stroke();
        }
        for (let i = 0; i < pts.length; i++) {
          ctx.globalAlpha = life * (0.3 + 0.7 * ((i + 1) / pts.length)); ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(pts[i].px, pts[i].py, 5.5, 0, 6.283); ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }
    }
    if (this.best && this.byName[this.best]) {
      const p = this.byName[this.best];
      ctx.strokeStyle = "#15171C"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(p.px, p.py, 8, 0, 6.283); ctx.stroke();
    }
  },
};

// ------------------------------------------------------------------------------ progress chart
const chart = {
  draw(f) {
    if (!window.d3) return;
    const box = $("c-chart"), W = box.clientWidth, H = box.clientHeight;
    if (!W || !H) return;
    const svg = d3.select("#c-svg").attr("viewBox", `0 0 ${W} ${H}`);
    svg.selectAll("*").remove();
    const m = { l: 44, r: 112, t: 14, b: 24 };
    const curves = f.curves || {};
    const present = ARMS.filter((a) => curves[a.key] && curves[a.key].points.length);
    const all = present.flatMap((a) => curves[a.key].points);
    const labelsFit = (() => { const ys = present.map((a) => curves[a.key].points.at(-1)[1]).sort((p, q) => p - q);
      const span = (d3.max(all, (p) => p[1]) || 1) * 1.08; return ys.every((v, i) => !i || (v - ys[i - 1]) / span * (H - 38) >= 13); })();
    if (!labelsFit) m.r = 20;
    const budgetH = f.run ? f.run.budget_cpu_s / 3600 : 4;
    const x1 = Math.max(budgetH, d3.max(all, (p) => p[0]) || 0);
    let y1 = Math.max(0.5, d3.max(all, (p) => p[1]) || 0);
    if (f.reference) y1 = Math.max(y1, f.reference.value);
    const x = d3.scaleLinear().domain([0, x1]).range([m.l, W - m.r]);
    const y = d3.scaleLinear().domain([0, y1 * 1.08]).nice().range([H - m.b, m.t]);
    svg.append("g").attr("class", "grid").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat(""));
    svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSizeOuter(0));
    svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(6).tickFormat((d) => `${d}h`).tickSizeOuter(0));
    svg.append("line").attr("x1", x(budgetH)).attr("x2", x(budgetH)).attr("y1", m.t).attr("y2", H - m.b).attr("stroke", "#E2E5EA");
    const legend = $("c-legend");
    legend.innerHTML = "";
    if (!present.length) svg.append("text").attr("class", "empty-msg").attr("x", (W - m.r + m.l) / 2).attr("y", H / 2).attr("text-anchor", "middle").text("No GCMC results yet.");
    const line = d3.line().x((p) => x(p[0])).y((p) => y(p[1])).curve(d3.curveStepAfter);
    const ends = [];
    for (const a of present) {
      const pts = curves[a.key].points;
      const path = svg.append("path").attr("class", `line ${a.cls}`).attr("d", line(pts));
      if (a.dash) path.attr("stroke-dasharray", a.dash);
      ends.push({ a, y: y(pts.at(-1)[1]), v: pts.at(-1)[1], x: x(pts.at(-1)[0]) });
      const s = document.createElement("span");
      s.innerHTML = `<svg width="20" height="8"><line x1="0" y1="4" x2="20" y2="4" stroke="${a.cls === "sieve" ? ACCENT : "#9097A3"}" stroke-width="${a.cls === "sieve" ? 2 : 1.5}" ${a.dash ? `stroke-dasharray="${a.dash}"` : ""}/></svg>${a.label}`;
      legend.appendChild(s);
    }
    ends.sort((p, q) => p.y - q.y);
    if (!ends.some((e, i) => i && e.y - ends[i - 1].y < 13)) {  // direct labels only when they do not collide
      for (const e of ends) {
        svg.append("line").attr("x1", e.x).attr("x2", W - m.r + 4).attr("y1", e.y).attr("y2", e.y).attr("stroke", "#E2E5EA");
        svg.append("text").attr("class", "endlabel").attr("x", W - m.r + 8).attr("y", e.y + 4).text(`${e.a.end} ${e.v.toFixed(2)}`);
      }
    }
    if (f.reference) {  // the server sends this only once the run has ended
      svg.append("line").attr("class", "ref").attr("x1", m.l).attr("x2", W - m.r).attr("y1", y(f.reference.value)).attr("y2", y(f.reference.value));
      svg.append("text").attr("class", "reftext").attr("x", m.l + 6).attr("y", y(f.reference.value) - 4).text(`held-out true best ${f.reference.value.toFixed(3)}`);
    }
    const cross = svg.append("line").attr("class", "cross").attr("y1", m.t).attr("y2", H - m.b).attr("visibility", "hidden");
    const tip = $("c-tip");
    svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", Math.max(0, W - m.l - m.r)).attr("height", Math.max(0, H - m.t - m.b))
      .attr("fill", "transparent").style("pointer-events", "all")
      .on("mousemove", (ev) => {
        const [px] = d3.pointer(ev), xv = x.invert(px);
        cross.attr("x1", px).attr("x2", px).attr("visibility", "visible");
        const rows = present.map((a) => { const p = curves[a.key].points.filter((q) => q[0] <= xv); return `${a.label.padEnd(15)} ${p.length ? p.at(-1)[1].toFixed(3) : "  –"}`; });
        tip.textContent = `${xv.toFixed(2)} CPU-h\n${rows.join("\n")}`;
        tip.hidden = false; tip.style.left = `${Math.min(px + 12, W - 210)}px`; tip.style.top = "34px";
      })
      .on("mouseleave", () => { cross.attr("visibility", "hidden"); tip.hidden = true; });
  },
};

// ------------------------------------------------------------------------------------- events
function addEvents(evs) {
  S.events.push(...evs);
  if (S.events.length > 1500) S.events = S.events.slice(-1000);
  renderEvents(false, evs);
}
function line(e) {
  const [verb, kind] = VERBS[e.type] || [e.type, ""];
  const li = document.createElement("li");
  if (kind) li.className = `k-${kind}`;
  li.innerHTML = `<span class="t"></span><span class="who"></span><span class="verb"></span><span class="msg"></span>`;
  li.children[0].textContent = clock(e.ts);
  li.children[1].textContent = short(who(e), 14);
  li.children[2].textContent = verb;
  const text = sentence(e);
  li.children[3].textContent = text; li.title = text;
  return li;
}
function renderEvents(full, fresh) {
  const ol = $("events"), keep = FILTERS[S.filter];
  if (full) { ol.innerHTML = ""; for (const e of S.events.filter(keep).slice(-300).reverse()) ol.appendChild(line(e)); }
  else { for (const e of fresh.filter(keep)) ol.insertBefore(line(e), ol.firstChild); while (ol.children.length > 300) ol.removeChild(ol.lastChild); }
  $("e-empty").hidden = ol.children.length > 0;
}

boot();
