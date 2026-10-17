"use strict";
// Agents page, "Validation" view: the validation chain end to end (live counts), and the recursive
// peer review of one structure as a tree that can be replayed from the reviewer's own events
// (data/science/reviews.db): nodes appear as sub-reviewers start, pulse while they work, flash
// red on a failed model call, and take their verdict colour when they finish.
(() => {
  const $ = (id) => document.getElementById(id);
  const qs = new URLSearchParams(location.search);
  const SNAP = !!qs.get("snapshot");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fx = (x, k = 2) => (x == null || isNaN(x) ? "–" : Number(x).toFixed(k));
  const nm = (s) => ({ "Cu-BTC": "HKUST-1", "MgMOF-74-VASP": "Mg-MOF-74" }[s] || String(s || "").replace(/_clean$/, ""));
  const short = (m) => String(m || "").split("/").pop().replace("NVIDIA-", "").replace(/-A\d+B$/, "");
  const getJSON = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
  // a sub-reviewer occasionally returns its split parts as dicts, stored as their Python repr: show the text
  const tidy = (s) => {
    s = String(s ?? "");
    if (!/^\{'(claim|verdict|evidence)'/.test(s)) return s;
    const m = /'claim':\s*['"](.+?)(?:['"](?:,\s*'|\})|$)/.exec(s) || /'evidence':\s*['"](.+?)(?:['"](?:,\s*'|\})|$)/.exec(s);
    return m ? m[1] : s;
  };
  const VERD = ["supported", "partly", "refuted", "unverifiable"];
  const CAPTURE = /selectiv|working capacit|temperature[- ]swing|vacuum[- ]swing|heat of adsorption|\bq_?st\b|isosteric|regenerat|henry/i;
  const STEP_TYPES = new Set(["agent_start", "agent_finish", "agent_fail", "agent_restart", "agent_exit", "delegate", "discovery"]);
  const V = { list: [], name: null, data: null, steps: [], pos: 0, timer: null, sel: "reviewer", poll: null, chainT: null, shown: false };

  // ------------------------------------------------------------------------------- mode switch
  function setMode(val) {
    $("run-view").hidden = val; $("val-view").hidden = !val;
    $("m-run").setAttribute("aria-selected", !val); $("m-val").setAttribute("aria-selected", val);
    $("mode-note").textContent = val ? "how SIEVE's claims are checked: critic, tier-2, referee and peer reviewer" : "the agent team screening the pool, live";
    if (val) qs.set("view", "validation"); else qs.delete("view");
    history.replaceState(null, "", `?${qs}`);
    if (val) showValidation(); else { stopPlay(); window.agentsBoot && window.agentsBoot(); }
  }
  $("m-run").onclick = () => setMode(false);
  $("m-val").onclick = () => setMode(true);

  async function showValidation() {
    if (V.shown) return;
    V.shown = true;
    runSelect();
    chain();
    if (!SNAP) V.chainT = setInterval(chain, 15000);
    try { V.list = await getJSON("/api/validation/reviews"); } catch (_) { V.list = []; }
    const sel = $("rv-select");
    sel.innerHTML = V.list.map((b) => `<option value="${esc(b.name)}">${esc(nm(b.name))} · ${b.n_claims} claims</option>`).join("")
      || `<option>no reviews yet</option>`;
    const want = qs.get("review") && V.list.some((b) => b.name === qs.get("review")) ? qs.get("review") : (V.list[0] || {}).name;
    if (want) sel.value = want;
    sel.onchange = () => load(sel.value, true);
    $("rv-play").onclick = () => (V.timer ? stopPlay() : play(V.pos >= V.steps.length ? 0 : V.pos));
    $("rv-pos").oninput = () => { stopPlay(); render(+$("rv-pos").value); };
    new ResizeObserver(() => V.data && render(V.pos)).observe($("rv-tree"));
    if (want) load(want, !SNAP);
    else $("rv-detail").innerHTML = `<p class="dim">No peer review has finished yet. Open a structure on the Science page to start one.</p>`;
  }

  // the shell's run selector is filled by the run view's boot; fill it here if that has not run
  async function runSelect() {
    const sel = $("run-select");
    if (!sel || sel.options.length) return;
    let cfg; try { cfg = await getJSON("/api/config"); } catch (_) { return; }
    if (sel.options.length) return;
    const sieve = cfg.runs.filter((r) => r.arm === "sieve");
    const want = qs.get("run") || cfg.replay || (sieve[0] || cfg.runs[0] || {}).run_id;
    for (const r of cfg.runs) sel.appendChild(new Option(`${r.run_id} · ${r.arm}`, r.run_id, false, r.run_id === want));
    sel.onchange = () => { qs.set("run", sel.value); location.search = qs.toString(); };
    if (window.Shell && want) Shell.setRun(want);
  }

  // ------------------------------------------------------------------------------------ chain
  async function chain() {
    let c;
    try { c = await getJSON(`/api/validation/chain${qs.get("run") ? `?run=${encodeURIComponent(qs.get("run"))}` : ""}`); }
    catch (e) { $("chain").innerHTML = `<li class="dim">Could not load the chain: ${esc(e.message)}</li>`; return; }
    const s = c.screening || {}, k = c.critic || {}, t = c.tier2 || {}, r = c.referee || {}, rv = c.review || {};
    const rec = t.recommended, mine = (r.arms || []).find((a) => a.run === c.run) || {};
    const tal = rv.tally || {}, tot = VERD.reduce((a, v) => a + (tal[v] || 0), 0) || 1;
    const bar = (tl, n) => `<span class="vbar">${VERD.map((v) => `<i class="v-${v}" style="width:${(100 * (tl[v] || 0)) / (n || 1)}%" title="${tl[v] || 0} ${v}"></i>`).join("")}</span>`;
    const step = (i, title, big, unit, body, cls = "") => `<li class="${cls}"><div class="ch-h"><span class="ch-n">${i}</span>${title}</div>
      <div class="ch-v"><b class="mono">${big}</b> <span>${unit}</span></div><div class="ch-b">${body}</div></li>`;
    const armRow = (a) => `<tr class="${a.run === c.run ? "me" : ""}"><td>${esc(a.run.replace(/-main$/, ""))}</td><td class="mono">${esc(nm(a.pick1))}</td>
      <td class="mono n">${fx(a.claimed)}<sup>T${a.claimed_tier}</sup></td><td class="mono n">${a.pick1_referee == null ? `<span class="dim">pending</span>` : fx(a.pick1_referee)}</td>
      <td class="mono n" title="of the referee's current top 10; ${a.checked} of this arm's ${a.k} picks measured so far">${a.hits == null ? "–" : `${a.hits}/10`}${a.checked < a.k ? `<span class="dim">·${a.checked}m</span>` : ""}</td></tr>`;
    $("chain").innerHTML =
      step(1, "Screening agents", s.agents ?? "–", "agents", `${s.llm_calls ?? 0} model calls on <span class="mono">${esc(c.run || "–")}</span>; ${s.tier1 ?? 0} tier-1 GCMC runs rank ${s.tier1_structures ?? 0} structures (unconverged, ±18%).`)
      + step(2, "Critic veto", k.vetoes ?? 0, "vetoes", `${k.structures ?? 0} structures blocked from the shortlist${(k.reasons || []).length ? ": " + k.reasons.map(([w, n]) => `${esc(w)} <span class="mono">×${n}</span>`).join(", ") : ""}. No tier-1 number may be named a winner.`)
      + step(3, "Tier-2 confirmation", t.structures ?? 0, "confirmed", rec ? `2000-cycle GCMC (±8.6%) before a pick. Recommended <b class="mono">${esc(nm(rec.structure))}</b> ${fx(rec.uptake)} ± ${fx(rec.err)} mol/kg.` : "2000-cycle GCMC (±8.6%) before any recommendation.")
      + step(4, "Referee", `${r.measured ?? 0}/${r.union ?? 0}`, "arm picks re-measured",
        `Independent tier-2 (new seeds, own grids) of every finished arm's top 10: ${r.measurements ?? 0} runs, ${r.replicated ?? 0} replicated${r.running ? `, <b>${r.running} running</b>` : ""}${r.status === "running" ? " · live" : ""}.
         ${mine.pick1 ? `<div class="ch-key">${esc(nm(mine.pick1))}: claimed <b class="mono">${fx(mine.claimed)}</b> → referee <b class="mono">${mine.pick1_referee == null ? "pending" : fx(mine.pick1_referee)}</b> mol/kg</div>` : ""}
         <table class="ch-t"><thead><tr><th>arm</th><th>#1 pick</th><th class="n">claimed</th><th class="n">referee</th><th class="n" title="of the referee top 10">hits</th></tr></thead><tbody>${(r.arms || []).map(armRow).join("")}</tbody></table>${(r.arms || []).some((a) => a.checked < a.k) ? `<div class="dim sm">·Nm = only N of that arm's 10 picks re-measured so far</div>` : ""}`, "wide")
      + step(5, "Peer reviewer", rv.claims ?? 0, `claims · ${rv.structures ?? 0} structures`,
        `${bar(tal, tot)}<div class="ch-tl">${VERD.map((v) => `<span class="t-${v}">${tal[v] || 0} ${v}</span>`).join("")}</div>
         ${rv.splits ?? 0} compound claims split into sub-claims (${rv.nodes ?? 0} nodes). Capture metrics (CO₂/N₂ selectivity, working capacity, Q<sub>st</sub>): <b>${(rv.capture || {}).n ?? 0}</b> claims, ${(rv.capture || {}).supported ?? 0} supported, ${(rv.capture || {}).partly ?? 0} partly, ${(rv.capture || {}).refuted ?? 0} refuted.
         <div class="dim">${(rv.models || []).map(short).map(esc).join(", ")}</div>`);
  }

  // ---------------------------------------------------------------------------- review model
  async function load(name, animate) {
    stopPlay(); clearTimeout(V.poll);
    V.name = name; V.sel = "reviewer";
    qs.set("review", name); history.replaceState(null, "", `?${qs}`);
    let d;
    try { d = await getJSON(`/api/validation/review?name=${encodeURIComponent(name)}`); }
    catch (e) { $("rv-detail").innerHTML = `<p class="dim">Could not load the review: ${esc(e.message)}</p>`; return; }
    if (V.name !== name) return;
    build(d);
    $("rv-pos").max = V.steps.length;
    if (animate && V.steps.length) play(0); else render(V.steps.length);
    detail();
    if (d.run && d.run.status === "running" && !SNAP) V.poll = setTimeout(() => refresh(name), 5000);
  }
  async function refresh(name) {  // a review in progress: follow it
    if (V.name !== name) return;
    let d; try { d = await getJSON(`/api/validation/review?name=${encodeURIComponent(name)}`); } catch (_) { d = null; }
    if (d && V.name === name) {
      const atEnd = !V.timer && V.pos >= V.steps.length;
      build(d); $("rv-pos").max = V.steps.length;
      if (atEnd) render(V.steps.length);
      if (d.run && d.run.status === "running") V.poll = setTimeout(() => refresh(name), 5000);
    }
  }

  // nodes keyed by agent id: "analyst", "reviewer" (lead), "reviewer/c3", "reviewer/c3/a"
  function build(d) {
    V.data = d;
    const evs = d.events || [];
    const live = !d.events_match_tree && d.run && d.run.status === "running";
    const N = {};
    const add = (id, o) => (N[id] = Object.assign(N[id] || { id, kids: [], segs: [], fails: [], tokens: 0, calls: 0 }, o));
    if (d.analyst_writeup || evs.some((e) => e.agent === "analyst")) add("analyst", { kind: "analyst", depth: 0, claim: (d.analyst_writeup || {}).headline || "analyst write-up" });
    add("reviewer", { kind: "lead", depth: 0, claim: `Lead reviewer: ${nm(d.structure || d.name)}` });
    const walk = (n, parent) => {
      add(n.id, { kind: "claim", depth: n.id.split("/").length - 1, parent, claim: tidy(n.claim), verdict: n.verdict, evidence: n.evidence, grounding: n.grounding || [] });
      N[parent].kids.push(n.id);
      (n.children || []).forEach((k) => walk(k, n.id));
    };
    if (!live) (d.claims || []).forEach((n) => walk(n, "reviewer"));
    V.steps = [];
    for (const e of evs) {
      if (e.type === "llm_usage") { const n = N[e.agent]; if (n) { n.tokens += e.tokens || 0; n.calls += 1; } continue; }
      if (!STEP_TYPES.has(e.type) || !e.agent) continue;
      if (!N[e.agent]) {
        if (!live) continue;  // an event from a newer, unfinished run of this review
        const parent = e.parent && N[e.parent] ? e.parent : e.agent.split("/").slice(0, -1).join("/") || "reviewer";
        if (!N[parent]) continue;
        add(e.agent, { kind: "claim", depth: e.agent.split("/").length - 1, parent, claim: (e.text || "").replace(/^check: /, ""), grounding: [] });
        N[parent].kids.push(e.agent);
      }
      V.steps.push(e);
    }
    // per-node history as step indices, so any replay position renders without re-simulating
    V.steps.forEach((e, i) => {
      const n = N[e.agent];
      if (e.model) n.model = e.model;
      if (e.type === "agent_start") { n.segs.push([i, null]); if (n.first == null) n.first = i; }
      else if (e.type === "agent_finish") { const s = n.segs.at(-1); if (s && s[1] == null) s[1] = i; n.fin = i; if (e.verdict) { n.vEv = e.verdict; if (!n.verdict) n.verdict = e.verdict; } }
      else if (e.type === "agent_fail") n.fails.push(i);
      else if (e.type === "delegate") n.split = i;
      else if (e.type === "agent_exit") n.exit = i;
      else if (e.type === "discovery") n.fin = n.fin ?? i;
    });
    if (N.reviewer) N.reviewer.kids.sort((a, b) => +a.split("/c")[1] - +b.split("/c")[1]);
    V.nodes = N;
    V.order = [];
    const visit = (id) => { V.order.push(id); (N[id].kids || []).forEach(visit); };
    if (N.analyst) V.order.push("analyst");
    visit("reviewer");
    V.t0 = V.steps.length ? V.steps[0].ts : 0;
    V.t1 = V.steps.length ? V.steps.at(-1).ts : 1;
    V.live = live;
  }

  // state of node n after the first p steps
  function stateAt(n, p) {
    const seen = (i) => i != null && i < p;
    if (n.first == null) return p >= V.steps.length ? { vis: true, st: n.verdict ? `v-${n.verdict}` : "idle" } : { vis: false };
    if (!seen(n.first)) return { vis: false };
    const s = n.segs.filter((x) => seen(x[0])).at(-1);
    const lastFail = n.fails.filter(seen).at(-1);
    const working = s && !(s[1] != null && seen(s[1]));
    if (working) return { vis: true, st: lastFail != null && lastFail > s[0] && p - lastFail < 4 ? "retry" : "working" };
    if (n.kind !== "claim") return { vis: true, st: "done" };
    if (n.kids.length && !seen(n.exit)) return { vis: true, st: "split" };
    const v = n.kids.length || !V.live ? n.verdict : n.vEv || n.verdict;
    return { vis: true, st: v ? `v-${v}` : "done" };
  }

  // ----------------------------------------------------------------------------------- render
  function render(p) {
    V.pos = p;
    $("rv-pos").value = p;
    const d = V.data, N = V.nodes, span = Math.max(1, V.t1 - V.t0);
    const tNow = p ? V.steps[Math.min(p, V.steps.length) - 1].ts : V.t0;
    const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    $("rv-clock").textContent = V.steps.length ? `${mmss(tNow - V.t0)} / ${mmss(span)}` : "no events";
    $("rv-play").textContent = V.timer ? "Pause" : p >= V.steps.length ? "Replay" : "Play";
    const pct = (ts) => `${(100 * (ts - V.t0)) / span}%`;
    const rows = V.order.map((id, idx) => {
      const n = N[id], s = stateAt(n, p);
      if (!s.vis) return "";
      const last = n.parent ? N[n.parent].kids.at(-1) === id : true;
      const lab = n.kind === "lead" ? `<b>${esc(n.claim)}</b> <span class="dim">· collects the claims, spawns sub-reviewers, writes the assessment</span>`
        : n.kind === "analyst" ? `<span class="dim">analyst write-up ·</span> ${esc(n.claim)}`
        : esc(n.claim);
      const pill = s.st.startsWith("v-") ? `<span class="vpill ${s.st}">${s.st.slice(2)}</span>`
        : s.st === "split" ? `<span class="vpill split">split ×${n.kids.length}</span>`
        : s.st === "working" ? `<span class="vpill work">checking</span>` : s.st === "retry" ? `<span class="vpill retry">retry</span>` : "";
      const segs = n.segs.filter((x) => x[0] < p).map(([a, b]) => {
        const ta = V.steps[a].ts, tb = b != null && b < p ? V.steps[b].ts : tNow;
        return `<i style="left:${pct(ta)};width:max(2px,${(100 * (tb - ta)) / span}%)"></i>`;
      }).join("") + n.fails.filter((i) => i < p).map((i) => `<b style="left:${pct(V.steps[i].ts)}" title="model call failed, retried"></b>`).join("");
      const cap = n.kind === "claim" && CAPTURE.test(n.claim) ? `<span class="cap">capture</span>` : "";
      const g = (n.grounding || []).length ? `<span class="gc" title="Nemotron-Science-v1 grounding passages cited by the sub-reviewer / retrieved">cited ${n.grounding.filter((x) => x.used).length}/${n.grounding.length}</span>` : "";
      return `<div class="vrow k-${n.kind} ${s.st} ${last ? "last" : ""} ${V.sel === id ? "on" : ""}" style="--d:${n.depth}" data-id="${esc(id)}" role="button" tabindex="0">
        <span class="vn"></span><span class="vt">${lab}</span><span class="vc">${cap}</span><span class="vc">${g}</span><span class="vc">${pill}</span><span class="vg">${segs}</span></div>`;
    }).join("");
    const head = `<div class="vrow vhead"><span class="vt dim">agent · claim</span><span class="vg"><span class="ph" style="left:${pct(tNow)}"></span>timeline</span></div>`;
    $("rv-tree").innerHTML = head + rows;
    $("rv-tree").querySelectorAll(".vrow[data-id]").forEach((r) => { r.onclick = () => { V.sel = r.dataset.id; render(V.pos); detail(); }; r.onkeydown = (e) => { if (e.key === "Enter") r.onclick(); }; });
    // summary: tally so far (final once the replay ends)
    const top = (N.reviewer.kids || []).map((id) => N[id]);
    const tl = {}; let done = 0;
    for (const n of top) { const s = stateAt(n, p); if (s.vis && s.st.startsWith("v-")) { tl[s.st.slice(2)] = (tl[s.st.slice(2)] || 0) + 1; done++; } }
    const b = d.brief || {}, tot = top.length || 1;
    const models = (d.usage || []).map((u) => `${esc(short(u.model))} <span class="mono">${u.calls} calls · ${(u.tokens / 1000).toFixed(0)}k tok</span>`).join(" · ")
      || Object.entries(d.models || {}).map(([k, m]) => `${k} ${esc(short(m))}`).join(" · ");
    $("rv-sum").innerHTML = `<span class="vbar big">${VERD.map((v) => `<i class="v-${v}" style="width:${(100 * (tl[v] || 0)) / tot}%"></i>`).join("")}</span>
      <span class="ch-tl">${VERD.map((v) => `<span class="t-${v}"><b class="mono">${tl[v] || 0}</b> ${v}</span>`).join("")}<span class="dim">${done}/${top.length} claims judged</span></span>
      <span class="dim vs-m">${b.n_split || 0} split · depth ${b.max_depth || 1} · grounding ${b.grounding_used || 0}/${b.grounded || 0} cited · ${models}${V.live ? ` · <b class="live">review running</b>` : d.events_match_tree ? "" : " · events of this review are not in reviews.db; showing the final tree"}</span>`;
    $("rv-legend").innerHTML = `<span><i class="sw v-supported"></i>supported</span><span><i class="sw v-partly"></i>partly</span><span><i class="sw v-refuted"></i>refuted</span><span><i class="sw v-unverifiable"></i>unverifiable</span>
      <span><i class="sw work"></i>sub-reviewer working</span><span><i class="sw split"></i>split, waiting for its parts</span><span><i class="sw retry"></i>failed model call (retried)</span>
      <span class="dim">Bars: when each agent ran, on the review's clock. Verdicts of split claims roll up in code: refuted if a part is refuted, supported only if all parts are.</span>`;
    if (V.sel && $("rv-detail").dataset.id !== V.sel) detail();
  }

  function play(from) {
    stopPlay();
    let p = from;
    render(p);
    V.timer = setInterval(() => {
      p += 1;
      if (p > V.steps.length) { stopPlay(); render(V.steps.length); return; }
      render(p);
    }, Math.max(60, Math.min(220, 18000 / Math.max(1, V.steps.length))));
    render(p);
  }
  function stopPlay() { clearInterval(V.timer); V.timer = null; if ($("rv-play") && V.data) $("rv-play").textContent = V.pos >= V.steps.length ? "Replay" : "Play"; }

  // ----------------------------------------------------------------------------------- detail
  function detail() {
    const box = $("rv-detail"), d = V.data, n = V.nodes && V.nodes[V.sel];
    if (!n) return;
    box.dataset.id = V.sel;
    const meta = [n.model ? short(n.model) : null, n.calls ? `${n.calls} model call${n.calls > 1 ? "s" : ""}` : null, n.tokens ? `${(n.tokens / 1000).toFixed(1)}k tokens` : null,
      n.fails.length ? `${n.fails.length} failed call${n.fails.length > 1 ? "s" : ""} retried` : null,
      n.segs.length && n.segs[0][1] != null ? `${fx(V.steps[n.segs.at(-1)[1]].ts - V.steps[n.segs[0][0]].ts, 0)} s` : null].filter(Boolean).join(" · ");
    if (n.kind === "lead") {
      box.innerHTML = `<h3>Lead reviewer · ${esc(nm(d.structure || d.name))}</h3><div class="vd-meta mono">${esc(meta)}</div>
        <p>${esc(d.assessment || "")}</p>${d.would_change_our_mind ? `<p class="dim"><b>What would change the verdict:</b> ${esc(d.would_change_our_mind)}</p>` : ""}
        <div class="vd-k">Models</div><p class="mono sm">lead ${esc(short((d.models || {}).lead))} · sub-reviewers ${esc(short((d.models || {}).sub))}</p>
        <div class="vd-k">How it works</div><p class="sm">The lead lists the analyst's checkable claims and adds the Science page's own captions (pockets, 0.15 bar uptake, reference comparisons, capture metrics). Each claim goes to its own sub-reviewer with an evidence pack of the team's numbers and the three closest Nemotron-Science-v1 worked problems. A compound claim is split and each part gets a sub-sub-reviewer (depth ≤ 2). A claim resting on electrostatics is capped in code, since this model has no framework charges.</p>
        <p class="sm"><a href="/science?name=${encodeURIComponent(d.name)}">Open ${esc(nm(d.name))} on the Science page →</a></p>`;
      return;
    }
    if (n.kind === "analyst") {
      const w = d.analyst_writeup || {};
      box.innerHTML = `<h3>Analyst write-up</h3><div class="vd-meta mono">${esc(meta)}</div><p><b>${esc(w.headline || "")}</b></p><p>${esc(w.why_it_matters || "")}</p>
        ${(w.key_features || []).length ? `<ul class="sm">${w.key_features.map((k) => `<li>${esc(String(k).replace(/^- /, ""))}</li>`).join("")}</ul>` : ""}
        ${w.confidence ? `<p class="sm dim"><b>Confidence:</b> ${esc(w.confidence)}</p>` : ""}${w.caveats ? `<p class="sm dim"><b>Caveats:</b> ${esc(w.caveats)}</p>` : ""}
        <p class="sm dim">The claims the lead reviewer extracts come from this text.</p>`;
      return;
    }
    const kids = n.kids.map((id) => V.nodes[id]);
    const g = (n.grounding || []).slice().sort((a, b) => b.used - a.used || b.score - a.score);
    box.innerHTML = `<div class="vd-top"><span class="vpill v-${esc(n.verdict || "unverifiable")}">${esc(n.verdict || "pending")}</span><span class="mono dim">${esc(n.id)}</span>${CAPTURE.test(n.claim) ? `<span class="cap">capture metric</span>` : ""}</div>
      <p class="vd-claim">${esc(n.claim)}</p><div class="vd-meta mono">${esc(meta)}</div>
      <div class="vd-k">Evidence</div><p class="sm">${esc(n.evidence || (V.live ? "in progress" : "–"))}</p>
      ${kids.length ? `<div class="vd-k">Split into ${kids.length} parts</div><ul class="vd-kids">${kids.map((k) => `<li><span class="vpill v-${esc(k.verdict || "unverifiable")}">${esc(k.verdict || "…")}</span> ${esc(k.claim)}</li>`).join("")}</ul>` : ""}
      <div class="vd-k">Grounding · Nemotron-Science-v1 <span class="dim">${g.filter((x) => x.used).length} of ${g.length} cited by the sub-reviewer</span></div>
      ${g.length ? g.map((x) => `<div class="gsrc ${x.used ? "used" : ""}"><div class="cit">${esc(x.citation)}<span class="${x.used ? "u" : "sc"}">${x.used ? "cited" : "retrieved, not cited"}</span><span class="sc">BM25 ${fx(x.score, 1)}</span></div>
          <div class="q"><b>Q:</b> ${esc(String(x.question || "").slice(0, 240))}${String(x.question || "").length > 240 ? "…" : ""}</div>
          ${x.excerpt ? `<div class="x">${esc(String(x.excerpt).slice(0, 320))}</div>` : ""}${x.answer ? `<div class="a"><b>A:</b> ${esc(String(x.answer).slice(0, 200))}</div>` : ""}
          ${(x.topics || []).length ? `<div class="tp">${x.topics.slice(0, 4).map((t) => `<span>${esc(t)}</span>`).join("")}</div>` : ""}</div>`).join("")
        : `<p class="sm dim">${n.kids.length ? "Split claims are judged through their parts." : "No library passages were retrieved for this claim."}</p>`}
      <p class="sm dim">Library passages are synthetic, LLM-written worked problems (CC-BY-4.0): background for judging whether a mechanism is plausible, never evidence for this structure's numbers.</p>`;
  }

  if (qs.get("view") === "validation") setMode(true);
})();
