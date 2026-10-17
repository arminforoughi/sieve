"use strict";
// Discoveries: the measured leaderboard (sortable, CSV) + dossier cards for the run's top structures.
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fx = (x, k = 2) => (x == null ? "–" : Number(x).toFixed(k));
const short = (s) => (s || "").replace(/_clean$/, "");
const tierTag = (t) => (t === 2 ? `<span class="tier t2" title="tier 2: 400+2000 cycles, confirmed">T2</span>`
  : `<span class="tier t1" title="tier 1: 100+500 cycles, not converged; rank only">T1</span>`);
const sciURL = (s) => `/science?name=${encodeURIComponent(s)}`;
const S = { run: null, scope: "run", rows: [], sort: { k: "uptake", dir: -1 }, runs: [] };

const COLS = [
  { k: "rank", t: "#", n: true, nosort: true },
  { k: "structure", t: "structure" },
  { k: "uptake", t: "uptake mol/kg ± 1σ", n: true },
  { k: "tier", t: "tier", n: true },
  { k: "t1", t: "T1 mean", n: true },
  { k: "t2", t: "T2", n: true },
  { k: "PLD", t: "PLD Å", n: true },
  { k: "LCD", t: "LCD Å", n: true },
  { k: "VF", t: "void frac.", n: true },
  { k: "density", t: "ρ g/cm³", n: true },
  { k: "metals", t: "metals" },
  { k: "oms", t: "OMS" },
  { k: "run", t: "run", all: true },
];

async function boot() {
  const [cfg, summary] = await Promise.all([fetch("/api/config").then((r) => r.json()),
    fetch("/api/runs/summary").then((r) => r.json()).catch(() => [])]);
  S.runs = summary;
  const has = (id) => summary.some((r) => r.run_id === id && r.n_t1 + r.n_t2 > 0);
  // the requested run, else the last one viewed, else the newest SIEVE run with measurements, else any run with data
  const want = [params.get("run"), window.Shell && Shell.run].find((r) => r && cfg.runs.some((x) => x.run_id === r))
    || (summary.find((r) => r.arm === "sieve" && has(r.run_id)) || summary.find((r) => has(r.run_id)) || cfg.runs[0] || {}).run_id;
  const sel = $("run-select");
  for (const r of cfg.runs) sel.appendChild(new Option(`${r.run_id} · ${r.arm}`, r.run_id, false, r.run_id === want));
  sel.onchange = () => { params.set("run", sel.value); location.search = params.toString(); };
  $("replay").hidden = !cfg.replay;
  S.run = want;
  if (window.Shell) Shell.setRun(want);
  if (!want) { $("summary").innerHTML = `<div class="callout">No runs in the database yet. Start one with <code>python -m sieve sieve</code>.</div>`; $("cards").innerHTML = ""; return; }
  $("scope").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.scope = b.dataset.s;
    document.querySelectorAll("#scope button").forEach((x) => x.classList.toggle("on", x === b)); loadBoard(); };
  $("filter").oninput = renderBoard; $("only-t2").onchange = renderBoard;
  renderSummary();
  loadBoard();
  loadCards();
}

function renderSummary() {
  const r = S.runs.find((x) => x.run_id === S.run);
  if (!r) { $("summary").innerHTML = ""; return; }
  const all = S.runs.filter((x) => x.best_t2 || x.best_t1);
  const top2 = all.filter((x) => x.best_t2).sort((a, b) => b.best_t2.uptake - a.best_t2.uptake)[0];
  const stat = (k, v, s) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  const b2 = r.best_t2, b1 = r.best_t1;
  let html = stat("Status", esc(r.status), `${esc(r.arm)} arm · ${fx(r.spent_cpu_s / 3600)} of ${fx(r.budget_cpu_s / 3600)} CPU-h spent`)
    + stat("Measured by GCMC", `${r.n_t1} <small>T1</small> · ${r.n_t2} <small>T2</small>`, "distinct structures per tier")
    + stat(`Best confirmed ${tierTag(2)}`, b2 ? `${fx(b2.uptake)} <small>± ${fx(b2.err)}</small>` : "–", b2 ? `<a href="${sciURL(b2.structure)}">${esc(short(b2.structure))}</a>` : "no tier-2 confirmation yet: no winner can be named")
    + stat(`Best not yet confirmed ${tierTag(1)}`, b1 ? `${fx(b1.uptake)} <small>± ${fx(b1.err)}</small>` : "–", b1 ? `<a href="${sciURL(b1.structure)}">${esc(short(b1.structure))}</a> · unconverged, rank only` : "–");
  if (top2 && b2 && top2.run_id !== r.run_id && top2.best_t2.uptake > b2.uptake + b2.err)
    html += `<div class="callout bad"><b>Not the best in the database.</b> ${esc(top2.run_id)} (${esc(top2.arm)}) confirmed <b>${esc(short(top2.best_t2.structure))}</b> at ${fx(top2.best_t2.uptake)} ± ${fx(top2.best_t2.err)} mol/kg, above this run's best confirmed ${esc(short(b2.structure))} at ${fx(b2.uptake)}. See <a href="/compare">Compare</a>.</div>`;
  if (r.status !== "finished") html += `<div class="callout info">This run is still in progress (${esc(r.status)}); the ranking will change as more structures are measured.</div>`;
  $("summary").innerHTML = html;
}

