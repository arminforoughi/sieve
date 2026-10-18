"use strict";
// SIEVE agent flow: the team as a live stage. Same SSE stream as the control room.
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const params = new URLSearchParams(location.search);
const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (parent) parent.appendChild(e); return e; };
const f2 = (x) => (x ?? 0).toFixed(2), f3 = (x) => (x == null ? "–" : Number(x).toFixed(3));
const name = (s) => (s || "").replace(/_clean$/, "");
const pad = (n) => String(n).padStart(2, "0");
const clock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

const NODES = {
  orchestrator: { x: 500, y: 80, label: "Orchestrator", model: "Nemotron Super", glyph: "M-14,0a14,14 0 1,0 28,0a14,14 0 1,0 -28,0M-5,0a5,5 0 1,0 10,0a5,5 0 1,0 -10,0M0,-14v-6M0,14v6M-14,0h-6M14,0h6" },
  scout: { x: 150, y: 200, label: "Scout", model: "Nemotron Nano", glyph: "M-12,-4a9,9 0 1,0 18,0a9,9 0 1,0 -18,0M3,3l11,11" },
  allocator: { x: 850, y: 200, label: "Allocator", model: "Nemotron Super", glyph: "M-13,-13h10v10h-10zM3,-13h10v10h-10zM-13,3h10v10h-10zM3,3h10v10h-10z" },
  critic: { x: 500, y: 290, label: "Critic", model: "Nemotron Super", glyph: "M0,-15l12,5v8c0,8 -6,13 -12,16c-6,-3 -12,-8 -12,-16v-8zM-6,0l4,5l8,-9" },
};
const WORKER_Y = 530, POOL = { x0: 40, x1: 320, y0: 395, y1: 480 };
// speech-bubble slots in stage coordinates, chosen so no two overlap each other or a node
const SLOTS = { orchestrator: { x: 565, y: 18, w: 330, h: 104 }, scout: { x: 14, y: 300, w: 290, h: 84 },
  allocator: { x: 690, y: 300, w: 300, h: 104 }, critic: { x: 360, y: 395, w: 290, h: 86 } };
const S = { sats: {}, satCount: {}, bubbles: {}, run: null, speed: 1, workers: {}, nodes: {}, edges: {}, best: -1, measured: {}, vetoed: {}, rounds: {}, curRound: null,
  capture: {}, checks: {}, flags: {}, families: null, phase: null, rec: null, lastText: {},
  queue: [], busy: false, poolPts: [], poolByName: {}, live: false, lastToast: 0 };

// ------------------------------------------------------------------------------------ stage
function buildStage() {
  const svg = $("stage");
  svg.innerHTML = "";
  const gE = el("g", {}, svg);
  for (const r of ["scout", "allocator", "critic"]) {
    const a = NODES.orchestrator, b = NODES[r];
    S.edges[r] = el("path", { class: "edge", d: `M${a.x},${a.y + 40} C${a.x},${(a.y + b.y) / 2} ${b.x},${(a.y + b.y) / 2 - 30} ${b.x},${b.y - 40}` }, gE);
  }
  S.edges.bus = el("path", { class: "edge", d: `M${NODES.allocator.x},${NODES.allocator.y + 40} C${NODES.allocator.x},${WORKER_Y - 60} 700,${WORKER_Y - 40} 600,${WORKER_Y - 26}` }, gE);
  S.edges.results = el("path", { class: "edge", d: `M500,${WORKER_Y - 26} L${NODES.critic.x},${NODES.critic.y + 42}` }, gE);
  S.edges.pool = el("path", { class: "edge", d: `M${NODES.scout.x},${NODES.scout.y + 40} L${NODES.scout.x},${POOL.y0 - 6}` }, gE);
  el("rect", { x: POOL.x0 - 8, y: POOL.y0 - 6, width: POOL.x1 - POOL.x0 + 16, height: POOL.y1 - POOL.y0 + 12, rx: 6, fill: "none", stroke: "#E2E5EA" }, svg);
  el("text", { x: POOL.x0, y: POOL.y1 + 22, class: "label-small" }, svg).textContent = "candidate pool · cavity size → , void fraction ↑";
  S.poolG = el("g", {}, svg);
  S.workerG = el("g", {}, svg);
  el("text", { x: 380, y: WORKER_Y + 52, class: "label-small" }, svg).textContent = "GCMC workers";
  for (const [k, n] of Object.entries(NODES)) {
    const g = el("g", { class: "agent-node idle", transform: `translate(${n.x},${n.y})` }, svg);
    el("circle", { class: "ring", r: 46 }, g);
    el("path", { class: "glyph", d: n.glyph, transform: "scale(1.35)" }, g);
    el("text", { class: "title", y: 70, "text-anchor": "middle" }, g).textContent = n.label;
    el("text", { class: "model", y: 86, "text-anchor": "middle" }, g).textContent = n.model;
    S.nodes[k] = g;
  }
  S.satG = el("g", {}, svg);
  S.fx = el("g", {}, svg);
  S.bubbles = {}; S.sats = {}; S.satCount = {};
  drawPool();
}

