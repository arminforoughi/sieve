"use strict";
// Science view for one structure: binding pockets (RASPA's own probe-energy grid as isosurfaces),
// the 298 K isotherm against reference MOFs, the pocket-energy distribution, every screening
// measurement, the peer review, and downloads. Any pool structure can be requested; the server
// computes it once (status "computing") and the page polls.
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const SNAP = !!params.get("snapshot");
const ACCENT = "#0B8A98", ACCENT_DEEP = "#06525C", INK = "#15171C", INK2 = "#4A505B";
const REFS = ["Cu-BTC", "MgMOF-74-VASP"];
// literature values for orientation only (experiment includes the electrostatics this model leaves out)
const LIT = { "Cu-BTC": ["≈ 4–4.5", "HKUST-1, ~298 K, 1 bar"], "MgMOF-74-VASP": ["≈ 8", "296 K, 1 atm (Caskey et al., JACS 2008)"] };
// series styles: the selected structure is the only coloured series; comparators are ink tones + dashes
const COMP_STYLE = [{ c: INK, dash: "6 3" }, { c: "#7C828E", dash: "2 3" }, { c: "#A9AEB7", dash: "9 3 2 3" }];
const S = { items: [], data: {}, meas: {}, sel: null, v: null, fw: null, cube: null, shapes: [], frames: [], co2: null, anim: null, moved: false, tick: null };
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fx = (x, k = 2) => (x == null || isNaN(x) ? "–" : Number(x).toFixed(k));
const label = (n) => ({ "Cu-BTC": "HKUST-1", "MgMOF-74-VASP": "Mg-MOF-74" }[n] || String(n).replace(/_clean$/, ""));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getJSON = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
const typical = () => (S.items.find((i) => /typical/.test(i.role)) || {}).name;
const at = (d, pb) => (d && d.isotherm ? d.isotherm.find((q) => Math.abs(q.p_bar - pb) < 1e-9) || {} : {});
const tierTag = (t) => `<span class="tier t${t}">T${t}</span>`;

// ------------------------------------------------------------------------------------ boot
async function boot() {
  try { S.items = await getJSON("/api/science/list"); } catch (_) { S.items = []; }
  S.sel = params.get("name") || (S.items[0] || {}).name || "ZADREE_clean";
  renderPresets();
  initSearch();
  ["L-fw", "L-pk", "L-co2"].forEach((id) => ($(id).onchange = () => layers()));
  $("iso").oninput = () => { $("iso-v").textContent = `−${Math.abs(+$("iso").value)}`; };
  $("iso").onchange = () => { pockets(); legend3d(); caption(); };
  $("spin").onclick = () => { const on = $("spin").getAttribute("aria-pressed") !== "true"; $("spin").setAttribute("aria-pressed", on); if (S.v) S.v.spin(on ? "y" : false, 0.4); };
  $("reset").onclick = () => { S.moved = false; fit(); };
  $("hist-vs").onchange = histChart;
  for (const id of ["iso-chart", "hist-chart"]) new ResizeObserver(() => drawCharts()).observe($(id));
  // comparators load quietly in the background (only if already computed: never start work for them)
  for (const n of [...REFS, typical()].filter(Boolean)) {
    if ((S.items.find((i) => i.name === n) || {}).ready) loadScience(n, false);
    loadMeas(n);
  }
  select(S.sel);
}

function renderPresets() {
  $("presets").innerHTML = S.items.map((it) => `<button data-n="${esc(it.name)}" title="${esc(it.role)}">${esc(label(it.name))}<small>${esc(it.role.replace(/^reference: .*/, "reference").replace("best confirmed on the full pool", "best confirmed").replace("a typical pool structure (median)", "typical").replace("computed on request", ""))}</small></button>`).join("");
  $("presets").onclick = (e) => { const b = e.target.closest("button"); if (b) select(b.dataset.n); };
  markPresets();
}
function markPresets() { document.querySelectorAll("#presets button").forEach((b) => b.classList.toggle("on", b.dataset.n === S.sel)); }

function select(name) {
  S.sel = name; S.moved = false;
  params.set("name", name);
  history.replaceState(null, "", `?${params}`);
  markPresets();
  const it = S.items.find((i) => i.name === name);
  document.title = `SIEVE · ${label(name)}`;
  $("title").textContent = label(name);
  $("role").textContent = it && it.role !== "computed on request" ? it.role : "pool structure";
  $("dl-cif").href = `/api/download/cif?name=${encodeURIComponent(name)}`;
  if (S.v && S.fwName !== name) { clearInterval(S.anim); S.v.clear(); S.v.removeAllShapes(); S.fw = null; S.fwName = null; S.shapes = []; S.v.render(); }
  $("wait").classList.remove("pill"); waitMsg(`<span><span class="spin"></span> Loading structure…</span>`);
  renderAll();
  loadMeas(name).then(() => { if (S.sel === name) renderAll(); });
  loadScience(name, true);
  loadReview(name);
}

// ----------------------------------------------------------------------------------- data
async function loadMeas(name) {
  if (S.meas[name]) return S.meas[name];
  try { S.meas[name] = await getJSON(`/api/measurements?name=${encodeURIComponent(name)}`); }
  catch (_) { S.meas[name] = { rows: [], pool: null }; }
  if (S.sel === name || S.data[S.sel]) renderAll();
  return S.meas[name];
}