async function loadBoard() {
  $("lb").innerHTML = `<tbody><tr><td><span class="spin"></span> Loading measurements…</td></tr></tbody>`;
  try {
    const d = await fetch(`/api/leaderboard?run=${encodeURIComponent(S.scope === "all" ? "*" : S.run)}`).then((r) => r.json());
    S.rows = d.rows || [];
  } catch (e) { $("lb").innerHTML = `<tbody><tr><td class="note">Could not load the leaderboard: ${esc(e.message)}</td></tr></tbody>`; return; }
  $("lb-sub").textContent = S.scope === "all" ? "· all runs, each structure at its highest tier" : `· ${S.run}`;
  renderBoard();
}

function visibleRows() {
  const q = $("filter").value.trim().toLowerCase(), t2 = $("only-t2").checked;
  const { k, dir } = S.sort;
  const val = (r) => (k === "metals" ? (r.metals || []).join(" ") : k === "oms" ? (r.oms ? 1 : 0) : r[k]);
  return S.rows.filter((r) => (!t2 || r.tier === 2) && (!q || r.structure.toLowerCase().includes(q) || (r.metals || []).join(" ").toLowerCase().includes(q)))
    .sort((a, b) => { const x = val(a), y = val(b); if (x == null) return 1; if (y == null) return -1; return (x > y ? 1 : x < y ? -1 : 0) * dir; });
}

function renderBoard() {
  const cols = COLS.filter((c) => !c.all || S.scope === "all");
  const rows = visibleRows();
  const head = `<thead><tr>${cols.map((c) => `<th class="${c.n ? "n" : ""} ${c.nosort ? "" : "sort"}" data-k="${c.k}" ${S.sort.k === c.k ? `aria-sort="${S.sort.dir > 0 ? "ascending" : "descending"}"` : ""}>${c.t}</th>`).join("")}</tr></thead>`;
  const body = rows.length ? rows.map((r, i) => `<tr class="click" data-s="${esc(r.structure)}">
      <td class="rk">${i + 1}</td>
      <td class="name"><a href="${sciURL(r.structure)}">${esc(short(r.structure))}</a>${r.science ? `<span class="sci-dot" title="isotherm and binding pockets already computed"></span>` : ""}</td>
      <td class="n">${fx(r.uptake)} <span class="pm">± ${fx(r.err)}</span></td>
      <td class="n">${tierTag(r.tier)}</td>
      <td class="n">${fx(r.t1)}</td><td class="n">${fx(r.t2)}</td>
      <td class="n">${fx(r.PLD)}</td><td class="n">${fx(r.LCD)}</td><td class="n">${fx(r.VF)}</td><td class="n">${fx(r.density)}</td>
      <td class="metal">${esc((r.metals || []).join(" ") || "–")}</td><td>${r.oms ? "yes" : r.oms === false ? "no" : "–"}</td>
      ${S.scope === "all" ? `<td class="metal">${esc(r.run)}</td>` : ""}</tr>`).join("")
    : `<tr><td colspan="${cols.length}" class="note">${S.rows.length ? "No rows match the filter." : "This run has not measured any structure by GCMC yet (tier 0 descriptors only)."}</td></tr>`;
  $("lb").innerHTML = head + `<tbody>${body}</tbody>`;
  $("lb-count").textContent = `${rows.length} of ${S.rows.length} structures · ${S.rows.filter((r) => r.tier === 2).length} tier-2 confirmed`;
  $("lb").querySelectorAll("th.sort").forEach((th) => (th.onclick = () => {
    const k = th.dataset.k; S.sort = { k, dir: S.sort.k === k ? -S.sort.dir : (k === "structure" || k === "metals" || k === "run" ? 1 : -1) }; renderBoard();
  }));
  $("lb").querySelector("tbody").onclick = (e) => { if (e.target.closest("a")) return; const tr = e.target.closest("tr[data-s]"); if (tr) location.href = sciURL(tr.dataset.s); };
  const cs = cols.filter((c) => c.k !== "rank");
  const csv = [["rank", ...cs.map((c) => c.k === "uptake" ? "uptake_molkg,err_molkg" : c.k)].join(",")]
    .concat(rows.map((r, i) => [i + 1, ...cs.map((c) => c.k === "uptake" ? `${r.uptake},${r.err}` : c.k === "metals" ? (r.metals || []).join(" ") : r[c.k] ?? "")].join(",")));
  const a = $("csv");
  if (a.dataset.url) URL.revokeObjectURL(a.dataset.url);
  a.dataset.url = a.href = URL.createObjectURL(new Blob([csv.join("\n") + "\n"], { type: "text/csv" }));
  a.download = `sieve-leaderboard-${S.scope === "all" ? "all-runs" : S.run}.csv`;
}