// sub-agents: small nodes orbiting their parent; they scale in, pulse while working, fade on exit
const SAT_ANGLES = { orchestrator: [200, 340], scout: [-60, 260], allocator: [-120, 80], critic: [200, 340] };
function satellite(id) {
  if (S.sats[id]) return S.sats[id];
  const parts = id.split("/"), parentId = parts.slice(0, -1).join("/"), top = parts[0];
  const parent = S.sats[parentId] || (NODES[top] ? { x: NODES[top].x, y: NODES[top].y } : { x: 500, y: 300 });
  const depth = parts.length - 1;
  const k = (S.satCount[parentId] = (S.satCount[parentId] || 0) + 1) - 1;
  const [a0, a1] = SAT_ANGLES[top] || [0, 360];
  const ang = ((a0 + ((a1 - a0) * ((k % 6) + 0.5)) / 6) * Math.PI) / 180;
  const r = depth === 1 ? 92 + 16 * Math.floor(k / 6) : 48;
  const x = parent.x + r * Math.cos(ang), y = parent.y + r * Math.sin(ang);
  const g = el("g", { class: "sat working", transform: `translate(${x},${y})` }, S.satG);
  el("line", { x1: 0, y1: 0, x2: parent.x - x, y2: parent.y - y, class: "sat-link" }, g);
  el("circle", { r: depth === 1 ? 15 : 11, class: "sat-ring" }, g);
  const t = el("text", { y: depth === 1 ? 28 : 22, "text-anchor": "middle", class: "sat-label" }, g);
  t.textContent = parts[parts.length - 1].slice(0, 14);
  return (S.sats[id] = { g, x, y });
}
function setSat(id, state) { const s = satellite(id); s.g.setAttribute("class", `sat ${state}`); }

function setNode(role, state) { const g = S.nodes[role]; if (g) g.setAttribute("class", `agent-node ${state}`); }
function flash(edge) { const e = S.edges[edge]; if (!e) return; e.classList.add("hot"); setTimeout(() => e.classList.remove("hot"), 700); }

function pt(id) {  // a point in stage coordinates
  if (NODES[id]) return { x: NODES[id].x, y: NODES[id].y };
  if (S.sats[id]) return { x: S.sats[id].x, y: S.sats[id].y };
  if (id === "workers") return { x: 500, y: WORKER_Y };
  if (S.workers[id]) return { x: S.workers[id].x, y: WORKER_Y };
  return { x: 500, y: 300 };
}

function fly(from, to, cls, dur = 650, delay = 0) {
  const a = pt(from), b = pt(to);
  const c = el("circle", { class: `msg ${cls}`, r: cls === "veto" ? 7 : 5.5, cx: a.x, cy: a.y, opacity: 0 }, S.fx);
  const t0 = performance.now() + delay;
  const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.15, my = (a.y + b.y) / 2 - Math.abs(b.x - a.x) * 0.12;
  const step = (now) => {
    const t = Math.min(1, Math.max(0, (now - t0) / dur));
    if (now >= t0) {
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2, u = 1 - e;
      c.setAttribute("cx", u * u * a.x + 2 * u * e * mx + e * e * b.x);
      c.setAttribute("cy", u * u * a.y + 2 * u * e * my + e * e * b.y);
      c.setAttribute("opacity", t < 0.9 ? 1 : (1 - t) * 10);
    }
    if (t < 1) requestAnimationFrame(step); else c.remove();
  };
  requestAnimationFrame(step);
}