async function loadScience(name, primary) {
  if (S.data[name] && S.data[name].status === "ready") { if (primary) { status(null); show(); } return; }
  for (;;) {
    if (primary && S.sel !== name) return;
    let d;
    try { d = await getJSON(`/api/science?name=${encodeURIComponent(name)}`); }
    catch (e) { d = { status: "neterror", error: e.message }; }
    if (d.status === "ready") {
      S.data[name] = d;
      if (S.sel === name) { status(null); renderAll(); show(); } else { drawCharts(); table(); caption(); }
      return;
    }
    if (!primary) return;
    if (d.status === "error") { S.data[name] = d; status("error", d.error); renderAll(); waitMsg(`Could not compute this structure: ${esc(d.error)}`); return; }
    if (d.status === "neterror") { status("error", `The dashboard server did not answer (${esc(d.error)}). Retrying…`); await sleep(8000); continue; }
    status("computing", null, d.since);
    if (S.fwName !== name && !S.previewing) { S.previewing = true; preview(name).finally(() => (S.previewing = false)); }
    waitMsg(`<span><span class="spin"></span> Framework shown; binding pockets and CO₂ positions appear when the energy grid is computed…</span>`);
    await sleep(SNAP ? 1500 : 5000);
    if (SNAP) return;
  }
}

function status(kind, msg, since) {
  const box = $("status");
  clearInterval(S.tick);
  if (!kind) { box.hidden = true; return; }
  box.hidden = false;
  box.className = `callout ${kind === "error" ? "bad" : "info"} status`;
  if (kind === "error") { box.innerHTML = `<div><b>Not available.</b> ${msg}</div>`; return; }
  const draw = () => {
    const s = since ? Math.max(0, Date.now() / 1000 - since) : 0, m = Math.floor(s / 60);
    box.innerHTML = `<span class="spin"></span><div><b>Computing ${esc(label(S.sel))}</b>${since ? ` · ${m} min ${Math.floor(s % 60)} s so far` : ""}. `
      + `RASPA builds the 0.5 Å CO₂-probe energy grid, then runs GCMC at 6 pressures (0.05–5 bar, 150 + 500 cycles each, 3 at a time). `
      + `Expect roughly 5–20 minutes while screening runs share the CPU. Results are cached in <code>data/science/</code>: you can leave and come back. `
      + `Descriptors and any screening measurements are shown below already.</div>`;
  };
  draw(); S.tick = setInterval(draw, 1000);
}

// ------------------------------------------------------------------------------- render all
function renderAll() { header(); kpis(); exports(); drawCharts(); table(); measTable(); caption(); }

function header() {
  const m = S.meas[S.sel] || {}, p = m.pool || {}, d = S.data[S.sel] || {};
  const metals = (p.metals && p.metals.length ? p.metals : d.metals) || [];
  const best = bestMeas(S.sel);
  const tags = [];
  if (metals.length) tags.push(`<span class="chip">${esc(metals.join(" · "))}</span>`);
  if (p.oms) tags.push(`<span class="chip warn" title="open metal sites bind CO₂ electrostatically; this charge-free model under-rates them">open metal sites</span>`);
  if (best) tags.push(`<span class="chip ${best.tier === 2 ? "ok" : "warn"}">screened: ${tierTag(best.tier)} ${fx(best.uptake)} ± ${fx(best.err)} mol/kg</span>`);
  else if (m.rows && m.pool) tags.push(`<span class="chip">not screened by any run</span>`);
  if (d.n_atoms || p.n_atoms) tags.push(`<span class="chip">${d.n_atoms || p.n_atoms} atoms / cell</span>`);
  if (m.rows && !m.pool) tags.push(`<span class="chip" title="reference structure outside the 12,020-structure CoRE pool: no tier-0 descriptors">not in the screening pool</span>`);
  $("tags").innerHTML = tags.join("");
}

function bestMeas(name) {
  const rows = (S.meas[name] || {}).rows || [];
  return rows.slice().sort((a, b) => b.tier - a.tier || a.err - b.err)[0];
}

