// Compare page: "What we found" and "Against standard CO2 capture materials" (/api/findings).
(function () {
  const ARM = { "sieve-main": "SIEVE", "random-main": "random", "descriptor-main": "descriptor", "uniform-main": "uniform" };
  const f = (x, d = 2) => (x == null ? "–" : Number(x).toFixed(d));
  const pm = (v, d = 2) => (v && v[0] != null ? `${f(v[0], d)} <span class="dim">± ${f(v[1], d)}</span>` : '<span class="dim">–</span>');
  const short = (s) => s.replace(/_clean$/, "");

  function disorderChip(d) {
    if (!d) return '<span class="dim">not scanned</span>';
    if (!d.disordered) return '<span class="chip ok">clean</span>';
    const k = Object.entries(d.kinds || {}).map(([a, n]) => `${n}× ${a}`).join(", ");
    return `<span class="chip bad" title="closest pair ${f(d.min_distance_A)} Å">disordered</span> <span class="dim">${k}</span>`;
  }

  function leads(L) {
    const rows = L.rows || [];
    const head = `<tr><th>#</th><th>structure</th><th>found by (arm rank, own claim)</th><th class="num">referee, 1 bar (mol/kg)</th><th>crystal file</th><th class="num">resolved re-measure</th></tr>`;
    const body = rows.map((r, i) => {
      const by = r.by.map((b) => `${ARM[b.run] || b.run} #${b.rank} <span class="dim">(${f(b.claimed)}<sup>T${b.tier}</sup>)</span>`).join(" · ");
      const res = (L.resolved || {})[r.structure];
      const sieve = r.by.some((b) => b.run === "sieve-main");
      return `<tr class="${sieve ? "lead" : ""}"><td>${i + 1}</td><td class="mono"><a href="/science?name=${encodeURIComponent(r.structure)}">${short(r.structure)}</a></td>
        <td>${by}</td><td class="num mono">${pm(r.referee)}</td><td>${disorderChip(r.disorder)}</td>
        <td class="num mono">${res ? pm(res.uptake1) : r.disorder && r.disorder.disordered ? '<span class="dim">pending</span>' : '<span class="dim">n/a</span>'}</td></tr>`;
    }).join("");
    document.getElementById("fnd-leads").innerHTML = `<thead>${head}</thead><tbody>${body}</tbody>`;

    const top = rows[0], firstClean = rows.find((r) => r.disorder && !r.disorder.disordered);
    const nDis = rows.slice(0, 5).filter((r) => r.disorder && r.disorder.disordered).length;
    const sievePick = rows.find((r) => r.by.some((b) => b.run === "sieve-main" && b.rank === 1));
    const cards = [];
    if (top) cards.push(`<div class="callout bad"><b>Highest uptake: ${short(top.structure)}</b>, ${f(top.referee[0])} mol/kg, found by ${top.by.map((b) => ARM[b.run]).join(" + ")}, not by SIEVE. ${top.disorder && top.disorder.disordered ? "Its crystal file is disordered." : ""}</div>`);
    if (nDis) cards.push(`<div class="callout info"><b>${nDis} of the top 5</b> leads have disordered crystal files (atoms recorded twice). Their values may be inflated until re-measured on resolved copies.</div>`);
    if (firstClean) cards.push(`<div class="callout info"><b>Best clean structure: ${short(firstClean.structure)}</b>, ${f(firstClean.referee[0])} mol/kg${sievePick && sievePick.structure === firstClean.structure ? ", SIEVE's own pick, confirmed by the referee" : ""}.</div>`);
    document.getElementById("fnd-sum").innerHTML = cards.join("");
  }

  function same(rows, model) {
    document.getElementById("fnd-model").textContent = model;
    const head = `<tr><th>structure</th><th class="num">0.15 bar · flue gas</th><th class="num">1 bar</th><th class="num">CO₂/N₂ selectivity</th><th class="num">working capacity, VSA</th><th class="num">working capacity, TSA</th><th class="num">heat of adsorption (kJ/mol)</th></tr>`;
    const body = rows.map((r) => `<tr class="${r.role.startsWith("lead") ? "lead" : ""}">
      <td class="lbl2"><a href="/science?name=${encodeURIComponent(r.name)}">${r.label}</a><span class="role">${r.role}</span></td>
      <td class="num mono">${pm(r.p015)}</td><td class="num mono">${pm(r.p1)}</td><td class="num mono">${pm(r.selectivity, 1)}</td>
      <td class="num mono">${pm(r.wc_vsa)}</td><td class="num mono">${pm(r.wc_tsa)}</td><td class="num mono">${pm(r.qst, 1)}</td></tr>`).join("");
    document.getElementById("fnd-same").innerHTML = `<thead>${head}</thead><tbody>${body}</tbody>`
      + `<caption class="note" style="caption-side:bottom;text-align:left;padding-top:6px">Uptake and working capacity in mol/kg (= mmol/g), ± 1σ. VSA: 0.15 → 0.01 bar; TSA: 298 → 393 K at 0.15 bar. Selectivity from a 15:85 CO₂/N₂ mixture at 1 bar.</caption>`;
  }

  function lit(rows) {
    const head = `<tr><th>material</th><th>type</th><th class="num">0.15 bar (mol/kg)</th><th class="num">1 bar (mol/kg)</th><th class="num">binding (kJ/mol)</th><th>weak point</th></tr>`;
    document.getElementById("fnd-lit").innerHTML = `<thead>${head}</thead><tbody>` + rows.map((r) =>
      `<tr><td class="lbl2">${r.material}</td><td>${r.type}</td><td class="num">${r.p015}</td><td class="num">${r.p1}</td><td class="num">${r.binding}</td><td>${r.weak}</td></tr>`).join("") + "</tbody>";
  }

  fetch("/api/findings").then((r) => r.json()).then((d) => {
    leads(d.leads); same(d.same_model, d.model); lit(d.literature);
  }).catch((e) => { document.getElementById("fnd-sum").innerHTML = `<div class="callout bad">Could not load findings: ${e}</div>`; });
})();