function burst(x, y, color = "#0B8A98") {
  for (let i = 0; i < 3; i++) {
    const c = el("circle", { class: "burst", cx: x, cy: y, r: 4, stroke: color }, S.fx);
    const t0 = performance.now() + i * 140;
    const step = (now) => { const t = Math.min(1, Math.max(0, (now - t0) / 900)); c.setAttribute("r", 4 + 34 * t); c.setAttribute("opacity", 1 - t); if (t < 1) requestAnimationFrame(step); else c.remove(); };
    requestAnimationFrame(step);
  }
}

// speech bubbles: foreignObject in fixed slots, so they scale with the stage and never collide
const bubbleTimers = {};
function bubble(role, text, cls = "") {
  const slot = SLOTS[role]; if (!slot) return;
  let fo = S.bubbles[role];
  if (!fo) {
    fo = el("foreignObject", { x: slot.x, y: slot.y, width: slot.w, height: slot.h }, $("stage"));
    fo.innerHTML = `<div xmlns="http://www.w3.org/1999/xhtml" class="bubble"></div>`;
    S.bubbles[role] = fo;
  }
  const b = fo.firstChild;
  b.className = `bubble ${cls}`;
  b.innerHTML = `<span class="who">${role}</span><span class="say">${esc(text)}</span>`;
  b.style.opacity = 1;
  clearTimeout(bubbleTimers[role]);
  if (!params.get("snapshot")) bubbleTimers[role] = setTimeout(() => (b.style.opacity = 0.25), cls === "thinking" ? 60000 : 15000);
}

function toast(html) {
  const t = $("toast"); t.hidden = false; t.innerHTML = html;
  t.style.animation = "none"; void t.offsetWidth; t.style.animation = "";
}

// --------------------------------------------------------------------------- pool + workers
function drawPool() {
  S.poolG.innerHTML = "";
  if (!S.poolPts.length) return;
  const lx = (v) => POOL.x0 + (Math.log(Math.min(40, Math.max(2.5, v))) - Math.log(2.5)) / (Math.log(40) - Math.log(2.5)) * (POOL.x1 - POOL.x0);
  const ly = (v) => POOL.y1 - Math.min(1, Math.max(0, v)) * (POOL.y1 - POOL.y0);
  const step = Math.max(1, Math.floor(S.poolPts.length / 900));
  for (let i = 0; i < S.poolPts.length; i += step) {
    const [n, lcd, vf] = S.poolPts[i];
    el("circle", { class: "pool-dot", cx: lx(lcd), cy: ly(vf), r: 1.2, opacity: 0.6 }, S.poolG);
  }
  S.poolXY = (n) => { const p = S.poolByName[n]; return p ? { x: lx(p[1]), y: ly(p[2]) } : null; };
  for (const s of Object.keys(S.measured)) markPool(s, false);
}
function markPool(s, animate) {
  if (!S.poolXY) return;
  const p = S.poolXY(s); if (!p) return;
  el("circle", { class: "pool-dot hit", cx: p.x, cy: p.y, r: 3 }, S.poolG);
  if (animate) burst(p.x, p.y);
}

function layoutWorkers(ids) {
  const keys = ids.slice().sort();
  if (keys.join() === Object.keys(S.workers).sort().join()) return;
  S.workerG.innerHTML = ""; const old = S.workers; S.workers = {};
  const n = Math.max(1, keys.length), w = Math.min(96, 560 / n - 6), x0 = 500 - (n * (w + 6)) / 2;
  keys.forEach((id, i) => {
    const x = x0 + i * (w + 6) + w / 2;
    const g = el("g", { class: "worker-chip", transform: `translate(${x - w / 2},${WORKER_Y - 22})` }, S.workerG);
    el("rect", { width: w, height: 44, rx: 6 }, g);
    const t1 = el("text", { x: 6, y: 16 }, g); t1.textContent = id.replace("worker-", "w");
    const t2 = el("text", { x: 6, y: 32 }, g);
    S.workers[id] = { x, g, t2, state: (old[id] || {}).state };
  });
}
function setWorker(id, state, text) {
  const w = S.workers[id]; if (!w) return;
  w.g.setAttribute("class", `worker-chip ${state}`);
  w.t2.textContent = (text || "").slice(0, 12);
}

