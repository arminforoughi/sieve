"use strict";
// A guided 3D tour of one structure: framework -> the pore -> CO2 moving in -> why it works.
// Uses 3Dmol.js and /api/viz (real CO2 positions from a short GCMC run with movie output).
const TOUR_STEPS = ["The framework", "The pore", "CO₂ inside", "Why it captures CO₂"];
const TOUR_MS = 6500;

class Tour {
  constructor(host, name, info = {}) {
    this.host = host; this.name = name; this.info = info; this.step = 0; this.playing = true;
    host.classList.add("tour"); host.__tour = this;
    host.innerHTML = `<div class="tour-gl"></div>
      <div class="tour-cap"><div class="tour-title"></div><div class="tour-text"></div></div>
      <div class="tour-ctl">${TOUR_STEPS.map((s, i) => `<button data-i="${i}" title="${s}">${i + 1}</button>`).join("")}<button data-p="1" title="pause">❚❚</button></div>
      <div class="tour-wait">Simulating where CO₂ sits… (short GCMC run, about a minute)</div>`;
    host.querySelector(".tour-ctl").onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.p) { this.playing = !this.playing; b.textContent = this.playing ? "❚❚" : "▶"; return; }
      this.go(+b.dataset.i); this.playing = false; host.querySelector("[data-p]").textContent = "▶";
    };
    this.load();
  }
  async load() {
    let d;
    for (;;) {
      d = await fetch(`/api/viz?name=${encodeURIComponent(this.name)}`).then((r) => r.json());
      if (d.status !== "computing") break;
      await new Promise((r) => setTimeout(r, 5000));
    }
    if (d.status !== "ready") { this.host.querySelector(".tour-wait").textContent = `Could not simulate CO₂ positions: ${d.error || d.status}`; return; }
    this.host.querySelector(".tour-wait").remove();
    this.d = d;
    try {
      const v = this.v = $3Dmol.createViewer(this.host.querySelector(".tour-gl"), { backgroundColor: "white" });
      this.m = v.addModel(d.cif, "cif", { doAssembly: true, duplicateAssemblyAtoms: true, normalizeAssembly: true });
      v.addUnitCell(this.m, { box: { color: "#C3C8D0" }, alabel: "", blabel: "", clabel: "" });
      v.resize(); v.zoomTo(); v.zoom(1.05);
      new ResizeObserver(() => { v.resize(); v.render(); }).observe(this.host);
    } catch (_) { this.host.querySelector(".tour-cap").textContent = "3D view needs WebGL."; return; }
    this.go(0);
    this.timer = setInterval(() => { if (this.playing && !document.hidden) this.go((this.step + 1) % TOUR_STEPS.length); }, TOUR_MS);
  }
  frameworkStyle(faint) {
    const v = this.v, metals = this.d.metals || [];
    v.setStyle({ model: this.m }, faint ? { stick: { radius: 0.09, color: "#B9BEC7" } } : { stick: { radius: 0.14, colorscheme: "Jmol" }, sphere: { scale: 0.22, colorscheme: "Jmol" } });
    if (metals.length) v.setStyle({ model: this.m, elem: metals }, { stick: { radius: 0.14, colorscheme: "Jmol" }, sphere: { scale: faint ? 0.5 : 0.62, colorscheme: "Jmol" } });
  }
  clear() {
    clearInterval(this.anim); this.anim = null;
    this.v.removeAllShapes(); this.v.removeAllLabels();
    if (this.co2) { this.v.removeModel(this.co2); this.co2 = null; }
  }
  caption(title, text) {
    this.host.querySelector(".tour-title").textContent = title;
    this.host.querySelector(".tour-text").innerHTML = text;
    this.host.querySelectorAll(".tour-ctl button[data-i]").forEach((b) => b.classList.toggle("on", +b.dataset.i === this.step));
  }
  metalLabel() {
    const metals = this.d.metals || []; if (!metals.length) return;
    const a = this.v.selectedAtoms({ model: this.m, elem: metals[0] })[0]; if (!a) return;
    this.v.addLabel(`${metals.join("/")} node`, { position: a, backgroundColor: "#15171C", fontColor: "white", fontSize: 12, backgroundOpacity: 0.8 });
  }
  cavity(alpha = 0.28, grow = true) {
    const c = this.d.cavity, f = this.info.features || {};
    let r = grow ? 0.5 : c.radius;
    const add = () => { this.v.removeAllShapes(); this.v.addSphere({ center: { x: c.center[0], y: c.center[1], z: c.center[2] }, radius: r, color: "#0B8A98", opacity: alpha }); this.v.render(); };
    add();
    if (grow) { const t = setInterval(() => { r = Math.min(c.radius, r + c.radius / 12); add(); if (r >= c.radius) clearInterval(t); }, 60); }
    const lcd = f.LCD;
    this.v.addLabel(lcd ? `largest cavity ${lcd.value} Å · ${lcd.percentile}th pct` : "largest cavity", { position: { x: c.center[0], y: c.center[1] + c.radius, z: c.center[2] }, backgroundColor: "#0B8A98", fontColor: "white", fontSize: 12 });
  }
  playCO2() {
    const frames = this.d.frames || []; if (!frames.length) return;
    let i = 0;
    const show = () => {
      const mols = frames[i % frames.length]; i++;
      const xyz = `${mols.length * 3}\nco2\n` + mols.map((m) => `O ${m[0].join(" ")}\nC ${m[1].join(" ")}\nO ${m[2].join(" ")}`).join("\n");
      if (this.co2) this.v.removeModel(this.co2);
      this.co2 = this.v.addModel(xyz, "xyz");
      this.v.setStyle({ model: this.co2 }, { stick: { radius: 0.2, colorscheme: "Jmol" }, sphere: { scale: 0.42, colorscheme: "Jmol" } });
      this.v.render();
    };
    show(); this.anim = setInterval(show, 260);
  }
  go(step) {
    if (!this.v) return;
    this.step = step; this.clear();
    const d = this.d, f = this.info.features || {}, up = this.info.uptake ?? d.uptake_molkg;
    const pct = (k, u = "") => (f[k] ? `${(+f[k].value).toFixed(f[k].value > 10 ? 1 : 2)}${u} (${f[k].percentile}th percentile)` : "–");
    if (step === 0) {
      this.frameworkStyle(false); this.metalLabel();
      this.caption(TOUR_STEPS[0], `${(d.metals || []).join(", ") || "Metal"} nodes joined by organic linkers into a rigid, porous crystal${d.core && d.core.Has_OMS && d.core.Has_OMS.startsWith("Y") ? ", with open metal sites" : ""}. Large spheres: metal atoms.`);
    } else if (step === 1) {
      this.frameworkStyle(true); this.cavity();
      this.caption(TOUR_STEPS[1], `The empty space CO₂ can enter. Largest cavity ${pct("LCD", " Å")}; pore-limiting window ${pct("PLD", " Å")}; void fraction ${pct("AV_VF")}.`);
    } else if (step === 2) {
      this.frameworkStyle(true); this.playCO2();
      this.caption(TOUR_STEPS[2], `Real GCMC snapshots at 298 K, 1 bar: about <b>${d.molecules_per_cell}</b> CO₂ molecules per unit cell, cycling through the pore and crowding the walls. That loading is <b>${(+up).toFixed(2)} mol/kg</b>.`);
    } else {
      this.frameworkStyle(false); this.cavity(0.12, false); this.playCO2();
      const kf = (this.info.key_features || []).map((x) => `• ${String(x).replace(/^\s*[-•*]\s*/, "")}`).join("<br>");
      this.caption(TOUR_STEPS[3], kf || `Wide, highly open pores (void ${pct("AV_VF")}) give CO₂ room, and walls close enough to overlap their van der Waals pull hold it. No framework charges in this model, so geometry is the whole story.`);
    }
    this.v.render();
  }
}
