"use strict";
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const f3 = (x) => (x == null ? "–" : Number(x).toFixed(3));
const name = (s) => (s || "").replace(/_clean$/, "");

async function boot() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  const want = params.get("run") || (cfg.runs.find((r) => r.arm === "sieve") || cfg.runs[0] || {}).run_id;
  const sel = $("run-select");
  for (const r of cfg.runs) sel.appendChild(new Option(`${r.run_id} · ${r.arm}`, r.run_id, false, r.run_id === want));
  sel.onchange = () => { params.set("run", sel.value); location.search = params.toString(); };
  $("n-agents").href = `/agents?run=${encodeURIComponent(want || "")}`;
  $("n-room").href = `/?run=${encodeURIComponent(want || "")}`;
  if (!want) { $("lead").textContent = "No runs yet."; return; }
  const d = await fetch(`/api/discoveries?run=${encodeURIComponent(want)}`).then((r) => r.json());
  const arm = d.run ? d.run.arm : "";
  $("lead").textContent = `${d.cards.length ? "The structures this run rated highest, best first" : "Nothing measured yet"}. `
    + (d.explained ? "Write-ups are by the analyst agent (Nemotron Super), grounded only in the run's own data. " : `No analyst write-ups for this run yet (python -m sieve.agents.explain <db> ${want}). `)
    + "Uptake is simulated CO₂ loading at 298 K, 1 bar without framework charges: a ranking signal, not an experimental prediction.";
  $("cards").innerHTML = d.cards.map((c, i) => card(c, i)).join("");
  // the tour simulates CO2 positions on first view; start with the top card, others on demand
  d.cards.forEach((c, i) => {
    const host = document.getElementById(`mv-${i}`);
    const info = { features: c.features, key_features: (c.note || {}).key_features, uptake: c.estimate.uptake };
    if (i === 0) new Tour(host, c.structure, info);
    else { host.innerHTML = `<button class="tour-start">Show the 3D tour: framework, pore, CO₂ inside</button>`; host.firstChild.onclick = () => new Tour(host, c.structure, info); }
  });
}

function card(c, i) {
  const n = c.note || {}, e = c.estimate;
  const tags = [c.recommended ? `<span class="tag rec">RECOMMENDED</span>` : "",
    e.tier === 2 ? `<span class="tag ok">TIER-2 CONFIRMED</span>` : `<span class="tag">TIER 1 ONLY</span>`,
    c.critic_vetoes.length ? (e.tier === 2 ? `<span class="tag">critic veto lifted by tier 2</span>` : `<span class="tag veto">CRITIC VETO: ${esc(c.critic_vetoes[0]).slice(0, 40)}</span>`) : ""].join("");
  const bars = Object.values(c.features).map((f) => `<div class="bar-row"><span>${esc(f.label)}</span><span class="v">${f.value}</span>
      <div class="track" title="${f.percentile}th percentile of the pool"><i style="left:${f.percentile}%"></i></div></div>`).join("");
  const meas = c.measurements.map((m) => `<div>T${m.tier} ${f3(m.uptake)} ± ${f3(m.err)} mol/kg · ${m.cpu_s}s CPU · ${esc(m.mode)}</div>`).join("");
  const why = c.picked.map((p) => `round ${p.round} (tier ${p.tier}): ${esc(p.why || "–")}`).join("<br>");
  return `<div class="card ${c.recommended ? "rec" : ""}">
    <div><div class="mv" id="mv-${i}"></div></div>
    <div>
      <div class="rank">#${c.rank} IN THIS RUN</div>
      <div class="name">${esc(name(c.structure))}</div>
      <div>${tags}</div>
      ${n.headline ? `<div class="head">${esc(n.headline)}</div>` : ""}
      ${n.why_it_matters ? `<div class="why">${esc(n.why_it_matters)}</div>` : `<div class="muted">No write-up yet.</div>`}
      ${n.key_features && n.key_features.length ? `<ul class="feat">${n.key_features.map((k) => `<li>${esc(String(k).replace(/^\s*[-•*]\s*/, ""))}</li>`).join("")}</ul>` : ""}
      ${n.confidence ? `<div class="conf"><b>Confidence.</b> ${esc(n.confidence)}</div>` : ""}
      ${n.caveats ? `<div class="cav">${esc(n.caveats)}</div>` : ""}
    </div>
    <div>
      <div class="name" style="font-size:24px">${f3(e.uptake)} <span class="muted">± ${f3(e.err)} mol/kg</span></div>
      ${c.true_value != null ? `<div class="truth">true (independent tier-2): ${f3(c.true_value)}</div>` : ""}
      <div class="bars" style="margin-top:10px">${bars}
        <div class="bar-row"><span>open metal sites</span><span class="v">${c.open_metal_sites ? "yes" : "no"}</span></div>
        <div class="bar-row"><span>metals</span><span class="v">${esc((c.metals || []).join(" ") || "–")}</span></div>
        <div class="bar-row"><span>density</span><span class="v">${c.density_g_cm3 ?? "–"} g/cm³</span></div></div>
      <div class="meas">${meas}</div>
      ${why ? `<div class="who"><b>Why it was picked.</b><br>${why}</div>` : ""}
    </div></div>`;
}

async function viewer(id, s) {
  if (!window.$3Dmol) return;
  try {
    const P = await fetch(`/api/cif?name=${encodeURIComponent(s)}`).then((r) => r.json());
    if (P.error) return;
    const v = $3Dmol.createViewer(document.getElementById(id), { backgroundColor: "white" });
    const m = v.addModel(P.cif, "cif", { doAssembly: true, duplicateAssemblyAtoms: true, normalizeAssembly: true });
    v.setStyle({}, { stick: { radius: 0.14, colorscheme: "Jmol" }, sphere: { scale: 0.24, colorscheme: "Jmol" } });
    v.addUnitCell(m, { box: { color: "#9097A3" }, alabel: "", blabel: "", clabel: "" });
    for (const p of P.void) v.addSphere({ center: { x: p[0], y: p[1], z: p[2] }, radius: 0.9, color: "#0B8A98", opacity: 0.12 });
    v.resize(); v.zoomTo(); v.zoom(1.1); v.render(); if (!params.get("snapshot")) v.spin("y", 0.3);
    window.addEventListener("resize", () => { v.resize(); v.render(); });
  } catch (_) { document.getElementById(id).innerHTML = '<div class="muted" style="padding:12px">3D view needs WebGL.</div>'; }
}
boot();