// ---------------------------------------------------------------------------- event actions
function act(e, animate) {
  const p = e.p || {}, role = p.role || e.agent;
  const sub = e.agent && e.agent.includes("/") ? e.agent : null;
  if (sub) {
    const parentId = e.parent || sub.split("/").slice(0, -1).join("/");
    if (sub.startsWith("scout/") && e.type === "agent_start") (S.subFams = S.subFams || new Set()).add(sub.split("/")[1]);
    switch (e.type) {
      case "agent_start": setSat(sub, "working"); if (animate) fly(parentId, sub, "task", 450); break;
      case "agent_finish": setSat(sub, "idle"); if (animate) fly(sub, parentId, "answer", 450); chat(e); break;
      case "agent_fail": setSat(sub, "failed"); chat(e); break;
      case "agent_restart": setSat(sub, "recovered"); chat(e); break;
      case "agent_exit": setSat(sub, "exited"); break;
      case "delegate": if (animate) bubble(sub.split("/")[0], p.text || "spawning sub-agents"); chat(e); break;
      default:
    }
    return;
  }
  switch (e.type) {
    case "delegate":
      if (animate) { bubble(role, p.text || "spawning sub-agents"); burst(NODES[role] ? NODES[role].x : 500, NODES[role] ? NODES[role].y : 300); }
      chat(e);
      break;
    case "agent_exit":
      break;
    case "agent_start":
      setNode(role, "working");
      if (animate) { if (role !== "orchestrator") { fly("orchestrator", role, "task"); flash(role); } bubble(role, `${p.text || "thinking"}…`, "thinking"); }
      break;
    case "agent_finish":
      setNode(role, "idle");
      S.lastText[role] = p.text || "";
      if (animate) { if (role !== "orchestrator") { fly(role, "orchestrator", "answer"); flash(role); } bubble(role, p.text || "done"); }
      chat(e);
      break;
    case "agent_fail":
      setNode(role, "failed");
      if (animate) bubble(role, `model call failed${p.retry ? `, retry ${p.retry} in ${p.backoff_s}s` : ""}`, "veto");
      chat(e);
      break;
    case "agent_restart":
      setNode(role, "recovered");
      if (animate) bubble(role, p.text || "recovered");
      chat(e);
      break;
    case "veto":
      if (animate) { fly("critic", "orchestrator", "veto", 800); flash("critic"); bubble("critic", p.text || `veto ${name(p.structure)}`, "veto"); }
      S.vetoed[p.structure] = p.reason; chat(e);
      break;
    case "promote":
      if (animate) { flash("bus"); (p.structures || []).forEach((s, i) => fly("allocator", "workers", "job", 700, i * 120)); }
      break;
    case "task_start":
      setWorker(e.agent, "busy", name(p.structure));
      if (animate) { flash("bus"); fly("allocator", e.agent, "job", 600); }
      break;
    case "task_done": {
      setWorker(e.agent, "", "");
      if (p.tier === 3) {  // capture check: APS, not an uptake
        S.capture[p.structure] = p;
        if (animate) { flash("results"); fly(e.agent, "critic", "result", 700); }
        break;
      }
      const isNew = !(p.structure in S.measured);
      S.measured[p.structure] = [p.uptake, p.err, p.tier];
      if (isNew) markPool(p.structure, animate);
      if (animate) { flash("results"); fly(e.agent, "critic", "result", 700); }
      if (p.uptake > S.best) {
        S.best = p.uptake;
        if (animate && performance.now() - S.lastToast > 2500) {
          S.lastToast = performance.now();
          toast(`New best found: <b>${esc(name(p.structure))}</b> ${f3(p.uptake)} mol/kg · tier ${p.tier}${p.tier === 1 ? " (unconfirmed)" : ""}`);
          const xy = S.poolXY && S.poolXY(p.structure); if (xy) burst(xy.x, xy.y, "#1E9B62");
        }
      }
      break;
    }
    case "timeout":
      setWorker(e.agent, "killed", "TIMEOUT");
      if (animate) { const w = S.workers[e.agent]; if (w) burst(w.x, WORKER_Y, "#D7353A"); }
      setTimeout(() => setWorker(e.agent, "", ""), 1200);
      break;
    case "recommend":
      setNode("orchestrator", "recovered");
      S.rec = p;
      if (animate) { burst(NODES.orchestrator.x, NODES.orchestrator.y, "#1E9B62"); toast(p.aps != null ? `Recommended: <b>${esc(name(p.structure))}</b> APS ${f3(p.aps)} mol/kg · capture-checked` : `Recommended: <b>${esc(name(p.structure))}</b> ${f3(p.uptake)} mol/kg · tier-2 confirmed`); bubble("orchestrator", p.text || "recommendation made"); }
      chat(e);
      break;
    case "phase": S.phase = p; break;
    case "capture_required": case "disorder_check_required": case "disorder_artifact": case "disorder_passed":
      S.checks[e.type] = (S.checks[e.type] || 0) + 1;
      if (e.type === "disorder_artifact") S.vetoed[p.structure] = p.text || "disorder artifact";
      if (animate) bubble("critic", p.text || e.type.replace(/_/g, " "), e.type === "disorder_passed" ? "" : "veto");
      chat(e);
      break;
    case "decision":
      if (p.families) S.families = p.families;
      if (e.agent === "allocator" && p.kind === "random_slice") S.randomSlice = (S.randomSlice || 0) + (p.n || 0);
      break;
    case "round_start":
      S.curRound = p.round; S.rounds[p.round] = S.rounds[p.round] || { round: p.round }; Object.assign(S.rounds[p.round], { n: p.n, split: p.split || p.phase });
      renderRounds();
      break;
    case "round_end":
      S.rounds[p.round] = Object.assign(S.rounds[p.round] || { round: p.round }, { spent: p.spent_cpu_s, best: p.best, done: true });
      renderRounds();
      break;
    default:
  }
}