function kpis() {
  const d = S.data[S.sel] || {}, p = (S.meas[S.sel] || {}).pool || {}, k = d.pockets || {};
  const ready = d.status === "ready", wait = ready ? "–" : `<span class="skel" style="display:inline-block;width:70px"></span>`;
  const a15 = at(d, 0.15), a1 = at(d, 1);
  const kpi = (lab, v, sub, hl) => `<div class="kpi ${hl ? "hl" : ""}"><div class="k">${lab}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  $("kpis").innerHTML =
    kpi("Uptake at 0.15 bar", a15.uptake != null ? `${fx(a15.uptake)} <small>± ${fx(a15.err)}</small>` : wait, "mol/kg · flue-gas CO₂ pressure", true)
    + kpi("Uptake at 1 bar", a1.uptake != null ? `${fx(a1.uptake)} <small>± ${fx(a1.err)}</small>` : wait, "mol/kg · 298 K")
    + kpi("Strong-binding pore", k.strong_fraction != null ? `${Math.round(100 * k.strong_fraction)}<small>%</small>` : wait, "of accessible volume below −10 kJ/mol")
    + kpi("Deepest well", k.min_kjmol != null ? `${fx(k.min_kjmol, 1)}` : wait, `kJ/mol · median ${fx(k.median_kjmol, 1)}`)
    + kpi("PLD / LCD", p.PLD != null ? `${fx(p.PLD, 1)} <small>/</small> ${fx(p.LCD, 1)}` : "–", "Å · pore-limiting / largest cavity")
    + kpi("Void fraction · density", p.VF != null ? `${fx(p.VF)} <small>·</small> ${fx(p.density)}` : "–", "· g/cm³ (tier-0 descriptors)");
}

function exports() {
  const ready = (S.data[S.sel] || {}).status === "ready", q = encodeURIComponent(S.sel);
  for (const [id, kind] of [["dl-csv", "isotherm.csv"], ["dl-cube", "cube"], ["dl-json", "json"]]) {
    const a = $(id);
    if (ready) { a.href = `/api/download/${kind}?name=${q}`; a.removeAttribute("aria-disabled"); a.title = ""; }
    else { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); a.title = "available once computed"; }
  }
}

// -------------------------------------------------------------------------------------- 3D
function waitMsg(html) { const w = $("wait"); w.hidden = false; w.innerHTML = html; }

function viewer() {
  if (S.v || S.noGL) return S.v;
  if (!window.$3Dmol) { S.noGL = true; waitMsg("3Dmol.js did not load (offline?)."); return null; }
  try { S.v = $3Dmol.createViewer($("gl"), { backgroundColor: "white", antialias: true }); }
  catch (_) { S.noGL = true; waitMsg("The 3D view needs WebGL, which this browser has disabled."); return null; }
  // the container is laid out by CSS grid and changes size with the window and the side pane: keep
  // the canvas matched to it and, until the user has moved the camera, keep the structure framed
  new ResizeObserver(() => { if (!S.v) return; S.v.resize(); if (!S.moved) fit(); else S.v.render(); }).observe($("gl"));
  for (const ev of ["mousedown", "wheel", "touchstart"]) $("gl").addEventListener(ev, () => (S.moved = true), { passive: true });
  return S.v;
}
function fit() { if (S.v && S.fw) { S.v.zoomTo({ model: S.fw }); S.v.zoom(1.2); S.v.render(); } }

async function show() {
  const name = S.sel, d = S.data[name];
  if (!d || !d.cif) return;
  if (!framework(name, d.cif)) return;
  const v = S.v;
  $("wait").hidden = true; $("wait").classList.remove("pill");
  try { S.cube = await fetch(`/api/science/cube?name=${encodeURIComponent(name)}`).then((r) => r.text()); } catch (_) { S.cube = ""; }
  if (S.sel !== name) return;
  pockets();
  getJSON(`/api/viz?name=${encodeURIComponent(name)}`).then((z) => { if (z.status === "ready" && S.sel === name) { S.frames = z.frames || []; layers(); } }).catch(() => {});
  if ($("spin").getAttribute("aria-pressed") === "true") v.spin("y", 0.4);
}

// the bare framework: drawn at once (from the CIF) while the energy grid may still be computing
function framework(name, cif) {
  const v = viewer(); if (!v) return false;
  clearInterval(S.anim); v.spin(false); v.clear(); v.removeAllShapes(); S.shapes = []; S.co2 = null; S.frames = []; S.cube = null;
  S.fw = v.addModel(cif, "cif", { doAssembly: true, duplicateAssemblyAtoms: true, normalizeAssembly: true });
  const ax = { hidden: true, color: "#C3C8D0", radius: 0.02 };
  v.addUnitCell(S.fw, { box: { color: "#C3C8D0" }, astyle: ax, bstyle: ax, cstyle: ax, alabel: "", blabel: "", clabel: "" });
  S.fwName = name; layers(); fit(); legend3d();
  return true;
}
async function preview(name) {
  try {
    const p = await getJSON(`/api/cif?name=${encodeURIComponent(name)}`);
    if (p.error || S.sel !== name || (S.data[name] || {}).status === "ready") return;
    if (framework(name, p.cif)) { $("wait").classList.add("pill"); }
  } catch (_) { /* the full view follows when the data is ready */ }
}

function pockets() {
  const v = S.v; if (!v) return;
  for (const sh of S.shapes) v.removeShape(sh);
  S.shapes = [];
  if (S.cube && $("L-pk").checked) {
    const t = +$("iso").value;
    S.shapes.push(v.addVolumetricData(S.cube, "cube", { isoval: t, color: "#38B2BF", opacity: 0.55, smoothness: 3 }));
    S.shapes.push(v.addVolumetricData(S.cube, "cube", { isoval: t - 6, color: ACCENT_DEEP, opacity: 0.9, smoothness: 3 }));
  }
  v.render();
}

function layers() {
  const v = S.v; if (!v || !S.fw) return;
  v.setStyle({ model: S.fw }, $("L-fw").checked ? { stick: { radius: 0.11, colorscheme: "Jmol" } } : { line: { hidden: true } });
  clearInterval(S.anim);
  if (S.co2) { v.removeModel(S.co2); S.co2 = null; }
  if ($("L-co2").checked && S.frames.length) {
    let i = 0;
    const tick = () => {
      const mols = S.frames[i++ % S.frames.length];
      const xyz = `${mols.length * 3}\nco2\n` + mols.map((m) => `O ${m[0].join(" ")}\nC ${m[1].join(" ")}\nO ${m[2].join(" ")}`).join("\n");
      if (S.co2) v.removeModel(S.co2);
      S.co2 = v.addModel(xyz, "xyz");
      v.setStyle({ model: S.co2 }, { stick: { radius: 0.11, colorscheme: "Jmol" }, sphere: { scale: 0.2, colorscheme: "Jmol" } });
      v.render();
    };
    tick(); if (!SNAP) S.anim = setInterval(tick, 350);
  }
  pockets(); legend3d();
}

function legend3d() {
  const t = Math.abs(+$("iso").value), d = S.data[S.sel] || {};
  const JM = { C: "#909090", H: "#FFFFFF", O: "#FF0D0D", N: "#3050F8", Cu: "#C88033", Zn: "#7D80B0", Mg: "#8AFF00", Co: "#F090A0", Ni: "#50D050", Fe: "#E06633", Zr: "#94E0E0", Al: "#BFA6A6", Mn: "#9C7AC7", Cd: "#FFD98F", S: "#FFFF30", F: "#90E050", Cl: "#1FF01F", Br: "#A62929", P: "#FF8000", B: "#FFB5B5" };
  const els = ["C", "H", "N", "O", ...((S.meas[S.sel] || {}).pool || {}).metals || d.metals || []].filter((e, i, a) => a.indexOf(e) === i);
  $("vlegend").innerHTML = `<span><i style="background:#38B2BF;opacity:.6"></i>probe energy below −${t} kJ/mol</span>`
    + `<span><i style="background:${ACCENT_DEEP}"></i>below −${t + 6} kJ/mol (deepest pockets)</span>`
    + (d.pockets && d.pockets.min_kjmol > -t ? `<span><b>Nothing in this pore is below −${t} kJ/mol</b> (deepest ${fx(d.pockets.min_kjmol, 1)}): lower the threshold to see its best sites.</span>` : "")
    + (S.frames.length && $("L-co2").checked ? `<span>CO₂ from a GCMC snapshot (red O, grey C)</span>` : "")
    + els.map((e) => `<span class="el"><i style="background:${JM[e] || "#D0D0D0"}"></i>${e}</span>`).join("");
}