// ---------------------------------------------------------------------------- dossier cards
async function loadCards() {
  let d;
  try { d = await fetch(`/api/discoveries?run=${encodeURIComponent(S.run)}`).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }); }
  catch (e) { $("cards").innerHTML = `<div class="card note">Could not load the dossiers: ${esc(e.message)}. The leaderboard above is unaffected.</div>`; return; }
  const running = d.run && d.run.status !== "finished";
  $("cards-note").textContent = d.explained ? "Write-ups by the analyst agent, grounded only in the run's own data."
    : running ? "Analyst write-ups are added when the run finishes; below is what the data already says."
      : `No analyst write-ups yet: python -m sieve.agents.explain data/runs.db ${S.run}`;
  if (!d.cards.length) { $("cards").innerHTML = `<div class="card note">Nothing measured by GCMC in this run yet.</div>`; return; }
  $("cards").innerHTML = d.cards.map(card).join("");
  d.cards.forEach((c, i) => {
    const btn = document.getElementById(`tour-${i}`);
    btn.onclick = () => {
      const host = document.getElementById(`mv-${i}`); host.hidden = false; btn.remove();
      new Tour(host, c.structure, { features: c.features, key_features: (c.note || {}).key_features, uptake: c.estimate.uptake });
    };
  });
}

function card(c, i) {
  const n = c.note || {}, e = c.estimate;
  const tags = [c.recommended ? `<span class="chip ok">RECOMMENDED</span>` : "", tierTag(e.tier),
    e.tier === 2 ? `<span class="chip ok">confirmed</span>` : `<span class="chip warn">unconverged · rank only</span>`,
    c.critic_vetoes.length ? (e.tier === 2 ? `<span class="chip">critic veto lifted by tier 2</span>` : `<span class="chip bad" title="${esc(c.critic_vetoes[0])}">critic veto</span>`) : "",
    c.open_metal_sites ? `<span class="chip">open metal sites</span>` : ""].join("");
  const bars = Object.values(c.features).map((f) => `<span>${esc(f.label)}</span><span class="v">${f.value}</span>
      <div class="track" title="${f.percentile}th percentile of the pool"><i style="left:${f.percentile}%"></i></div>`).join("");
  const meas = c.measurements.map((m) => `<div>T${m.tier} ${fx(m.uptake, 3)} ± ${fx(m.err, 3)} mol/kg · ${m.cpu_s} CPU-s · ${esc(m.mode)}</div>`).join("");
  const why = c.picked.map((p) => `round ${p.round} (T${p.tier}): ${esc(p.why || "–")}`).join("<br>");
  const write = n.why_it_matters
    ? `${n.headline ? `<div class="head">${esc(n.headline)}</div>` : ""}<div class="why">${esc(n.why_it_matters)}</div>
       ${n.key_features && n.key_features.length ? `<ul class="feat">${n.key_features.map((k) => `<li>${esc(String(k).replace(/^\s*[-•*]\s*/, ""))}</li>`).join("")}</ul>` : ""}
       ${n.caveats ? `<div class="note">${esc(n.caveats)}</div>` : ""}`
    : `<div class="pending">Analyst write-up not ready yet. The numbers below come straight from the run's measurements and the pool descriptors.</div>`;
  return `<article class="card dcard ${c.recommended ? "rec" : ""}">
    <div class="top"><span class="rank">#${c.rank}</span><span class="name">${esc(short(c.structure))}</span>
      <span class="val">${fx(e.uptake, 2)} <small>± ${fx(e.err, 2)} mol/kg</small></span></div>
    <div class="tags">${tags}</div>
    ${write}
    <div class="bars">${bars}
      <span>metals</span><span class="v">${esc((c.metals || []).join(" ") || "–")}</span>
      <span>density</span><span class="v">${c.density_g_cm3 ?? "–"}</span></div>
    ${c.true_value != null ? `<div class="meas">independent tier-2 re-measurement: ${fx(c.true_value, 3)} mol/kg</div>` : ""}
    <div class="meas">${meas}</div>
    ${why ? `<div class="who"><b>Why it was picked.</b><br>${why}</div>` : ""}
    <div class="mv" id="mv-${i}" hidden></div>
    <div class="actions"><a class="btn sm primary" href="${sciURL(c.structure)}">Isotherm, pockets &amp; review →</a>
      <button class="btn sm" id="tour-${i}">3D tour</button></div>
  </article>`;
}
boot();