// queue: live events animate one after another; a backlog fast-forwards silently
function enqueue(evs, animate) {
  if (!animate) { for (const e of evs) act(e, false); renderBoard(); return; }
  S.queue.push(...evs);
  if (!S.busy) drain();
}
function drain() {
  const e = S.queue.shift();
  if (!e) { S.busy = false; return; }
  S.busy = true;
  const backlog = S.queue.length;
  act(e, backlog < 25);
  renderBoard();
  const gap = backlog > 25 ? 0 : { agent_start: 260, agent_finish: 420, veto: 380, task_start: 90, task_done: 140, recommend: 900 }[e.type] ?? 40;
  setTimeout(drain, gap / Math.max(1, Math.min(4, backlog / 6 + 1)));
}

// ------------------------------------------------------------------------------------ chat
function chat(e) {
  const p = e.p || {}, role = (e.agent && e.agent.includes("/")) ? e.agent : (p.role || e.agent || "orchestrator");
  const li = document.createElement("li");
  if (role.includes("/")) li.style.marginLeft = `${16 * (role.split("/").length - 1)}px`;
  const kind = e.type === "agent_fail" ? "fail" : e.type === "recommend" ? "win" : role;
  li.className = `r-${kind.split("/")[0]}${role.includes("/") ? " sub" : ""}`;
  const to = role.includes("/") ? `→ ${e.parent || role.split("/").slice(0, -1).join("/")}` : e.type === "delegate" ? "→ sub-agents" : e.type === "veto" ? "→ orchestrator" : role === "orchestrator" ? "→ team" : "→ orchestrator";
  let body = `<div class="text">${esc(p.text || (e.type === "agent_fail" ? `call failed: ${p.error}` : e.type))}</div>`;
  const d = p.decision || {};
  if (d.jobs) body += `<div class="jobs">${d.jobs.map((j) => `<span class="${j.tier === 2 ? "t2" : ""}" title="${esc(j.why)}">T${j.tier} ${esc(name(j.structure))}</span>`).join("")}</div>`;
  if (d.vetoes && d.vetoes.length) body += `<div class="jobs">${d.vetoes.map((v) => `<span title="${esc(v.reason)}">veto ${esc(name(v.structure))}</span>`).join("")}</div>`;
  if (d.lcd_target != null) body += `<div class="jobs"><span>cavity ${(+d.lcd_target).toFixed(1)} ± ${(+d.lcd_width || 0).toFixed(1)} Å</span><span>void ×${(+d.w_void || 0).toFixed(2)}</span>${d.w_oms ? `<span>open metal +${(+d.w_oms).toFixed(2)}</span>` : ""}${Object.entries(d.metal_bonus || {}).slice(0, 3).map(([m, v]) => `<span>${esc(m)} +${(+v).toFixed(2)}</span>`).join("")}</div>`;
  if (p.reasoning) body += `<div class="reason">${esc(p.reasoning.slice(-1800))}</div>`;
  li.innerHTML = `<div class="head"><b>${esc(role)}</b><span class="to">${to}</span>${p.model ? `<span>${esc(p.model.split("/").pop().replace("NVIDIA-", ""))}</span>` : ""}${p.round != null ? `<span>round ${p.round}</span>` : ""}<span class="t">${clock(e.ts)}</span></div>${body}`;
  li.onclick = () => li.classList.toggle("open");
  const ol = $("chat");
  ol.insertBefore(li, ol.firstChild);
  while (ol.children.length > 200) ol.removeChild(ol.lastChild);
  $("chat-empty").hidden = true;
}