function caption() {
  const d = S.data[S.sel];
  if (!d || !d.pockets || !d.isotherm) { $("cap").textContent = ""; return; }
  const p = d.pockets, u15 = at(d, 0.15).uptake, tn = typical(), t15 = at(S.data[tn], 0.15).uptake, h15 = at(S.data["Cu-BTC"], 0.15).uptake;
  $("cap").innerHTML = `The shaded surfaces enclose the regions where a CO₂ oxygen is held more strongly than the threshold, computed from the same force field the GCMC uses. `
    + `<b>${Math.round(100 * p.strong_fraction)}%</b> of the accessible pore binds below −10 kJ/mol; the deepest well is <b>${fx(p.min_kjmol, 1)} kJ/mol</b>. `
    + (u15 != null ? `At 0.15 bar it holds <b>${fx(u15)} mol/kg</b>` + [t15 && S.sel !== tn ? `${fx(u15 / t15, 1)}× the typical pool structure` : "", h15 && S.sel !== "Cu-BTC" ? `${fx(u15 / h15, 1)}× HKUST-1 in the same model` : ""].filter(Boolean).map((s, i) => (i ? " and " : ", ") + s).join("") + "." : "");
}

// ---------------------------------------------------------------------------------- charts
function compared() {
  return [S.sel, ...REFS, typical()].filter((n, i, a) => n && a.indexOf(n) === i);
}
function styleOf(name) {
  if (name === S.sel) return { c: ACCENT, dash: null, w: 2.25, r: 3.5 };
  const k = compared().filter((n) => n !== S.sel).indexOf(name);
  return { ...COMP_STYLE[k % COMP_STYLE.length], w: 1.4, r: 2.5 };
}
const swatch = (st, mark) => `<svg width="26" height="10" aria-hidden="true"><line x1="1" x2="25" y1="5" y2="5" stroke="${st.c}" stroke-width="${st.w}" ${st.dash ? `stroke-dasharray="${st.dash}"` : ""}/>${mark ? `<circle cx="13" cy="5" r="2.6" fill="${st.c}"/>` : ""}</svg>`;

function frameOf(id, h) {
  const el = $(id).querySelector("svg"), W = Math.max(280, el.clientWidth || $(id).clientWidth || 500), H = h;
  const svg = d3.select(el).attr("viewBox", `0 0 ${W} ${H}`).attr("height", H); svg.selectAll("*").remove();
  return { svg, W, H };
}
function drawCharts() { if (!window.d3) return; isoChart(); histChart(); }

