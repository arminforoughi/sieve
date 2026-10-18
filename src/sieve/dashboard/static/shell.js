"use strict";
// One top bar for every page: brand, nav with the active page, and (where a page is about one run)
// the run selector. Pages put <header class="shell" data-page="science" [data-runs]></header> first
// in <body> and load this script right after it; it renders synchronously so page scripts can use
// #run-select and #replay straight away. Shell.setRun(id) keeps ?run= on every nav link.
(function () {
  const PAGES = [
    ["workflow", "/how", "How it works"],
    ["room", "/", "Control room"],
    ["agents", "/agents", "Agents"],
    ["discoveries", "/discoveries", "Discoveries"],
    ["science", "/science", "Science"],
    ["compare", "/compare", "Compare"],
    ["guide", "/guide", "Methods"],
  ];
  const RUN_PAGES = new Set(["workflow", "room", "agents", "discoveries"]);
  const host = document.querySelector("header.shell");
  if (!host) return;
  const page = host.dataset.page;
  const params = new URLSearchParams(location.search);
  let run = params.get("run") || "";
  try { if (!run) run = localStorage.getItem("sieve.run") || ""; } catch (_) { /* storage blocked */ }
  host.innerHTML = `<a class="brand" href="/">SIEVE<small>CO₂ MOF screen</small></a>
    <nav aria-label="Pages">${PAGES.map(([k, href, label]) =>
      `<a data-k="${k}" href="${href}"${k === page ? ' class="on" aria-current="page"' : ""}>${label}</a>`).join("")}</nav>
    <div class="right">${host.hasAttribute("data-runs")
      ? `<span class="badge" id="replay" hidden>REPLAY 20&times;</span><label for="run-select">Run</label><select id="run-select" aria-label="Run"></select>`
      : `<span id="replay" hidden></span>`}</div>`;
  function setRun(id) {
    run = id || "";
    try { if (run) localStorage.setItem("sieve.run", run); } catch (_) { /* storage blocked */ }
    host.querySelectorAll("nav a").forEach((a) => {
      const k = a.dataset.k, base = PAGES.find((p) => p[0] === k)[1];
      a.href = RUN_PAGES.has(k) && run ? `${base}?run=${encodeURIComponent(run)}` : base;
    });
  }
  setRun(run);
  // badges shared by every page: material family (zeolite; MOF only where the run mixes both),
  // disorder flag (fraction of atoms in clashing pairs) and tier tags incl. tier 3 (capture check)
  const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function fam(nameOrFlags, showMof) {
    const z = typeof nameOrFlags === "string" ? nameOrFlags.startsWith("IZA-") : (nameOrFlags || {}).fam === "zeolite";
    return z ? `<span class="fam" title="IZA zeolite: pure-silica framework, no cations; compared on geometry only">zeolite</span>`
      : showMof ? `<span class="fam mof" title="CoRE MOF 2019">MOF</span>` : "";
  }
  function dis(frac) {
    return frac ? `<span class="dis" title="disordered: ${esc((100 * frac).toFixed(1))}% of atoms sit in pairs closer than any bond (doubled sites in the CIF)">disordered</span>` : "";
  }
  const tier = (t) => `<span class="tier t${t}"${t === 3 ? ' title="tier 3: capture check (15:85 CO2/N2 mixture + 0.01 bar desorption)"' : ""}>T${t}</span>`;
  window.Shell = { setRun, get run() { return run; }, fam, dis, tier, esc };
})();