// ------------------------------------------------------------------------------ board, rounds
let boardKeys = "";
function renderBoard() {
  const top = Object.entries(S.measured).filter(([s]) => !/_dres/.test(s)).sort((a, b) => b[1][0] - a[1][0]).slice(0, 10);
  const key = top.map(([s, m]) => s + m[2] + (S.vetoed[s] ? "v" : "") + (S.capture[s] ? "c" : "") + ((S.flags[s] || {}).dis ? "d" : "")).join();
  if (key === boardKeys) return;
  const prev = new Set(boardKeys.split(",")); boardKeys = key;
  $("board").innerHTML = top.map(([s, m], i) => {
    const c = S.capture[s] || S.capture[s.replace(/_clean$/, "_dres_clean")];
    const tag = S.vetoed[s] ? `<span class="tag veto">VETOED</span>` : c ? `<span class="tag t3" title="capture check: S ${c.S == null ? "–" : (+c.S).toFixed(1)}, ΔN ${f3(c.dn)} mol/kg">APS ${f3(c.aps)}</span>`
      : m[2] === 2 ? `<span class="tag ok">CONFIRMED</span>` : `<span class="tag">tier 1</span>`;
    const fl = S.flags[s] || {};
    const badges = (window.Shell ? Shell.fam(s) + Shell.dis(fl.dis) : "");
    return `<li class="${prev.has(s + m[2] + (S.vetoed[s] ? "v" : "")) ? "" : "new"}"><span class="n">${i + 1}</span><span class="s" title="${esc(s)}">${esc(name(s))} ${badges}</span><span class="u">${f3(m[0])}</span>${tag}</li>`;
  }).join("") || `<li class="empty">Nothing measured yet.</li>`;
}
function renderRounds() {
  const box = $("rounds");
  const rs = Object.values(S.rounds).filter((r) => r.round > 0).sort((a, b) => a.round - b.round);
  box.innerHTML = rs.map((r) => `<div class="rnd ${r.round === S.curRound && !r.done ? "now" : ""}"><div class="h">ROUND ${r.round}</div>
    <div class="b">${r.n ?? "–"} jobs</div><div class="dim">${esc(r.split || "")}</div>
    ${r.best ? `<div class="mono">${esc(name(r.best.structure)).slice(0, 12)} ${(+r.best.uptake).toFixed(2)}</div>` : ""}
    ${r.spent != null ? `<div class="dim">${(r.spent / 3600).toFixed(2)} CPU-h</div>` : ""}</div>`).join("");
  box.scrollLeft = box.scrollWidth;
}

// -------------------------------------------------------------------------------- frames
function frame(f) {
  if (!f.run) return;
  const v = f.vitals || {};
  $("a-int").textContent = v.interventions ?? 0;
  $("a-spent").textContent = f2(v.spent_cpu_s / 3600); $("a-budget").textContent = f2(f.run.budget_cpu_s / 3600);
  $("a-bar").style.width = `${Math.min(100, (100 * v.spent_cpu_s) / f.run.budget_cpu_s)}%`;
  const b = v.best_t2 ? { ...v.best_t2, tier: 2 } : v.best;
  $("a-best").textContent = b ? f3(b.uptake_molkg) : "–";
  $("a-best-sub").textContent = b ? `${name(b.structure)} · ${b.tier === 2 ? "confirmed" : "tier 1"}` : "";
  const sp = v.spend || [];
  const tok = sp.reduce((a, s) => a + (s.tokens || 0), 0);
  $("a-tokens").textContent = tok ? `${(tok / 1000).toFixed(1)}k` : "–";
  $("a-split").textContent = sp.map((s) => `${(s.model || "").includes("Nano") ? "Nano" : "Super"} ${((s.tokens || 0) / 1000).toFixed(1)}k${s.usd ? ` $${s.usd.toFixed(2)}` : ""}`).join(" · ");
  $("a-round").textContent = S.curRound ?? "–";
  layoutWorkers((f.workers || []).map((w) => w.worker_id));
  $("stage-note").textContent = (f.run.arm || "").startsWith("sieve") ? `· ${f.run.finished ? "finished" : "live"}` : `· "${f.run.arm}" is a fixed-policy baseline — no agents`;
  S.flags = f.flags || {};
  for (const [k, c] of Object.entries(f.capture || {})) S.capture[k] = S.capture[k] || c;
  renderFlow(f);
  renderBoard();
}