function isoChart() {
  const { svg, W, H } = frameOf("iso-chart", 300), narrow = W < 460, m = { l: 48, r: narrow ? 12 : 92, t: 12, b: 40 };
  const names = compared().filter((n) => S.data[n] && S.data[n].isotherm);
  const meas = ((S.meas[S.sel] || {}).rows || []);
  const ys = names.flatMap((n) => S.data[n].isotherm.map((p) => (p.uptake || 0) + (p.err || 0))).concat(meas.map((r) => r.uptake + r.err));
  const x = d3.scaleLog().domain([0.04, 6.5]).range([m.l, W - m.r]);
  const y = d3.scaleLinear().domain([0, Math.max(1, d3.max(ys) || 1) * 1.06]).nice().range([H - m.b, m.t]);
  svg.append("g").attr("class", "gridl").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5).tickSize(-(W - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).tickValues([0.05, 0.15, 0.5, 1, 2, 5]).tickFormat((d) => `${d}`));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5));
  svg.append("text").attr("class", "lbl").attr("x", (m.l + W - m.r) / 2).attr("y", H - 6).attr("text-anchor", "middle").text("CO₂ pressure (bar, log scale)");
  svg.append("text").attr("class", "lbl").attr("transform", `translate(12,${(m.t + H - m.b) / 2}) rotate(-90)`).attr("text-anchor", "middle").text("uptake (mol/kg)");
  for (const [pb, lab] of [[0.15, "flue gas · 0.15 bar"], [1, "1 bar · screening"]]) {
    svg.append("line").attr("x1", x(pb)).attr("x2", x(pb)).attr("y1", m.t).attr("y2", H - m.b).attr("stroke", pb === 0.15 ? INK2 : "#C9CED6").attr("stroke-dasharray", "3 3");
    svg.append("text").attr("class", "lbl ink").attr("x", x(pb) + 4).attr("y", m.t + 10).text(lab);
  }
  const lg = [];
  if (!names.length) {
    const st = (S.data[S.sel] || {}).status;
    svg.append("text").attr("class", "msg").attr("x", (m.l + W - m.r) / 2).attr("y", H / 2).attr("text-anchor", "middle").text(st === "error" ? "No isotherm for this structure." : "Isotherm is being simulated…");
  }
  const line = d3.line().x((p) => x(p.p_bar)).y((p) => y(p.uptake)).defined((p) => p.uptake != null);
  const ends = [];
  for (const n of names.slice().reverse()) {  // selected drawn last, on top
    const st = styleOf(n), pts = S.data[n].isotherm.filter((p) => p.uptake != null);
    const g = svg.append("g");
    g.append("path").attr("d", line(pts)).attr("fill", "none").attr("stroke", st.c).attr("stroke-width", st.w).attr("stroke-dasharray", st.dash);
    for (const p of pts) if (p.err) {
      g.append("line").attr("x1", x(p.p_bar)).attr("x2", x(p.p_bar)).attr("y1", y(p.uptake - p.err)).attr("y2", y(p.uptake + p.err)).attr("stroke", st.c).attr("stroke-width", 1);
      for (const s of [-1, 1]) g.append("line").attr("x1", x(p.p_bar) - 3).attr("x2", x(p.p_bar) + 3).attr("y1", y(p.uptake + s * p.err)).attr("y2", y(p.uptake + s * p.err)).attr("stroke", st.c).attr("stroke-width", 1);
    }
    g.selectAll(null).data(pts).enter().append("circle").attr("cx", (p) => x(p.p_bar)).attr("cy", (p) => y(p.uptake)).attr("r", st.r).attr("fill", st.c).attr("stroke", "#fff").attr("stroke-width", 1)
      .append("title").text((p) => `${label(n)} · ${p.p_bar} bar: ${fx(p.uptake, 3)} ± ${fx(p.err, 3)} mol/kg`);
    const last = pts.at(-1);
    if (last) ends.push({ y: y(last.uptake), t: label(n), sel: n === S.sel });
  }
  // screening-run measurements of the selected structure (all at 1 bar)
  const mk = { 1: false, 2: false };
  meas.forEach((r, i) => {
    const cx = x(1) + (i - (meas.length - 1) / 2) * 7, cy = y(r.uptake), filled = r.tier === 2; mk[r.tier] = true;
    svg.append("line").attr("x1", cx).attr("x2", cx).attr("y1", y(r.uptake - r.err)).attr("y2", y(r.uptake + r.err)).attr("stroke", INK).attr("stroke-width", 1);
    svg.append("path").attr("d", d3.symbol(d3.symbolDiamond, 46)()).attr("transform", `translate(${cx},${cy})`).attr("fill", filled ? INK : "#fff").attr("stroke", INK).attr("stroke-width", 1.2)
      .append("title").text(`${r.run_id} · tier ${r.tier}: ${fx(r.uptake, 3)} ± ${fx(r.err, 3)} mol/kg`);
  });
  if (!narrow) {
    ends.sort((a, b) => a.y - b.y).forEach((e, i, a) => { if (i && e.y - a[i - 1].y < 13) e.y = a[i - 1].y + 13; });
    for (const e of ends) svg.append("text").attr("class", "lbl ink").attr("x", W - m.r + 8).attr("y", e.y + 4).attr("font-weight", e.sel ? 600 : 400).text(e.t);
  }
  for (const n of names) lg.push(`<span>${swatch(styleOf(n), true)}${esc(label(n))}${n === S.sel ? "" : REFS.includes(n) ? " (reference)" : " (typical)"}</span>`);
  if (mk[2]) lg.push(`<span><svg width="12" height="12"><path d="${d3.symbol(d3.symbolDiamond, 46)()}" transform="translate(6,6)" fill="${INK}"/></svg>screening run, tier 2</span>`);
  if (mk[1]) lg.push(`<span><svg width="12" height="12"><path d="${d3.symbol(d3.symbolDiamond, 46)()}" transform="translate(6,6)" fill="#fff" stroke="${INK}"/></svg>screening run, tier 1 (unconverged)</span>`);
  $("iso-legend").innerHTML = lg.join("");
}

