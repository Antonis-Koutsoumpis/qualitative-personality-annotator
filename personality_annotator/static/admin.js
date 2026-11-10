/* Admin page: progress overview, per-item results (heatmap, peaks, segments), exports. */
(() => {
  const $ = (id) => document.getElementById(id);
  let ov = null;        // overview payload
  let cfg = null;       // public config
  let res = null;       // current item results
  let rMedia = null;    // media element in results view
  let currentItem = null;
  let selectedRaters = null;   // null = all

  // ------------------------------------------------------------- boot
  async function boot() {
    cfg = await PA.api("/api/config");
    try { await loadOverview(); }
    catch (e) {
      if (e.status === 401) { showLogin(); return; }
      PA.toast(e.message, true); return;
    }
    bind();
    showTab(location.hash.replace("#", "") || "overview");
  }

  function showLogin() {
    $("login").classList.remove("hidden");
    $("tabs").classList.add("hidden");
    $("loginForm").onsubmit = async (e) => {
      e.preventDefault();
      try { await PA.api("/api/admin/login", { json: { password: $("pw").value } }); location.reload(); }
      catch (err) { PA.toast(err.message, true); }
    };
  }

  async function loadOverview() {
    ov = await PA.api("/api/admin/overview");
    $("project").textContent = ov.project_name + " · admin";
    $("modalityChip").textContent = ov.modality;
    renderOverview();
    fillItemSelect();
    $("binS").value = cfg.coverage_bin_s;
    $("binWrap").classList.toggle("hidden", ov.modality === "text");
    $("eVar").innerHTML = `<option value="">all variables</option>` +
      cfg.variables.map(v => `<option value="${PA.esc(v.id)}">${PA.esc(v.label)}</option>`).join("");
    $("paths").innerHTML = `Config file: <span class="mono">${PA.esc(ov.config_path)}</span><br>
      Data folder: <span class="mono">${PA.esc(ov.data_dir)}</span><br>
      This computer's instance id: <span class="mono">${PA.esc(ov.instance_id)}</span>` +
      (ov.password_protected ? "" : `<br>Admin page is not password-protected (set ANNOTATOR_ADMIN_PASSWORD to protect it).`);
  }

  function showTab(name) {
    if (!["overview", "results", "data"].includes(name)) name = "overview";
    document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
    ["overview", "results", "data"].forEach(t => $(`tab-${t}`).classList.toggle("hidden", t !== name));
    history.replaceState(null, "", `#${name}`);
    if (name === "results" && !res && $("itemSel").value) loadItem($("itemSel").value);
  }

  // ------------------------------------------------------------- overview
  function renderOverview() {
    const t = ov.totals;
    const stat = (v, l) => `<div class="card stat"><div class="v">${v}</div><div class="l">${l}</div></div>`;
    $("stats").innerHTML =
      stat(t.items, "items") +
      stat(`${t.completed}<span class="muted" style="font-size:1rem"> / ${t.target_annotations}</span>`, `annotations (target: ${ov.k} per item)`) +
      stat(t.items_complete, "items fully annotated") +
      stat(ov.annotators.length, "annotators");

    const s = ov.sync || {};
    $("sources").innerHTML = ov.sources.map(src =>
      `<div><span class="chip">${PA.esc(src.type)}</span> <span class="mono">${PA.esc(src.path || src.record || src.url)}</span></div>`).join("") +
      `<p class="muted" style="margin:.5rem 0 0">Last scan ${PA.esc(s.at || "")}: ${s.found ?? 0} files found, ${s.added ?? 0} new, ${s.active_items ?? 0} active items` +
      (s.deactivated ? `, ${s.deactivated} no longer found (kept but hidden)` : "") + `.</p>` +
      (s.errors?.length ? `<p style="color:var(--danger);margin:.4rem 0 0">${s.errors.map(PA.esc).join("<br>")}</p>` : "") +
      (s.text_kept_because_annotated?.length ? `<p class="muted">Text changed at the source but kept (already annotated): ${s.text_kept_because_annotated.map(PA.esc).join(", ")}</p>` : "");

    $("annTable").innerHTML = `<thead><tr><th>Code</th><th class="num">Age</th><th>Gender</th><th class="num">Completed</th><th class="num">Open</th><th class="num">Still available</th><th class="num">Ratings</th><th>Joined</th></tr></thead><tbody>` +
      (ov.annotators.length ? ov.annotators.map(a => `<tr><td><strong>${PA.esc(a.code)}</strong></td><td class="num">${a.age ?? ""}</td><td>${PA.esc(a.gender || "")}</td>
        <td class="num">${a.completed}</td><td class="num">${a.open}</td><td class="num">${a.remaining - a.open}</td><td class="num">${a.n_ratings}</td>
        <td class="small muted">${PA.esc((a.created_at || "").slice(0, 16).replace("T", " "))}</td></tr>`).join("")
        : `<tr><td colspan="8" class="muted">Nobody has logged in yet.</td></tr>`) + `</tbody>`;

    $("itemTable").innerHTML = `<thead><tr><th>Item</th><th>Source</th><th class="num">Duration</th><th>Completed</th><th class="num">Open</th><th class="num">No info</th><th></th></tr></thead><tbody>` +
      ov.items.map(i => {
        const pct = Math.min(100, (i.n_completed / ov.k) * 100);
        return `<tr class="${i.active ? "" : "muted"}"><td><strong>${PA.esc(i.item_id)}</strong>${i.active ? "" : " <span class='small'>(not found in sources)</span>"}<div class="small muted">${PA.esc(i.filename || "")}</div></td>
          <td>${PA.esc(i.source_type)}</td><td class="num">${i.duration_s ? PA.fmtTime(i.duration_s) : "—"}</td>
          <td style="min-width:120px"><div class="small">${i.n_completed} / ${ov.k}</div><div class="progress"><div style="width:${pct}%"></div></div></td>
          <td class="num">${i.n_open}</td><td class="num">${i.n_no_info}</td>
          <td><button class="small" data-view="${PA.esc(i.item_id)}">View results</button></td></tr>`;
      }).join("") + `</tbody>`;
    $("itemTable").querySelectorAll("[data-view]").forEach(b => b.onclick = () => {
      $("itemSel").value = b.dataset.view; showTab("results"); loadItem(b.dataset.view);
    });

    $("noInfoCard").classList.toggle("hidden", !ov.no_info.length);
    $("noInfoTable").innerHTML = `<thead><tr><th>Item</th><th>Annotator</th><th>Explanation</th></tr></thead><tbody>` +
      ov.no_info.map(n => `<tr><td>${PA.esc(n.item_id)}</td><td>${PA.esc(n.annotator_code)}</td><td>${PA.esc(n.no_info_explanation)}</td></tr>`).join("") + `</tbody>`;
  }

  function fillItemSelect() {
    const sel = $("itemSel"), prev = sel.value;
    sel.innerHTML = ov.items.filter(i => i.active).map(i =>
      `<option value="${PA.esc(i.item_id)}">${PA.esc(i.item_id)} — ${i.n_completed}/${ov.k} done</option>`).join("");
    if (prev) sel.value = prev;
  }

  // ------------------------------------------------------------- results
  function resultQuery() {
    const q = new URLSearchParams();
    if ($("inclOpen").checked) q.set("include_open", "true");
    if (selectedRaters) q.set("annotators", selectedRaters.join(","));
    if (ov.modality !== "text" && +$("binS").value > 0) q.set("bin_s", $("binS").value);
    return q;
  }

  async function loadItem(itemId, keepRaters = false) {
    if (!itemId) return;
    if (!keepRaters || itemId !== currentItem) selectedRaters = null;
    currentItem = itemId;
    try {
      res = await PA.api(`/api/admin/items/${encodeURIComponent(itemId)}/results?${resultQuery()}`);
    } catch (e) { PA.toast(e.message, true); return; }
    renderRaterFilter();
    renderMedia();
    renderHeat();
    renderSegments();
    await loadPeaks();
    $("rNoInfo").classList.toggle("hidden", !res.no_info.length);
    $("rNoInfo").innerHTML = `<h2>No personality information</h2>` + res.no_info.map(n =>
      `<p class="small"><strong>${PA.esc(n.annotator_code)}</strong>: ${PA.esc(n.no_info_explanation)}</p>`).join("");
  }

  function renderRaterFilter() {
    const raters = res.raters;
    if (!raters.length) { $("raterFilter").innerHTML = `<span class="muted">No annotations for this item yet.</span>`; return; }
    $("raterFilter").innerHTML = `<span class="muted">Raters:</span> ` + raters.map(r =>
      `<label class="rater-toggle"><input type="checkbox" value="${PA.esc(r)}" ${!selectedRaters || selectedRaters.includes(r) ? "checked" : ""}>${PA.esc(r)}</label>`).join("");
    $("raterFilter").querySelectorAll("input").forEach(cb => cb.onchange = () => {
      const on = [...$("raterFilter").querySelectorAll("input:checked")].map(x => x.value);
      selectedRaters = on.length === raters.length ? null : on;
      loadItem(currentItem, true);
    });
  }

  function renderMedia() {
    const box = $("rMedia");
    const it = res.item;
    if (rMedia && rMedia.dataset.item === it.item_id) return;
    box.innerHTML = "";
    rMedia = null;
    if (it.modality === "video") {
      box.innerHTML = `<div class="player-wrap"><video controls preload="auto"></video></div>`;
    } else if (it.modality === "audio") {
      box.innerHTML = `<div class="audio-wrap"><audio controls preload="auto"></audio></div>`;
    } else {
      box.innerHTML = `<div class="text-panel" id="rText"></div>`;
      return;
    }
    rMedia = box.querySelector("video, audio");
    rMedia.dataset.item = it.item_id;
    rMedia.src = it.media_url;
    rMedia.ontimeupdate = movePlayheads;
    rMedia.onloadedmetadata = () => renderHeat();
  }

  // `soft` keeps shading light enough to read text on top of it
  const heatColor = (frac, soft = false) => frac <= 0 ? "var(--heat-0)"
    : `color-mix(in oklab, var(--heat-1) ${Math.round(soft ? 12 + 40 * frac : 15 + 85 * frac)}%, var(--heat-0))`;
  function scoreColor(mean, n, maxN, soft = false) {
    if (!n) return "var(--heat-0)";
    const mid = (cfg.scale.min + cfg.scale.max) / 2, half = (cfg.scale.max - cfg.scale.min) / 2;
    const dir = (mean - mid) / half;     // -1 .. 1
    const base = dir < 0 ? "var(--low)" : dir > 0 ? "var(--high)" : "var(--mid)";
    const strength = Math.max(Math.abs(dir), 0.25) * (0.45 + 0.55 * n / maxN) * (soft ? 0.55 : 1);
    return `color-mix(in oklab, ${base} ${Math.round(100 * strength)}%, var(--heat-0))`;
  }

  function renderHeat() {
    const isText = res.item.modality === "text";
    $("textVar").classList.toggle("hidden", !isText);
    const cov = res.coverage;
    const maxN = Math.max(1, ...cov.map(c => c.n_annotators));
    const byScore = $("colorBy").value === "score";
    $("legend").innerHTML = byScore
      ? `<span>low (${cfg.scale.min})</span><span class="bar" style="background:linear-gradient(90deg,var(--low),var(--heat-0),var(--high))"></span><span>high (${cfg.scale.max})</span><span class="muted">· stronger = more raters</span>`
      : `<span>0</span><span class="bar" style="background:linear-gradient(90deg,var(--heat-0),var(--heat-1))"></span><span>${maxN} rater${maxN > 1 ? "s" : ""}</span>`;
    if (isText) return renderTextHeat(maxN, byScore);

    const bin = res.bin_s;
    const dur = (rMedia && isFinite(rMedia.duration) && rMedia.duration) || res.item.duration_s ||
      Math.max(0, ...cov.map(c => c.bin_end_s));
    const nBins = Math.max(1, Math.ceil(dur / bin - 1e-9));
    const idx = {};
    cov.forEach(c => { (idx[c.variable] ||= {})[c.bin_index] = c; });
    $("heatTitle").textContent = `Annotation coverage (${bin} s bins)`;
    const heat = $("heat");
    heat.className = "heat";
    heat.innerHTML = cfg.variables.map(v => {
      const cells = [];
      for (let b = 0; b < nBins; b++) {
        const c = idx[v.id]?.[b];
        const col = byScore ? scoreColor(c?.mean_score, c?.n_annotators || 0, maxN) : heatColor((c?.n_annotators || 0) / maxN);
        cells.push(`<div style="background:${col}"></div>`);
      }
      return `<div class="lab" title="${PA.esc(v.label)}"><span class="chip"><span class="dot" style="background:${PA.varColor(cfg, v.id)}"></span>${PA.esc(v.label)}</span></div>
        <div class="cells" data-var="${PA.esc(v.id)}">${cells.join("")}<div class="ph"></div></div>`;
    }).join("");
    heat.querySelectorAll(".cells").forEach(row => {
      const where = (e) => {
        const r = row.getBoundingClientRect();
        return Math.min(nBins - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * nBins)));
      };
      row.onmousemove = (e) => {
        const b = where(e), v = row.dataset.var, c = idx[v]?.[b];
        const s = b * bin, en = Math.min(dur, (b + 1) * bin);
        const rs = res.ratings.filter(r => r.variable === v && r.start_s < en && Math.max(r.end_s, r.start_s + 1e-6) > s);
        showTip(e, `<strong>${PA.esc(PA.varLabel(cfg, v))}</strong> · ${PA.fmtTime(s)}–${PA.fmtTime(en)}<br>
          ${c ? `${c.n_annotators} rater(s) · mean ${c.mean_score}${c.sd_score != null ? ` · SD ${c.sd_score}` : ""} · low/mid/high ${c.n_low}/${c.n_mid}/${c.n_high}` : "no annotations"}` +
          rs.slice(0, 6).map(r => `<div class="ex"><strong>${PA.esc(r.annotator_code)}</strong> (${r.score}): ${PA.esc(r.explanation)}</div>`).join("") +
          (rs.length > 6 ? `<div class="ex">… and ${rs.length - 6} more</div>` : ""));
      };
      row.onmouseleave = hideTip;
      row.onclick = (e) => { if (rMedia) { rMedia.currentTime = where(e) * bin; rMedia.play(); } };
    });
    movePlayheads();
  }

  function movePlayheads() {
    if (!rMedia) return;
    const d = rMedia.duration || res.item.duration_s;
    if (!d) return;
    document.querySelectorAll("#heat .ph").forEach(p => p.style.left = `${(rMedia.currentTime / d) * 100}%`);
  }

  function renderTextHeat(maxN, byScore) {
    const tv = $("textVar");
    if (!tv.options.length) {
      tv.innerHTML = cfg.variables.map(v => `<option value="${PA.esc(v.id)}">${PA.esc(v.label)}</option>`).join("");
      const firstUsed = res.coverage[0]?.variable; if (firstUsed) tv.value = firstUsed;
    }
    const v = tv.value;
    $("heatTitle").textContent = `Coverage per word · ${PA.varLabel(cfg, v)}`;
    $("heat").className = "";
    $("heat").innerHTML = `<p class="small muted" style="margin:0">Words in the text on the left are shaded by how many raters marked them for the selected variable. Hover a word for details.</p>`;
    const panel = $("rText");
    const words = PA.renderText(panel, res.item.text || "");
    const idx = {};
    res.coverage.filter(c => c.variable === v).forEach(c => idx[c.token_index] = c);
    const vMax = Math.max(1, ...Object.values(idx).map(c => c.n_annotators));
    words.forEach((w, i) => {
      const c = idx[i];
      w.style.background = c ? (byScore ? scoreColor(c.mean_score, c.n_annotators, vMax, true) : heatColor(c.n_annotators / vMax, true)) : "";
      w.onmousemove = (e) => {
        const s = +w.dataset.s, en = +w.dataset.e;
        const rs = res.ratings.filter(r => r.variable === v && r.char_start < en && r.char_end > s);
        showTip(e, `<strong>${PA.esc(PA.varLabel(cfg, v))}</strong> · “${PA.esc(w.textContent)}”<br>` +
          (c ? `${c.n_annotators} rater(s) · mean ${c.mean_score}${c.sd_score != null ? ` · SD ${c.sd_score}` : ""}` : "no annotations") +
          rs.slice(0, 6).map(r => `<div class="ex"><strong>${PA.esc(r.annotator_code)}</strong> (${r.score}): ${PA.esc(r.explanation)}</div>`).join(""));
      };
      w.onmouseleave = hideTip;
    });
  }

  function showTip(e, html) {
    const t = $("tip");
    t.innerHTML = html;
    t.classList.remove("hidden");
    const x = Math.min(e.clientX + 14, innerWidth - t.offsetWidth - 8);
    const y = e.clientY + 16 + t.offsetHeight > innerHeight ? e.clientY - t.offsetHeight - 10 : e.clientY + 16;
    t.style.left = `${x}px`; t.style.top = `${y}px`;
  }
  function hideTip() { $("tip").classList.add("hidden"); }

  function where(r) {
    return res.item.modality === "text"
      ? `<span class="quote">“${PA.esc((r.span_text || "").slice(0, 90))}${(r.span_text || "").length > 90 ? "…" : ""}”</span>`
      : `<span class="mono">${PA.fmtTime(r.start_s)}–${PA.fmtTime(r.end_s)}</span>`;
  }

  function renderSegments() {
    const rows = [...res.ratings].sort((a, b) => (a.start_s ?? a.char_start) - (b.start_s ?? b.char_start));
    $("segTable").innerHTML = `<thead><tr><th>Rater</th><th>Segment</th><th>Variable</th><th class="num">Score</th><th>Explanation</th><th></th></tr></thead><tbody>` +
      (rows.length ? rows.map(r => `<tr data-seek="${r.start_s ?? ""}">
        <td><strong>${PA.esc(r.annotator_code)}</strong>${r.assignment_status !== "completed" ? '<div class="small muted">not submitted</div>' : ""}</td>
        <td>${where(r)}</td>
        <td><span class="chip"><span class="dot" style="background:${PA.varColor(cfg, r.variable)}"></span>${PA.esc(PA.varLabel(cfg, r.variable))}</span></td>
        <td class="num">${r.score}</td>
        <td>${PA.esc(r.explanation)}${r.notes ? `<div class="small muted">Note: ${PA.esc(r.notes)}</div>` : ""}</td>
        <td><button class="small" title="Extract this segment" data-seg="${r.segment_id}">⇩</button></td></tr>`).join("")
        : `<tr><td colspan="6" class="muted">No ratings${$("inclOpen").checked ? "" : " from submitted items"}.</td></tr>`) + `</tbody>`;
    bindRowActions($("segTable"));
    $("segTable").querySelectorAll("[data-seg]").forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      extract({ mode: "segments", segment_ids: [b.dataset.seg], include_open: $("inclOpen").checked });
    });
  }

  function bindRowActions(table) {
    table.querySelectorAll("tr[data-seek]").forEach(tr => tr.onclick = () => {
      if (rMedia && tr.dataset.seek !== "") { rMedia.currentTime = +tr.dataset.seek; rMedia.play(); }
    });
  }

  async function loadPeaks() {
    const q = resultQuery();
    q.set("threshold", $("thr").value || 1);
    let peaks;
    try { peaks = await PA.api(`/api/admin/items/${encodeURIComponent(currentItem)}/peaks?${q}`); }
    catch (e) { PA.toast(e.message, true); return; }
    const isText = res.item.modality === "text";
    $("peakTable").innerHTML = `<thead><tr><th>Variable</th><th>Segment</th><th class="num">Max raters</th><th class="num">Mean score</th><th></th></tr></thead><tbody>` +
      (peaks.length ? peaks.map((p, i) => `<tr data-seek="${isText ? "" : p.start}">
        <td><span class="chip"><span class="dot" style="background:${PA.varColor(cfg, p.variable)}"></span>${PA.esc(PA.varLabel(cfg, p.variable))}</span></td>
        <td>${isText ? `<span class="quote">“${PA.esc((p.span_text || "").slice(0, 80))}${(p.span_text || "").length > 80 ? "…" : ""}”</span>` : `<span class="mono">${PA.fmtTime(p.start)}–${PA.fmtTime(p.end)}</span>`}</td>
        <td class="num">${p.max_n_annotators}</td><td class="num">${p.mean_score ?? ""}</td>
        <td><button class="small" data-peak="${i}" title="Extract this peak">⇩</button></td></tr>`).join("")
        : `<tr><td colspan="5" class="muted">No stretches with at least ${$("thr").value} rater(s).</td></tr>`) + `</tbody>`;
    bindRowActions($("peakTable"));
    $("peakTable").querySelectorAll("[data-peak]").forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      const p = peaks[+b.dataset.peak];
      extract({ mode: "region", item_id: currentItem, variable: p.variable, start: p.start, end: p.end,
        include_open: $("inclOpen").checked, annotators: selectedRaters || [] });
    });
  }

  async function extract(body) {
    PA.toast("Preparing the download…");
    try { await PA.download("/api/admin/extract", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
    catch (e) { PA.toast(e.message, true); }
  }

  // ------------------------------------------------------------- bindings
  function bind() {
    document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
    $("btnSync").onclick = async () => {
      $("btnSync").disabled = true;
      try { await PA.api("/api/admin/sync", { json: {} }); await loadOverview(); PA.toast("Sources re-scanned"); }
      catch (e) { PA.toast(e.message, true); }
      $("btnSync").disabled = false;
    };
    $("itemSel").onchange = () => loadItem($("itemSel").value);
    const move = (d) => {
      const sel = $("itemSel"), i = Math.max(0, Math.min(sel.options.length - 1, sel.selectedIndex + d));
      sel.selectedIndex = i; loadItem(sel.value);
    };
    $("prevItem").onclick = () => move(-1);
    $("nextItem").onclick = () => move(1);
    $("inclOpen").onchange = () => loadItem(currentItem, true);
    $("binS").onchange = () => loadItem(currentItem, true);
    $("thr").onchange = loadPeaks;
    $("colorBy").onchange = renderHeat;
    $("textVar").onchange = renderHeat;
    $("btnExtractItemPeaks").onclick = () => extract({ mode: "peaks", item_id: currentItem, threshold: +$("thr").value || 1,
      include_open: $("inclOpen").checked, annotators: selectedRaters || [], bin_s: ov.modality === "text" ? null : +$("binS").value });

    document.querySelectorAll("[data-export]").forEach(b => b.onclick = async () => {
      const q = new URLSearchParams();
      if ($("xOpen").checked) q.set("include_open", "true");
      if ($("xEmpty").checked) q.set("include_empty", "true");
      q.set("threshold", $("xThr").value || 1);
      try { await PA.download(`/api/admin/export/${b.dataset.export}?${q}`); }
      catch (e) { PA.toast(e.message, true); }
    });
    $("btnExtractAll").onclick = () => extract({ mode: "peaks", threshold: +$("eThr").value || 1,
      variable: $("eVar").value || null, include_open: $("xOpen").checked });
    $("importFile").onchange = async () => {
      const f = $("importFile").files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch("/api/admin/import", { method: "POST", body: fd, credentials: "same-origin" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || r.statusText);
        const c = j.imported;
        $("importResult").textContent = `Imported: ${c.annotators} annotator(s), ${c.assignments} assignment(s), ${c.segments} segment(s), ${c.ratings} rating(s).` +
          (j.warnings.length ? "\nWarnings:\n- " + j.warnings.join("\n- ") : "");
        $("importResult").classList.remove("hidden");
        await loadOverview();
        res = null;
      } catch (e) { PA.toast(e.message, true); }
      $("importFile").value = "";
    };
  }

  boot();
})();