// ------------------------------------------------------------------------- flow strip
// One lane per kind of agent work, left to right in the order a round runs, with live counts;
// the lane whose agent is working now is outlined, and the phase stepper shows where the run is.
function renderFlow(f) {
  const fl = f.flow || {}, j = fl.jobs || {}, cap = f.objective === "capture", agentic = (f.run.arm || "").startsWith("sieve");
  const phases = [["explore", "1 · explore T1"], ["confirm", "2 · confirm T2"], ["capture", "3 · capture check T3"], ["final", "final checks"], ["done", "recommend"]];
  const cur = f.run.finished ? "done" : (S.phase || f.phase || {}).phase || (agentic ? "explore" : null);
  const order = phases.map((p) => p[0]);
  $("phases").innerHTML = phases.map(([k, label]) => {
    const off = (k === "capture" && !cap) || (k === "final" && !cap && !f.disorder_check);
    const cls = off ? "off" : k === cur ? `now${k === "capture" ? " t3" : ""}` : order.indexOf(k) < order.indexOf(cur) ? "done" : "";
    return `<li class="${cls}"${off ? ` title="not used by this run (${k === "capture" ? "objective: uptake" : "no finalist checks"})"` : ""}>${label}</li>`;
  }).join("");
  $("flow-note").textContent = `· objective: ${cap ? "flue-gas capture (APS)" : "1-bar uptake"} · pool: ${f.pool === "mixed" ? "MOFs + zeolites" : "CoRE MOFs"}`;
  $("flow-more").href = `/how?run=${encodeURIComponent(f.run.run_id)}`;
  const st = {};
  for (const a of f.agents || []) { const r = (a.agent_id || "").split("/")[0]; if (a.state === "working" || !st[r]) st[r] = a.state; }
  const run = (f.workers || []).filter((w) => w.state === "running");
  const byT = (t) => run.filter((w) => w.tier === t).length;
  const T = (t, n) => (n ? `${Shell.tier(t)} ${n}` : "");
  const famN = S.families ? Object.values(S.families) : null;
  const sf = [...(S.subFams || [])], fz = sf.filter((x) => x.startsWith("z")).length;
  const famTxt = famN ? `${famN.filter((x) => x.class !== "zeolite").length} MOF${famN.some((x) => x.class === "zeolite") ? ` + ${famN.filter((x) => x.class === "zeolite").length} zeolite` : ""} families`
    : sf.length ? `${sf.length - fz} MOF${fz ? ` + ${fz} zeolite` : ""} families scouted` : "families not scouted yet";
  const caps = Object.entries(S.capture).sort((a, b) => (b[1].aps ?? -1e9) - (a[1].aps ?? -1e9));
  const ck = S.checks, rec = S.rec;
  const lanes = [
    ["orchestrator", "Orchestrator", `round ${S.curRound ?? "–"} · phase ${cur || "–"}`, esc((S.lastText.orchestrator || "plans the budget split").replace(/^round \d+ — /, "").slice(0, 140))],
    ["scout", "Scout", famTxt, S.families ? Object.entries(S.families).sort((a, b) => b[1].promise - a[1].promise).slice(0, 4)
      .map(([k, v]) => `${k}${v.class === "zeolite" ? "ᶻ" : ""} ${(+v.promise).toFixed(2)}`).join(" · ") : ""],
    ["allocator", "Allocator", `queued: ${[T(1, j[1]?.queued), T(2, j[2]?.queued), T(3, j[3]?.queued)].filter(Boolean).join(" ") || "none"}`,
      `random slice ${fl.random_slice || 0}${fl.disorder_jobs ? ` · disorder re-measures ${fl.disorder_jobs}` : ""}`],
    ["workers", "Workers", run.length ? `running: ${[T(1, byT(1)), T(2, byT(2)), T(3, byT(3))].filter(Boolean).join(" ")}` : "idle",
      `done ${Shell.tier(1)} ${j[1]?.done || 0} ${Shell.tier(2)} ${j[2]?.done || 0}${cap || j[3]?.done ? ` ${Shell.tier(3)} ${j[3]?.done || 0}` : ""}`],
    ["critic", "Critic", `vetoes ${fl.vetoes || 0}${cap ? ` · needs T3 ${fl.capture_required || 0}` : ""}`,
      f.disorder_check ? `disorder checks ${fl.disorder_required || 0} · passed ${fl.disorder_passed || 0} · artifacts ${fl.disorder_artifacts || 0}` : "no disorder check in this run"],
    ["recommend", "Recommend", rec ? `${esc(name(rec.structure))}` : "not yet",
      rec ? (rec.aps != null ? `APS ${f3(rec.aps)} · S ${(+rec.S).toFixed(1)} · ΔN ${f3(rec.dn)}` : `${f3(rec.uptake)} mol/kg, tier ${rec.tier}`)
        : cap ? (caps.length ? `best APS so far ${f3(caps[0][1].aps)} (${esc(name(caps[0][0]))})` : "needs T2 + a T3 capture check") : "needs tier-2 confirmation"],
  ];
  $("lanes").innerHTML = lanes.map(([k, label, a, b]) => {
    const state = k === "workers" ? (run.length ? "working" : "idle") : k === "recommend" ? (rec ? "done" : "") : st[k] || "";
    return `<li class="${state}"><div class="lh">${label}<span class="st">${state === "working" ? "working" : state === "failed" ? "fell back" : state === "recovered" ? "rule-based" : ""}</span></div>
      <div class="lb">${a}</div><div class="lb dim">${b}</div></li>`;
  }).join("");
}