function histChart() {
  const sel = $("hist-vs"), opts = [...REFS, typical()].filter((n) => n && n !== S.sel);
  const keep = sel.value || typical() || "";
  sel.innerHTML = opts.map((n) => `<option value="${esc(n)}">${esc(label(n))}</option>`).join("") + `<option value="none">nothing</option>`;
  sel.value = opts.includes(keep) || keep === "none" ? keep : (opts.includes(typical()) ? typical() : opts[0] || "none");
  const { svg, W, H } = frameOf("hist-chart", 220), m = { l: 48, r: 14, t: 12, b: 40 };
  const d = S.data[S.sel], other = S.data[sel.value];
  if (!d || !d.pockets || !d.pockets.hist) {
    svg.append("text").attr("class", "msg").attr("x", W / 2).attr("y", H / 2).attr("text-anchor", "middle").text(d && d.status === "error" ? "No energy grid for this structure." : "Energy grid is being computed…");
    $("hist-legend").innerHTML = ""; return;
  }
  const norm = (h) => { const s = d3.sum(h.counts) || 1; return h.counts.map((c, i) => ({ x0: h.edges[i], x1: h.edges[i + 1], f: c / s })); };
  const a = norm(d.pockets.hist), b = other && other.pockets && other.pockets.hist ? norm(other.pockets.hist) : null;
  const used = [...a, ...(b || [])].filter((q) => q.f > 0.001).map((q) => q.x0);
  const lo = Math.min(-15, Math.floor((d3.min(used) ?? -15) / 5) * 5);
  const x = d3.scaleLinear().domain([lo, 0]).range([m.l, W - m.r]);
  const y = d3.scaleLinear().domain([0, d3.max([...a, ...(b || [])], (q) => q.f) * 1.12]).nice().range([H - m.b, m.t]);
  svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", Math.max(0, x(-10) - m.l)).attr("height", H - m.b - m.t).attr("fill", "rgba(11,138,152,0.06)");
  svg.append("text").attr("class", "lbl ink").attr("x", m.l + 6).attr("y", m.t + 12).text("strong binding (< −10)");
  svg.append("g").attr("class", "gridl").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(6));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickFormat(d3.format(".0%")));
  svg.append("text").attr("class", "lbl").attr("x", (m.l + W - m.r) / 2).attr("y", H - 6).attr("text-anchor", "middle").text("CO₂-oxygen probe energy (kJ/mol)");
  svg.append("text").attr("class", "lbl").attr("transform", `translate(12,${(m.t + H - m.b) / 2}) rotate(-90)`).attr("text-anchor", "middle").text("share of pore");
  svg.selectAll(null).data(a.filter((q) => q.x0 >= lo)).enter().append("rect").attr("x", (q) => x(q.x0) + 1).attr("width", (q) => Math.max(0, x(q.x1) - x(q.x0) - 2))
    .attr("y", (q) => y(q.f)).attr("height", (q) => y(0) - y(q.f)).attr("fill", ACCENT)
    .append("title").text((q) => `${q.x0} to ${q.x1} kJ/mol: ${(100 * q.f).toFixed(1)}%`);
  const lg = [`<span><svg width="14" height="10"><rect width="14" height="10" fill="${ACCENT}"/></svg>${esc(label(S.sel))}</span>`];
  if (b) {
    const pts = b.filter((q) => q.x0 >= lo).flatMap((q) => [[x(q.x0), y(q.f)], [x(q.x1), y(q.f)]]);
    svg.append("path").attr("d", d3.line()(pts)).attr("fill", "none").attr("stroke", INK).attr("stroke-width", 1.4).attr("stroke-dasharray", "4 3");
    lg.push(`<span>${swatch({ c: INK, w: 1.4, dash: "4 3" })}${esc(label(sel.value))}</span>`);
  }
  $("hist-legend").innerHTML = lg.join("");
}

