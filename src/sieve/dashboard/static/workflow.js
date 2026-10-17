"use strict";
// How it works: the agent workflow as one figure, with live counts for the selected run.
(function () {
  const $ = (id) => document.getElementById(id);
  const short = (m) => (m || "").replace(/^.*\//, "").replace(/^NVIDIA-/, "").replace(/-A\d+B$/, "");
  const nm = (s) => (s || "").replace(/_clean$/, "");
  const n = (x) => (x == null ? "–" : Number(x).toLocaleString());
  const chip = (t, cls = "") => `<span class="chip ${cls}">${t}</span>`;
  const stat = (rows) => `<div class="wf-stat">${rows.filter(Boolean).map((r) => `<span class="row">${r}</span>`).join("")}</div>`;

  function step(i, o) {
    const tag = o.href ? "a" : "div";
    return `<li><${tag} class="wf-step ${o.cls || ""}${o.off ? " off" : ""}"${o.href ? ` href="${o.href}"` : ""}>
      <span class="wf-k"><span class="wf-n">${i}</span>${o.k}</span>
      <span class="wf-title">${o.title}</span>
      <p class="wf-what">${o.what}</p>
      <span class="wf-who">${o.who || ""}</span>
      ${o.stat || ""}</${tag}></li>`;
  }

  function render(d) {
    const r = d.run, M = d.models || {}, q = (role) => (M[role] ? chip(short(M[role])) : "");
    const run = r ? `?run=${encodeURIComponent(r.run_id)}` : "";
    if (!r) { $("wf-run").innerHTML = "No run yet."; return; }
    const pol = r.policy || {};
    $("wf-run").innerHTML = `<span>Run <b class="mono">${r.run_id}</b> (${r.arm}, ${r.status})</span>
      <span>budget <b>${r.spent_h} / ${r.budget_h} CPU-h</b></span>
      <span>objective <b>${r.objective === "capture" ? "flue-gas capture score" : "CO₂ uptake at 1 bar"}</b></span>
      ${pol.random_frac ? `<span>policy <b>v5</b>: upside-seeking, ${Math.round(100 * pol.random_frac)}% random slice</span>` : ""}
      <span><b>${n(d.llm_calls)}</b> model calls</span>`;

    const sc = d.scout, al = d.allocate || {}, si = d.simulate || {}, cr = d.critique, rec = d.recommend;
    const agentic = r.agentic;
    $("wf-steps").innerHTML = [
      step(1, { k: "describe", title: "Measure the shapes", cls: "", href: "/discoveries" + run,
        what: "Pore sizes, void fraction, surface area and metals for every candidate. Seconds each, no simulation.",
        who: chip("code · no AI"), stat: stat([`<b>${n(d.describe.structures)}</b>structures described`]) }),
      step(2, { k: "scout", title: "Map the territory", cls: "loop", off: !agentic, href: "/agents" + run,
        what: "Groups the pool into material families; a sub-scout per family sets its search rule and rates its promise.",
        who: q("scout") + (agentic ? chip("+ sub-scouts") : ""),
        stat: sc ? stat([`<b>${n(sc.calls)}</b>scout decisions`, sc.sub_scouts ? `<b>${n(sc.sub_scouts)}</b>sub-scouts` : null]) : stat(["not used by this arm"]) }),
      step(3, { k: "allocate", title: "Pick the next jobs", cls: "loop", off: !agentic, href: "/agents" + run,
        what: "Chooses which structures to simulate and how carefully, within the round's budget." + (pol.random_frac ? " v5 keeps a random slice so luck is never lost." : ""),
        who: q("allocator") + q("orchestrator"),
        stat: stat([`<b>${n(al.rounds)}</b>rounds`, agentic ? `<b>${n(al.calls)}</b>allocations` : null, al.random_slice ? `<b>${n(al.random_slice)}</b>random picks` : null]) }),
      step(4, { k: "simulate", title: "Run GCMC", cls: "loop", href: "/" + run,
        what: "RASPA on local CPUs: short tier-1 runs to rank, long tier-2 runs to confirm" + (si.tier3 ? ", tier-3 flue-gas checks." : ".") + " Timeouts are normal.",
        who: chip("RASPA2 workers"),
        stat: stat([`<b>${n(si.tier1)}</b>tier 1 · <b>${n(si.tier2)}</b>tier 2${si.tier3 ? ` · <b>${n(si.tier3)}</b>tier 3` : ""}`, `<b>${n(si.timeouts)}</b>timeouts recovered`]) }),
      step(5, { k: "critique", title: "Challenge the ranking", cls: "loop", off: !agentic, href: "/agents" + run,
        what: "Flags leaders backed only by cheap runs, forces confirmations, caps oversized cells. Sub-critics examine each contested leader.",
        who: q("critic") + (agentic ? chip("+ sub-critics") : ""),
        stat: cr ? stat([`<b>${n(cr.vetoes)}</b>vetoes`, cr.disorder_flags ? `<b>${n(cr.disorder_flags)}</b>disorder artefacts` : null]) : stat(["no critic in this arm"]) }),
      step(6, { k: "recommend", title: "Name a winner", href: rec ? `/science?name=${encodeURIComponent(rec.structure)}` : null,
        what: "Only a tier-2-confirmed structure may be named. The analyst explains why it works.",
        who: q("orchestrator") + q("analyst"),
        stat: stat([rec ? `<b>${nm(rec.structure)}</b>${Number(rec.uptake).toFixed(2)} mol/kg, tier ${rec.tier}` : "not yet"]) }),
    ].join("");

    const v = d.verify || {}, ref = v.referee || {}, rv = v.review || {}, dis = v.disorder, t = rv.tally || {};
    $("wf-verify").innerHTML = [
      step("A", { k: "critic veto", title: "Confirmed or vetoed", cls: "verify", href: "/agents?view=validation",
        what: "Nothing measured only by a cheap run can be called a winner.", who: q("critic"),
        stat: stat([cr ? `<b>${n(cr.vetoes)}</b>vetoes · <b>${n(si.tier2)}</b>confirmed` : "–"]) }),
      step("B", { k: "referee", title: "Re-measured independently", cls: "verify", href: "/compare",
        what: "A fresh tier-2 simulation of every arm's top 10, with new seeds. No AI involved.", who: chip("code · no AI"),
        stat: stat([ref.measured != null ? `<b>${n(ref.measured)}/${n(ref.union)}</b>picks re-measured` : "not run",
          ref.best ? `best: <b>${nm(ref.best.structure)}</b>${Number(ref.best.uptake).toFixed(2)}` : null]) }),
      step("C", { k: "disorder check", title: "Crystal file is sound", cls: "verify", href: "/compare",
        what: "Flags structures whose file keeps atoms on top of each other, which inflates binding. Flagged leads are re-measured on a resolved copy.",
        who: chip("code · no AI"),
        stat: stat([dis ? `<b>${n(dis.disordered)}</b>of ${n(dis.scanned)} scanned are disordered` : "scan pending"]) }),
      step("D", { k: "peer review", title: "Every claim checked", cls: "verify", href: "/agents?view=validation",
        what: "A lead reviewer gives each claim to a sub-reviewer, which splits compound claims further. Grounded on Nemotron-Science-v1.",
        who: (rv.models || []).map((m) => chip(short(m))).join("") + chip("recursive"),
        stat: stat([rv.claims != null ? `<b>${n(rv.claims)}</b>claims · ${n(t.supported)} supported · ${n(t.partly)} partly · ${n(t.refuted)} refuted` : "–"]) }),
    ].join("");
  }

  function load() {
    const run = (window.Shell && Shell.run) || "";
    fetch("/api/workflow" + (run ? `?run=${encodeURIComponent(run)}` : "")).then((r) => r.json()).then(render)
      .catch((e) => { $("wf-run").innerHTML = `<span class="callout bad">Could not load: ${e}</span>`; });
  }

  // run selector: only agent runs are meaningful here, but baselines render with the AI steps dimmed
  const sel = $("run-select");
  if (sel) {
    fetch("/api/config").then((r) => r.json()).then((c) => {
      const cur = (window.Shell && Shell.run) || "";
      const runs = c.runs || [];
      sel.innerHTML = runs.map((x) => `<option value="${x.run_id}">${x.run_id} · ${x.arm}</option>`).join("");
      const pick = runs.find((x) => x.run_id === cur) || runs.find((x) => (x.arm || "").startsWith("sieve")) || runs[0];
      if (pick) { sel.value = pick.run_id; if (window.Shell) Shell.setRun(pick.run_id); }
      load();
    }).catch(load);
    sel.addEventListener("change", () => { if (window.Shell) Shell.setRun(sel.value); load(); });
  } else load();
  setInterval(load, 20000);
})();