// ----------------------------------------------------------------------------------- boot
async function boot() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  S.speed = cfg.speed || 1;
  $("replay").hidden = !cfg.replay;
  const sieveRuns = cfg.runs.filter((r) => (r.arm || "").startsWith("sieve"));
  const want = params.get("run") || cfg.replay || (sieveRuns[0] || cfg.runs[0] || {}).run_id;
  const sel = $("run-select");
  for (const r of cfg.runs) sel.appendChild(new Option(`${r.run_id} · ${r.arm}`, r.run_id, false, r.run_id === want));
  sel.onchange = () => { params.set("run", sel.value); location.search = params.toString(); };
  S.run = want;
  if (window.Shell) Shell.setRun(want);
  buildStage();
  fetch("/api/pool").then((r) => r.json()).then((d) => { S.poolPts = d.points; S.poolByName = Object.fromEntries(d.points.map((p) => [p[0], p])); drawPool(); }).catch(() => {});
  if (!want) return;
  if (params.get("snapshot")) {  // still image: state + each agent's latest words
    const q = encodeURIComponent(want);
    const f = await fetch(`/api/frame?run=${q}`).then((r) => r.json());
    const evs = (await fetch(`/api/events?run=${q}&limit=3000`).then((r) => r.json()))
      .filter((e) => e.type !== "worker_start" && e.type !== "llm_usage" && e.type !== "agent_tool");
    frame(f); enqueue(evs, false); frame(f);
    const last = {};
    for (const e of evs) if (e.type === "agent_finish" || e.type === "veto" || e.type === "recommend") last[(e.p || {}).role || e.agent] = e;
    for (const [role, e] of Object.entries(last)) bubble(role, (e.p || {}).text || e.type, e.type === "veto" ? "veto" : "");
    return;
  }
  let first = true;
  const es = new EventSource(`/api/stream?run=${encodeURIComponent(want)}&backlog=2000`);
  es.addEventListener("frame", (m) => frame(JSON.parse(m.data)));
  es.addEventListener("events", (m) => {
    const evs = JSON.parse(m.data).filter((e) => e.type !== "worker_start" && e.type !== "llm_usage" && e.type !== "agent_tool");
    for (const e of evs) if (e.type === "delegate") e.p.role = e.p.role || (e.agent || "").split("/")[0];
    enqueue(evs, !first); first = false;
  });
}
// the Validation view (validation.js) owns the page when ?view=validation; the run view boots on first show
let booted = false;
window.agentsBoot = () => { if (!booted) { booted = true; boot(); } };
if (params.get("view") !== "validation") window.agentsBoot();