// ------------------------------------------------------------------------- comparison table
function table() {
  const cols = compared();
  const cell = (n, f) => { try { const v = f(n); return v == null || v === "" ? "–" : v; } catch (_) { return "–"; } };
  const D = (n) => S.data[n] || {}, P = (n) => (S.meas[n] || {}).pool || {};
  const upt = (pb) => (n) => { const q = at(D(n), pb); return q.uptake == null ? null : `${fx(q.uptake)} <span class="pm">± ${fx(q.err)}</span>`; };
  const rows = [
    ["sec", "GCMC uptake, this model (mol/kg, 298 K)"],
    ["0.05 bar", upt(0.05)], ["0.15 bar (flue gas)", upt(0.15)], ["1 bar", upt(1)], ["5 bar", upt(5)],
    ["0.15 → 1 bar swing", (n) => { const a = at(D(n), 0.15).uptake, b = at(D(n), 1).uptake; return a == null || b == null ? null : fx(b - a); }],
    ["Screening runs, 1 bar", (n) => { const b = bestMeas(n); return b ? `${tierTag(b.tier)} ${fx(b.uptake)} <span class="pm">± ${fx(b.err)}</span>` : null; }],
    ["lit", "Experiment, ~1 bar (literature)", (n) => (LIT[n] ? `<span title="${esc(LIT[n][1])}">${LIT[n][0]}</span>` : null)],
    ["sec", "Binding pockets (CO₂-oxygen probe)"],
    ["Strong-binding pore <small>&lt; −10 kJ/mol</small>", (n) => (D(n).pockets ? `${Math.round(100 * D(n).pockets.strong_fraction)}%` : null)],
    ["Deepest well <small>kJ/mol</small>", (n) => (D(n).pockets ? fx(D(n).pockets.min_kjmol, 1) : null)],
    ["5th percentile <small>kJ/mol</small>", (n) => (D(n).pockets ? fx(D(n).pockets.p05_kjmol, 1) : null)],
    ["Median <small>kJ/mol</small>", (n) => (D(n).pockets ? fx(D(n).pockets.median_kjmol, 1) : null)],
    ["sec", "Structure (tier-0 descriptors)"],
    ["PLD / LCD <small>Å</small>", (n) => (P(n).PLD != null ? `${fx(P(n).PLD, 1)} / ${fx(P(n).LCD, 1)}` : null)],
    ["Void fraction", (n) => (P(n).VF != null ? fx(P(n).VF) : null)],
    ["Density <small>g/cm³</small>", (n) => (P(n).density != null ? fx(P(n).density) : null)],
    ["Metals", (n) => ((P(n).metals || D(n).metals || []).join(" ") || null)],
    ["Open metal sites", (n) => (P(n).oms == null ? null : P(n).oms ? "yes" : "no")],
  ];
  const th = cols.map((n) => `<th class="n ${n === S.sel ? "me" : ""}">${esc(label(n))}<br><span class="dim" style="font-weight:400">${n === S.sel ? "selected" : REFS.includes(n) ? "reference" : "typical"}</span></th>`).join("");
  $("cmp").innerHTML = `<thead><tr><th></th>${th}</tr></thead><tbody>` + rows.map((r) => {
    if (r[0] === "sec") return `<tr class="sec"><td colspan="${cols.length + 1}">${r[1]}</td></tr>`;
    const [lab, f, cls] = r[0] === "lit" ? [r[1], r[2], "lit"] : [r[0], r[1], ""];
    return `<tr class="${cls}"><td class="m">${lab}</td>${cols.map((n) => `<td class="n ${n === S.sel ? "me" : ""}">${cell(n, f)}</td>`).join("")}</tr>`;
  }).join("") + `</tbody>`;
}

function measTable() {
  const m = S.meas[S.sel];
  if (!m) { $("meas").innerHTML = `<div class="skel" style="height:40px"></div>`; return; }
  if (!m.rows.length) { $("meas").innerHTML = `<p class="note">No screening run has measured this structure by GCMC. The isotherm on this page comes from the separate science run.</p>`; return; }
  $("meas").innerHTML = `<div class="tbl-wrap"><table class="t"><thead><tr><th>run</th><th>tier</th><th class="n">uptake mol/kg ± 1σ</th><th class="n">CPU-s</th><th>mode</th></tr></thead><tbody>`
    + m.rows.map((r) => `<tr><td class="mono" style="font-size:12px">${esc(r.run_id)} <span class="dim">${esc(r.arm)}</span></td><td>${tierTag(r.tier)}</td><td class="n">${fx(r.uptake)} <span class="pm">± ${fx(r.err)}</span></td><td class="n">${fx(r.cpu_s, 0)}</td><td class="dim">${esc(r.mode)}</td></tr>`).join("")
    + `</tbody></table></div><p class="note" style="margin:8px 0 0">${tierTag(1)} values are unconverged and only rank structures; ${tierTag(2)} values are the confirmed ones.</p>`;
}

// ------------------------------------------------------------------------------ peer review
async function loadReview(name) {
  const box = $("review");
  box.innerHTML = `<div class="skel" style="height:56px"></div>`;
  for (;;) {
    if (S.sel !== name) return;
    let r;
    try { r = await getJSON(`/api/review?name=${encodeURIComponent(name)}`); } catch (e) { r = { status: "neterror", error: e.message }; }
    if (S.sel !== name) return;
    if (r.status === "ready") { renderReview(r); return; }
    if (r.status === "error") { box.innerHTML = `<p class="note">The review could not be produced: ${esc(r.error)}</p>`; return; }
    box.innerHTML = `<p class="note"><span class="spin"></span> ${r.status === "waiting" ? "The reviewer starts once the isotherm and pockets are computed." : r.status === "neterror" ? "Server did not answer; retrying…" : "The reviewer and its sub-reviewers are checking each claim against the data…"}</p>`;
    if (SNAP) return;
    await sleep(6000);
  }
}

function renderReview(r) {
  const t = r.tally || {}, tot = (t.supported || 0) + (t.partly || 0) + (t.refuted || 0) + (t.unverifiable || 0) || 1;
  const C = { supported: "var(--green)", partly: "var(--amber)", refuted: "var(--red)", unverifiable: "#C9CED6" };
  const bar = Object.keys(C).map((k) => `<i style="width:${(100 * (t[k] || 0)) / tot}%;background:${C[k]}" title="${t[k] || 0} ${k}"></i>`).join("");
  const leg = Object.keys(C).map((k) => `<span style="--c:${C[k]}">${t[k] || 0} ${k}</span>`).join("");
  let anyG = false;
  const ground = (g) => {
    if (!g || !g.length) return "";
    anyG = true;
    const used = g.filter((x) => x.used).length;
    return `<details class="grounding"><summary><span class="gchip">grounded on Nemotron-Science-v1 · <b>${used}</b>/${g.length} used</span></summary>`
      + g.slice().sort((a, b) => b.used - a.used || b.score - a.score).map((x) => `<div class="gsrc ${x.used ? "used" : ""}">
          <div class="cit">${esc(x.citation)}${x.used ? `<span class="u">used by the sub-reviewer</span>` : `<span class="sc">retrieved, not used</span>`}<span class="sc">score ${fx(x.score, 1)}</span></div>
          <div class="q"><b>Q:</b> ${esc(String(x.question || "").slice(0, 260))}${String(x.question || "").length > 260 ? "…" : ""}</div>
          ${x.excerpt ? `<div class="x">${esc(String(x.excerpt).slice(0, 360))}</div>` : ""}
          ${x.answer ? `<div class="a"><b>A:</b> ${esc(String(x.answer).slice(0, 300))}</div>` : ""}</div>`).join("") + `</details>`;
  };
  const node = (n, depth) => {
    const kids = n.children || [];
    return `<div class="claim ${esc(n.verdict)}"><div class="c"><span class="vd ${esc(n.verdict)}">${esc(String(n.verdict).toUpperCase())}</span>${esc(n.claim)}</div>
      <div class="e">${esc(n.evidence)}</div>${ground(n.grounding)}
      ${kids.length ? `<details ${depth === 0 && n.verdict !== "supported" ? "open" : ""}><summary>${kids.length} sub-claim${kids.length > 1 ? "s" : ""} checked separately</summary>${kids.map((k) => node(k, depth + 1)).join("")}</details>` : ""}</div>`;
  };
  const claims = (r.claims || []).map((c) => node(c, 0)).join("");
  $("review").innerHTML = `<div class="rv-sum"><div class="tally">${bar}</div><div class="tally-l">${leg}</div>
      <p style="margin:8px 0 0">${esc(r.assessment)}</p>
      ${r.would_change_our_mind ? `<p class="note" style="margin:6px 0 0"><b>What would change the verdict:</b> ${esc(r.would_change_our_mind)}</p>` : ""}</div>
    <div class="claims">${claims || `<p class="note">No claims were reviewed.</p>`}</div>
    ${anyG ? `<p class="gcap">Background reasoning from NVIDIA Nemotron-Science-v1 (synthetic, CC-BY-4.0): used to judge physical plausibility, never as evidence for this structure's numbers.</p>` : ""}
    ${r.models ? `<p class="gcap">Reviewer ${esc(r.models.lead)}; sub-reviewers ${esc(r.models.sub)}.</p>` : ""}`;
}

// ---------------------------------------------------------------------------------- search
function initSearch() {
  const q = $("q"), list = $("results");
  let timer = null, items = [], cur = -1, seq = 0;
  const close = () => { list.hidden = true; cur = -1; };
  const paint = () => list.querySelectorAll("li[data-n]").forEach((li, i) => li.setAttribute("aria-selected", i === cur));
  const run = async () => {
    const term = q.value.trim(), my = ++seq;
    if (!term) { close(); return; }
    try { items = await getJSON(`/api/search?q=${encodeURIComponent(term)}&limit=25`); } catch (_) { items = []; }
    if (my !== seq) return;
    list.hidden = false; cur = items.length ? 0 : -1;
    list.innerHTML = items.length ? items.map((it) => `<li role="option" data-n="${esc(it.name)}"><b>${esc(label(it.name))}</b><span class="r">${it.science ? "computed" : "compute on open"}</span>
        <span class="d">PLD ${fx(it.PLD, 1)} · LCD ${fx(it.LCD, 1)} Å · VF ${fx(it.VF)} · ρ ${fx(it.density)} · ${esc((it.metals || []).join(" ") || "no metal")}${it.oms ? " · OMS" : ""}</span></li>`).join("")
      : `<li class="none">No pool structure matches “${esc(term)}”.</li>`;
    paint();
  };
  q.oninput = () => { clearTimeout(timer); timer = setTimeout(run, 180); };
  q.onkeydown = (e) => {
    if (e.key === "ArrowDown" && items.length) { cur = Math.min(items.length - 1, cur + 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp" && items.length) { cur = Math.max(0, cur - 1); paint(); e.preventDefault(); }
    else if (e.key === "Enter" && cur >= 0 && items[cur]) { choose(items[cur].name); }
    else if (e.key === "Escape") close();
  };
  list.onclick = (e) => { const li = e.target.closest("li[data-n]"); if (li) choose(li.dataset.n); };
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) close(); });
  const choose = (n) => { close(); q.value = ""; q.blur(); select(n); window.scrollTo({ top: 0, behavior: "smooth" }); };
}

boot();
