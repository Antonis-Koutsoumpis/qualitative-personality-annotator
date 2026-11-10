/* Annotator page: one item at a time, segments with one or more ratings each. */
(() => {
  const $ = (id) => document.getElementById(id);
  let cfg, data;                       // config and current assignment payload
  let modality;
  let media = null, ws = null, wsRegions = null, draftRegion = null;
  let watched = [];                    // merged intervals known locally
  let pending = [];                    // intervals not yet sent
  let lastT = null, previewing = false;
  let words = [], scrolledToEnd = false, readingTime = 0;
  let editingId = null;                // segment id being edited, or null for new
  let textSel = null;                  // {s, e} for text draft
  let progressTimer = null, readTimer = null;
  let programmatic = false;           // true while we add wavesurfer regions ourselves

  // ------------------------------------------------------------- utilities
  const mergeIv = (ivs) => {
    const out = [];
    ivs.map(([a, b]) => [+a, +b]).filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0])
      .forEach(([a, b]) => {
        const last = out[out.length - 1];
        if (last && a <= last[1] + 0.05) last[1] = Math.max(last[1], b); else out.push([a, b]);
      });
    return out;
  };
  const coveredLen = (ivs, d) => ivs.reduce((s, [a, b]) => s + Math.max(0, Math.min(b, d) - Math.max(a, 0)), 0);
  const duration = () => (media && isFinite(media.duration) && media.duration > 0) ? media.duration : (data?.item?.duration_s || 0);
  const draftKey = () => `pa-draft:${data.assignment.id}`;

  // ------------------------------------------------------------- start-up
  async function init() {
    try {
      cfg = await PA.api("/api/config");
      const me = await PA.api("/api/me");
      $("me").textContent = me.code;
    } catch (e) {
      if (e.status === 401) { location.href = "/"; return; }
      PA.toast(e.message, true); return;
    }
    $("project").textContent = cfg.project_name;
    $("btnSubmit").textContent = cfg.item_selection === "choose" ? "Submit item" : "Submit item & continue";
    buildHelp();
    bindStatic();
    if (cfg.item_selection === "choose") await showList(); else await loadCurrent();
  }

  const chooseMode = () => cfg.item_selection === "choose";

  // ------------------------------------------------------------- item chooser
  let listItems = [], typeFilter = "";

  async function showList() {
    await leaveItem();
    $("loading").classList.remove("hidden");
    let resp;
    try { resp = await PA.api("/api/items"); }
    catch (e) { if (e.status === 401) location.href = "/"; else PA.toast(e.message, true); return; }
    $("loading").classList.add("hidden");
    listItems = resp.items;
    $("btnAddFiles").classList.toggle("hidden", !resp.can_add_files);
    $("btnAddFiles").onclick = () => PA.addFilesDialog(() => showList(), { isLocal: resp.is_local });
    $("chooser").classList.remove("hidden");
    $("btnBack").classList.add("hidden");
    $("itemName").textContent = "";
    const done = listItems.filter(i => i.status === "completed").length;
    $("progressText").textContent = `${done} submitted · ${listItems.length} item(s)`;
    renderTypeTabs();
    renderList();
  }

  function renderTypeTabs() {
    const counts = { "": listItems.length };
    listItems.forEach(i => counts[i.modality] = (counts[i.modality] || 0) + 1);
    const types = cfg.modalities.filter(m => cfg.modalities.length === 1 || counts[m]);
    if (typeFilter && !counts[typeFilter]) typeFilter = "";
    const tabs = (types.length > 1 ? [["", "All"]] : []).concat(types.map(m => [m, PA.TYPE_LABEL[m]]));
    $("typeTabs").innerHTML = tabs.map(([m, l]) =>
      `<button data-type="${m}" class="${m === typeFilter ? "on" : ""}">${m ? PA.TYPE_ICON[m] + " " : ""}${l} <span class="muted">${counts[m] || 0}</span></button>`).join("");
    $("typeTabs").querySelectorAll("button").forEach(b => b.onclick = () => { typeFilter = b.dataset.type; renderTypeTabs(); renderList(); });
  }

  function renderList() {
    const st = $("statusFilter").value, qtxt = $("search").value.trim().toLowerCase();
    const rows = listItems.filter(i => (!typeFilter || i.modality === typeFilter) && (!st || i.status === st)
      && (!qtxt || (i.filename || i.item_id).toLowerCase().includes(qtxt)));
    $("listCount").textContent = `${rows.length} shown`;
    const label = { new: "Not started", open: "In progress", completed: "Submitted ✓", full: "Enough annotators" };
    $("itemList").innerHTML = `<thead><tr><th>Item</th><th>Type</th><th class="num">Length</th><th>Status</th><th></th></tr></thead><tbody>` +
      rows.map(i => {
        const act = i.status === "completed" || i.status === "full" ? "" :
          `<button class="small ${i.status === "open" ? "" : "primary"}" data-open="${PA.esc(i.item_id)}">${i.status === "open" ? "Continue" : "Annotate"}</button>`;
        return `<tr class="${act ? "" : "done"}" ${act ? `data-open="${PA.esc(i.item_id)}"` : ""}>
          <td><span class="type-ico">${PA.TYPE_ICON[i.modality]}</span>${PA.esc(i.filename || i.item_id)}
            ${i.source_type !== "local" ? `<span class="small muted"> · ${PA.esc(i.source_type)}</span>` : ""}</td>
          <td>${PA.TYPE_LABEL[i.modality]}</td>
          <td class="num">${i.modality === "text" ? (i.n_chars ? `${Math.max(1, Math.round(i.n_chars / 6))} words` : "") : (i.duration_s ? PA.fmtTime(i.duration_s).replace(/\.\d$/, "") : "")}</td>
          <td><span class="badge ${i.status}">${label[i.status]}</span>${i.n_segments ? ` <span class="small muted">${i.n_segments} segment(s)</span>` : ""}</td>
          <td>${act}</td></tr>`;
      }).join("") + `</tbody>`;
    $("itemList").querySelectorAll("[data-open]").forEach(el => el.onclick = (e) => { e.stopPropagation(); openItem(el.dataset.open); });
    const empty = $("listEmpty");
    empty.classList.toggle("hidden", rows.length > 0);
    empty.innerHTML = listItems.length ? "No items match these filters."
      : (cfg.annotators_can_add_files ? "No items yet. Click <strong>+ Add files</strong> to add a folder, files or a link." : "No items yet. Ask the researcher to add files.");
  }

  async function openItem(itemId) {
    try {
      const resp = await PA.api(`/api/items/${encodeURIComponent(itemId)}/open`, { json: {} });
      $("chooser").classList.add("hidden");
      $("btnBack").classList.remove("hidden");
      data = resp;
      showProgress(resp.progress);
      setupItem();
      window.scrollTo(0, 0);
    } catch (e) { PA.toast(e.message, true); }
  }

  // Stop playback and timers of the item being left (its work stays saved).
  async function leaveItem() {
    stopTimers();
    if (data) await flushProgress();
    teardownMaterial();
    data = null;
    $("workspace").classList.add("hidden");
    $("submitbar").classList.add("hidden");
    $("done").classList.add("hidden");
  }

  function teardownMaterial() {
    previewing = false;
    if (ws) { try { ws.destroy(); } catch (_) {} ws = null; wsRegions = null; draftRegion = null; }
    ["video", "audio"].forEach(id => { const m = $(id); m.pause(); m.removeAttribute("src"); m.load(); });
    media = null;
    words = [];
    $("textPanel").textContent = "";
    ["mediaArea", "textArea", "mediaBounds", "textBounds", "mediaStatus"].forEach(id => $(id).classList.add("hidden"));
  }

  async function loadCurrent() {
    await leaveItem();
    $("loading").classList.remove("hidden");
    $("workspace").classList.add("hidden");
    $("submitbar").classList.add("hidden");
    let resp;
    try { resp = await PA.api("/api/current"); }
    catch (e) { if (e.status === 401) location.href = "/"; else PA.toast(e.message, true); return; }
    $("loading").classList.add("hidden");
    showProgress(resp.progress);
    if (resp.done) {
      $("done").classList.remove("hidden");
      $("itemName").textContent = "";
      $("doneStats").textContent = `You completed ${resp.progress.completed} item(s).`;
      return;
    }
    $("done").classList.add("hidden");
    data = resp;
    setupItem();
  }

  function showProgress(p) {
    $("progressText").textContent = chooseMode() ? `${p.completed} submitted`
      : `${p.completed} done · ${p.remaining} to go`;
  }

  function setupItem() {
    teardownMaterial();
    modality = data.item.modality;
    editingId = null; textSel = null; previewing = false; pending = []; lastT = null;
    watched = mergeIv(data.assignment.watched_intervals || []);
    scrolledToEnd = data.assignment.scrolled_to_end;
    readingTime = data.assignment.reading_time_s || 0;
    $("itemName").textContent = `· ${data.item.filename || data.item.item_id}`;
    $("noInfo").checked = data.assignment.no_info;
    $("noInfoText").value = data.assignment.no_info_explanation || "";
    $("noInfoText").classList.toggle("hidden", !$("noInfo").checked);
    $("workspace").classList.remove("hidden");
    $("submitbar").classList.remove("hidden");
    if (modality === "text") setupText(); else setupMedia();
    clearEditor();
    restoreDraft();
    renderSegments();
    updateCompletion();
    progressTimer = setInterval(flushProgress, 4000);
  }

  function stopTimers() {
    clearInterval(progressTimer); clearInterval(readTimer);
    progressTimer = readTimer = null;
  }

  // ------------------------------------------------------------- media
  function setupMedia() {
    $("mediaArea").classList.remove("hidden");
    $("mediaBounds").classList.remove("hidden");
    const isAudio = modality === "audio";
    $("videoWrap").classList.toggle("hidden", isAudio);
    $("audioWrap").classList.toggle("hidden", !isAudio);
    $("audioHint").classList.toggle("hidden", !isAudio);
    media = isAudio ? $("audio") : $("video");
    if (ws) { ws.destroy(); ws = null; wsRegions = null; draftRegion = null; }
    if (isAudio && window.WaveSurfer) {
      media.controls = false;
      ws = WaveSurfer.create({
        container: "#waveform", media, url: data.item.media_url, height: 96,
        waveColor: getComputedStyle(document.documentElement).getPropertyValue("--ink-3").trim() || "#999",
        progressColor: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0f6b66",
        cursorColor: "#b3261e", barWidth: 2, barGap: 1, barRadius: 1, normalize: true,
      });
      wsRegions = ws.registerPlugin(WaveSurfer.Regions.create());
      wsRegions.enableDragSelection({ color: "rgba(168, 87, 26, 0.25)" });
      wsRegions.on("region-created", (r) => {
        if (programmatic) return;
        if (draftRegion && draftRegion !== r) draftRegion.remove();
        draftRegion = r;
        setBounds(r.start, r.end, false);
      });
      wsRegions.on("region-updated", (r) => { if (r === draftRegion) setBounds(r.start, r.end, false); });
      ws.on("ready", () => { drawWaveRegions(); renderTimeline(); });
      ws.on("decode", () => drawWaveRegions());
    } else {
      media.src = data.item.media_url;
    }
    media.playbackRate = +$("speed").value;
    const ms = $("mediaStatus");
    ms.textContent = data.item.needs_conversion
      ? "Preparing this file for the browser (converting it once; this can take a minute the first time)…"
      : "Loading…";
    ms.classList.remove("hidden");
    media.onloadeddata = () => ms.classList.add("hidden");
    media.onloadedmetadata = () => { ms.classList.add("hidden"); renderTimeline(); updateCompletion(); updateTime(); flushProgress(); };
    media.onplay = () => { lastT = media.currentTime; $("btnPlay").textContent = "❚❚ Pause"; };
    media.onpause = () => { trackTick(true); lastT = null; $("btnPlay").textContent = "▶ Play"; updateCompletion(); renderTimeline(); flushProgress(); };
    media.onseeking = () => { trackTick(); lastT = null; };
    media.onseeked = () => { if (!media.paused) lastT = media.currentTime; renderTimeline(); updateTime(); };
    media.onended = () => { trackTick(true); lastT = null; updateCompletion(); flushProgress(); };
    media.ontimeupdate = () => {
      if (previewing) {
        const s = PA.parseTime($("startIn").value), e = PA.parseTime($("endIn").value);
        if (media.currentTime >= e || media.currentTime < s - 0.3) { trackTick(); lastT = null; media.currentTime = s; }
      }
      trackTick();
      updateTime();
      renderPlayhead();
      updateCompletion();
    };
    media.onerror = () => {
      if (!media || !media.getAttribute("src")) return;   // ignore errors from teardown
      ms.textContent = "This file could not be loaded or converted. Check that the file still exists (or the link works).";
      ms.classList.remove("hidden");
      PA.toast("The media file could not be loaded.", true);
    };
  }

  // Record what has actually been played (seeking does not count).
  // `final` = called from pause/ended, when the element is already paused
  // but currentTime still marks how far playback got.
  function trackTick(final = false) {
    if (!media || media.seeking || (media.paused && !final)) return;
    const t = media.currentTime;
    if (lastT != null) {
      const d = t - lastT;
      if (d > 0 && d < 1.5 * Math.max(1, media.playbackRate) + 0.5) {
        pending.push([lastT, t]);
        watched = mergeIv(watched.concat([[lastT, t]]));
      }
    }
    lastT = t;
  }

  function updateTime() {
    $("timeText").textContent = `${PA.fmtTime(media.currentTime)} / ${PA.fmtTime(duration())}`;
  }

  function renderTimeline() {
    const tl = $("timeline");
    const d = duration();
    tl.innerHTML = "";
    if (!d) { tl.style.height = "24px"; return; }
    const pct = (t) => `${(Math.max(0, Math.min(t, d)) / d) * 100}%`;
    watched.forEach(([a, b]) => {
      const el = document.createElement("div");
      el.className = "watched"; el.style.left = pct(a); el.style.width = `calc(${pct(b)} - ${pct(a)})`;
      tl.appendChild(el);
    });
    // stack overlapping segments in lanes
    const segs = [...data.segments].sort((x, y) => x.start_s - y.start_s);
    const laneEnds = [];
    const placed = segs.map(s => {
      let lane = laneEnds.findIndex(e => e <= s.start_s);
      if (lane < 0) { lane = laneEnds.length; laneEnds.push(0); }
      laneEnds[lane] = s.end_s;
      return [s, lane];
    });
    const draft = draftBounds();
    const lanes = Math.max(1, laneEnds.length) + (draft ? 1 : 0);
    tl.style.height = `${10 + lanes * 15}px`;
    placed.forEach(([s, lane]) => {
      const el = document.createElement("div");
      el.className = "seg" + (s.id === editingId ? " active" : "");
      el.style.left = pct(s.start_s);
      el.style.width = `max(3px, calc(${pct(s.end_s)} - ${pct(s.start_s)}))`;
      el.style.top = `${8 + lane * 15}px`;
      el.style.background = PA.varColor(cfg, s.ratings[0]?.variable);
      el.title = `${PA.fmtTime(s.start_s)}–${PA.fmtTime(s.end_s)} · ` + s.ratings.map(r => `${PA.varLabel(cfg, r.variable)} ${r.score}`).join(", ");
      el.addEventListener("click", (ev) => { ev.stopPropagation(); editSegment(s.id); });
      tl.appendChild(el);
    });
    if (draft && !editingId) {
      const el = document.createElement("div");
      el.className = "seg draft";
      el.style.left = pct(draft[0]);
      el.style.width = `max(3px, calc(${pct(draft[1])} - ${pct(draft[0])}))`;
      el.style.top = `${8 + (lanes - 1) * 15}px`;
      tl.appendChild(el);
    }
    const ph = document.createElement("div");
    ph.className = "playhead"; ph.id = "playhead";
    tl.appendChild(ph);
    renderPlayhead();
  }

  function renderPlayhead() {
    const ph = $("playhead"), d = duration();
    if (ph && d) ph.style.left = `${(media.currentTime / d) * 100}%`;
  }

  function drawWaveRegions() {
    if (!wsRegions) return;
    wsRegions.getRegions().forEach(r => { if (r !== draftRegion) r.remove(); });
    data.segments.forEach(s => {
      const col = PA.varColor(cfg, s.ratings[0]?.variable);
      programmatic = true;
      const r = wsRegions.addRegion({ start: s.start_s, end: s.end_s, drag: false, resize: false, color: hexA(col, s.id === editingId ? 0.45 : 0.22) });
      programmatic = false;
      r.on("click", (ev) => { ev.stopPropagation(); editSegment(s.id); });
    });
  }
  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16 & 255}, ${n >> 8 & 255}, ${n & 255}, ${a})`;
  }

  function draftBounds() {
    if (modality === "text") return null;
    const s = PA.parseTime($("startIn").value), e = PA.parseTime($("endIn").value);
    return (isFinite(s) && isFinite(e) && e > s) ? [s, e] : null;
  }

  function setBounds(s, e, syncRegion = true) {
    if (s != null) $("startIn").value = PA.fmtTime(s);
    if (e != null) $("endIn").value = PA.fmtTime(e);
    onBoundsChanged(syncRegion);
  }

  function onBoundsChanged(syncRegion = true) {
    const b = draftBounds();
    $("durText").textContent = b ? `Length ${(b[1] - b[0]).toFixed(1)} s` : "";
    if (wsRegions && syncRegion && !editingId) {
      if (b) {
        if (draftRegion) draftRegion.setOptions({ start: b[0], end: b[1] });
        else {
          programmatic = true;
          draftRegion = wsRegions.addRegion({ start: b[0], end: b[1], color: "rgba(168, 87, 26, 0.25)" });
          programmatic = false;
        }
      }
    }
    markEditorActive();
    renderTimeline();
    saveDraft();
  }

  // ------------------------------------------------------------- text
  function setupText() {
    $("textArea").classList.remove("hidden");
    $("textBounds").classList.remove("hidden");
    media = null;
    const panel = $("textPanel");
    words = PA.renderText(panel, data.item.text || "");
    panel.scrollTop = 0;
    panel.onscroll = checkScrolled;
    setTimeout(checkScrolled, 50);
    panel.onmouseup = onTextMouseUp;
    readTimer = setInterval(() => {
      if (document.visibilityState === "visible") { readingTime += 1; updateCompletion(); }
    }, 1000);
    renderHighlights();
  }

  function checkScrolled() {
    const p = $("textPanel");
    if (p.scrollTop + p.clientHeight >= p.scrollHeight - 6) {
      if (!scrolledToEnd) { scrolledToEnd = true; updateCompletion(); flushProgress(); }
    }
  }

  function onTextMouseUp(ev) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) {
      const w = ev.target.closest(".w");
      if (w) showWordPopover(w, ev);
      return;
    }
    const range = sel.getRangeAt(0);
    const hit = words.filter(w => range.intersectsNode(w));
    sel.removeAllRanges();
    if (!hit.length) return;
    const s = +hit[0].dataset.s, e = +hit[hit.length - 1].dataset.e;
    textSel = { s, e };
    updateTextSelection();
  }

  function updateTextSelection() {
    $("selText").textContent = textSel ? `“${data.item.text.slice(textSel.s, textSel.e)}”` : "Select words in the text on the left.";
    renderHighlights();
    markEditorActive();
    saveDraft();
  }

  function renderHighlights() {
    if (modality !== "text") return;
    const segs = data.segments;
    words.forEach(w => {
      const s = +w.dataset.s, e = +w.dataset.e;
      const n = segs.filter(g => g.char_start < e && g.char_end > s).length;
      w.classList.remove("hl1", "hl2", "hl3", "sel", "focus");
      if (n) { w.classList.add(n >= 3 ? "hl3" : n === 2 ? "hl2" : "hl1"); w.dataset.n = n; } else delete w.dataset.n;
      if (textSel && textSel.s < e && textSel.e > s) w.classList.add("sel");
      const ed = editingId && segs.find(g => g.id === editingId);
      if (ed && ed.char_start < e && ed.char_end > s) w.classList.add("focus");
    });
  }

  function showWordPopover(w, ev) {
    closePopover();
    const s = +w.dataset.s, e = +w.dataset.e;
    const segs = data.segments.filter(g => g.char_start < e && g.char_end > s);
    if (!segs.length) return;
    const pop = document.createElement("div");
    pop.className = "popover"; pop.id = "popover";
    pop.innerHTML = `<div class="small muted">${segs.length} segment(s) cover this word</div>`;
    segs.forEach(g => {
      const b = document.createElement("button");
      b.className = "small";
      b.innerHTML = `<span class="quote">“${PA.esc(g.span_text.slice(0, 50))}${g.span_text.length > 50 ? "…" : ""}”</span>&nbsp;` +
        g.ratings.map(r => `<span class="chip"><span class="dot" style="background:${PA.varColor(cfg, r.variable)}"></span>${PA.esc(PA.varLabel(cfg, r.variable))} ${r.score}</span>`).join(" ");
      b.onclick = () => { closePopover(); editSegment(g.id); };
      pop.appendChild(b);
    });
    document.body.appendChild(pop);
    pop.style.left = `${Math.min(ev.clientX, innerWidth - 380)}px`;
    pop.style.top = `${ev.clientY + 12}px`;
    setTimeout(() => document.addEventListener("mousedown", outsidePop), 0);
  }
  function outsidePop(ev) { if (!ev.target.closest("#popover")) closePopover(); }
  function closePopover() { $("popover")?.remove(); document.removeEventListener("mousedown", outsidePop); }

  // ------------------------------------------------------------- ratings editor
  function groupedOptions(selected) {
    const groups = {};
    cfg.variables.forEach(v => (groups[v.group || ""] ||= []).push(v));
    let html = `<option value="">Choose a variable…</option>`;
    Object.entries(groups).forEach(([g, vs]) => {
      const opts = vs.map(v => `<option value="${PA.esc(v.id)}" ${v.id === selected ? "selected" : ""}>${PA.esc(v.label)}</option>`).join("");
      html += g ? `<optgroup label="${PA.esc(g)}">${opts}</optgroup>` : opts;
    });
    return html;
  }

  function addRatingRow(r = {}) {
    const row = document.createElement("div");
    row.className = "rating-row stack";
    const sc = cfg.scale;
    let pips = "";
    for (let v = sc.min; v <= sc.max; v++) {
      const lab = sc.labels[String(v)] || "";
      pips += `<button type="button" class="pip ${r.score == v ? "on" : ""}" data-v="${v}" title="${PA.esc(lab)}">${v}</button>`;
    }
    row.innerHTML = `
      <div class="row" style="flex-wrap:nowrap">
        <select class="var">${groupedOptions(r.variable)}</select>
        <button type="button" class="ghost small danger rm" title="Remove this rating">✕</button>
      </div>
      <div class="def hidden"></div>
      <div>
        <div class="scale"><span class="ends">${PA.esc(sc.labels[String(sc.min)] || "")}</span>${pips}<span class="ends">${PA.esc(sc.labels[String(sc.max)] || "")}</span></div>
        <div class="small muted scorelab" style="min-height:1.2em"></div>
      </div>
      <div><label class="field">Why? (required)</label><textarea class="expl" placeholder="What in this segment shows this?">${PA.esc(r.explanation || "")}</textarea></div>
      <div><label class="field">Notes (optional)</label><input type="text" class="notes" value="${PA.esc(r.notes || "")}"></div>`;
    row.dataset.score = r.score ?? "";
    const showDef = () => {
      const v = cfg.variables.find(x => x.id === row.querySelector(".var").value);
      const d = row.querySelector(".def");
      d.textContent = v?.definition || "";
      d.classList.toggle("hidden", !v?.definition);
    };
    const showScore = () => {
      row.querySelector(".scorelab").textContent = row.dataset.score ? (sc.labels[row.dataset.score] || "") : "";
    };
    row.querySelector(".var").onchange = () => { showDef(); markEditorActive(); saveDraft(); };
    row.querySelectorAll(".pip").forEach(p => p.onclick = () => {
      row.dataset.score = p.dataset.v;
      row.querySelectorAll(".pip").forEach(q => q.classList.toggle("on", q === p));
      showScore(); markEditorActive(); saveDraft();
    });
    row.querySelector(".rm").onclick = () => {
      row.remove();
      if (!$("ratingRows").children.length) addRatingRow();
      saveDraft();
    };
    row.querySelectorAll("textarea, input").forEach(el => el.oninput = () => { markEditorActive(); saveDraft(); });
    $("ratingRows").appendChild(row);
    showDef(); showScore();
    return row;
  }

  function readRatings() {
    return [...$("ratingRows").children].map(row => ({
      variable: row.querySelector(".var").value,
      score: row.dataset.score === "" ? null : +row.dataset.score,
      explanation: row.querySelector(".expl").value,
      notes: row.querySelector(".notes").value,
    }));
  }

  function editorDirty() {
    const r = readRatings();
    return !!(draftBounds() || textSel || r.some(x => x.variable || x.score != null || x.explanation.trim() || x.notes.trim()));
  }

  function markEditorActive() {
    $("editor").classList.toggle("idle", !editingId && !editorDirty());
  }

  function clearEditor() {
    editingId = null; textSel = null; previewing = false;
    $("startIn").value = ""; $("endIn").value = ""; $("durText").textContent = "";
    $("ratingRows").innerHTML = "";
    addRatingRow();
    $("editorTitle").textContent = "New segment";
    $("btnSave").textContent = "Save segment";
    $("btnDelete").classList.add("hidden");
    if (draftRegion) { draftRegion.remove(); draftRegion = null; }
    if (modality === "text") updateTextSelection();
    markEditorActive();
    renderSegments();
  }

  function editSegment(id) {
    const s = data.segments.find(x => x.id === id);
    if (!s) return;
    if (draftRegion) { draftRegion.remove(); draftRegion = null; }
    editingId = id;
    $("ratingRows").innerHTML = "";
    s.ratings.forEach(r => addRatingRow(r));
    $("editorTitle").textContent = "Edit segment";
    $("btnSave").textContent = "Update segment";
    $("btnDelete").classList.remove("hidden");
    if (modality === "text") {
      textSel = { s: s.char_start, e: s.char_end };
      updateTextSelection();
      const w = words.find(w => +w.dataset.s >= s.char_start);
      w?.scrollIntoView({ block: "center", behavior: "smooth" });
    } else {
      $("startIn").value = PA.fmtTime(s.start_s); $("endIn").value = PA.fmtTime(s.end_s);
      $("durText").textContent = `Length ${(s.end_s - s.start_s).toFixed(1)} s`;
      media.currentTime = s.start_s;
    }
    markEditorActive();
    renderSegments();
    $("editor").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  async function saveSegment() {
    const ratings = readRatings();
    const body = { ratings };
    if (modality === "text") {
      if (!textSel) return PA.toast("Select some words in the text first.", true);
      body.char_start = textSel.s; body.char_end = textSel.e;
    } else {
      const b = draftBounds();
      if (!b) return PA.toast("Set a start and an end time (end after start).", true);
      body.start_s = b[0]; body.end_s = b[1];
    }
    for (const r of ratings) {
      if (!r.variable) return PA.toast("Choose a variable for every rating.", true);
      if (r.score == null) return PA.toast(`Choose a score for ${PA.varLabel(cfg, r.variable)}.`, true);
      if (!r.explanation.trim()) return PA.toast(`Explain your rating for ${PA.varLabel(cfg, r.variable)}.`, true);
    }
    const dup = ratings.map(r => r.variable).find((v, i, a) => a.indexOf(v) !== i);
    if (dup) return PA.toast(`${PA.varLabel(cfg, dup)} is rated twice in this segment.`, true);
    try {
      const resp = editingId
        ? await PA.api(`/api/segments/${editingId}`, { method: "PUT", json: body })
        : await PA.api(`/api/assignments/${data.assignment.id}/segments`, { json: body });
      data.segments = resp.segments;
      PA.toast(editingId ? "Segment updated" : "Segment saved");
      localStorage.removeItem(draftKey());
      if (!editingId && $("noInfo").checked) { $("noInfo").checked = false; $("noInfoText").classList.add("hidden"); }
      clearEditor();
      drawWaveRegions();
      renderHighlights();
      updateCompletion();
    } catch (e) { PA.toast(e.message, true); }
  }

  async function deleteSegment() {
    if (!editingId || !confirm("Delete this segment and its ratings?")) return;
    try {
      const resp = await PA.api(`/api/segments/${editingId}`, { method: "DELETE" });
      data.segments = resp.segments;
      clearEditor(); drawWaveRegions(); renderHighlights(); updateCompletion();
      PA.toast("Segment deleted");
    } catch (e) { PA.toast(e.message, true); }
  }

  // ------------------------------------------------------------- drafts (autosave)
  function saveDraft() {
    if (!data || editingId) return;
    const d = { ratings: readRatings(), start: $("startIn").value, end: $("endIn").value, textSel };
    try { localStorage.setItem(draftKey(), JSON.stringify(d)); } catch (_) {}
  }
  function restoreDraft() {
    let d = null;
    try { d = JSON.parse(localStorage.getItem(draftKey()) || "null"); } catch (_) {}
    if (!d) return;
    if (d.ratings?.length) { $("ratingRows").innerHTML = ""; d.ratings.forEach(r => addRatingRow(r)); }
    if (modality === "text") { textSel = d.textSel; updateTextSelection(); }
    else { $("startIn").value = d.start || ""; $("endIn").value = d.end || ""; onBoundsChanged(); }
    markEditorActive();
  }

  // ------------------------------------------------------------- segment list
  function renderSegments() {
    const list = $("segList");
    list.innerHTML = "";
    const segs = [...data.segments];
    $("segCount").textContent = segs.length ? `(${segs.length})` : "";
    $("segEmpty").classList.toggle("hidden", segs.length > 0);
    segs.forEach(s => {
      const li = document.createElement("li");
      li.className = s.id === editingId ? "editing" : "";
      li.style.borderLeftColor = PA.varColor(cfg, s.ratings[0]?.variable);
      const where = modality === "text"
        ? `<div class="quote small">“${PA.esc(s.span_text.length > 120 ? s.span_text.slice(0, 120) + "…" : s.span_text)}”</div>`
        : `<span class="when">${PA.fmtTime(s.start_s)} – ${PA.fmtTime(s.end_s)}</span>`;
      li.innerHTML = `${where}
        <div class="row" style="margin-top:.35rem;gap:.35rem">${s.ratings.map(r =>
          `<span class="chip" title="${PA.esc(r.explanation)}"><span class="dot" style="background:${PA.varColor(cfg, r.variable)}"></span>${PA.esc(PA.varLabel(cfg, r.variable))}: <strong>${r.score}</strong></span>`).join("")}</div>`;
      li.onclick = () => editSegment(s.id);
      list.appendChild(li);
    });
    if (modality !== "text" && media) renderTimeline();
    if (wsRegions) drawWaveRegions();
  }

  // ------------------------------------------------------------- progress & submit
  async function flushProgress() {
    if (!data) return;
    let body;
    if (modality === "text") {
      body = { scrolled_to_end: scrolledToEnd, reading_time_s: readingTime };
    } else {
      trackTick();
      if (!pending.length && data.item.duration_s) return;
      body = { watched_intervals: pending, duration_s: duration() || null };
      pending = [];
    }
    try {
      const r = await PA.api(`/api/assignments/${data.assignment.id}/progress`, { json: body });
      if (modality !== "text") {
        watched = mergeIv(watched.concat(r.watched_intervals));
        if (!data.item.duration_s && duration()) data.item.duration_s = duration();
        renderTimeline();
      }
      updateCompletion();
    } catch (e) {
      if (body.watched_intervals) pending = body.watched_intervals.concat(pending);
      if (e.status === 401) location.href = "/";
    }
  }

  function completionState() {
    const c = cfg.completion;
    if (modality === "text") {
      const okScroll = !c.require_scroll_to_end || scrolledToEnd;
      const okTime = readingTime >= c.min_reading_time_s;
      const parts = [okScroll ? "Read to the end ✓" : "Scroll to the end of the text"];
      if (c.min_reading_time_s > 0) parts.push(`reading ${Math.floor(readingTime)} / ${c.min_reading_time_s} s`);
      return { ok: okScroll && okTime, frac: okScroll ? 1 : Math.min(0.95, ($("textPanel").scrollTop + $("textPanel").clientHeight) / $("textPanel").scrollHeight), text: parts.join(" · ") };
    }
    const d = duration();
    const p = d ? coveredLen(watched, d) / d : 0;
    return { ok: p + 1e-9 >= c.min_watched_proportion, frac: Math.min(1, p / c.min_watched_proportion),
      text: `Played ${Math.floor(p * 100)}% (${Math.round(c.min_watched_proportion * 100)}% needed)` };
  }

  function updateCompletion() {
    if (!data) return;
    const st = completionState();
    $("compText").textContent = st.text;
    $("compBar").style.width = `${Math.round(st.frac * 100)}%`;
    const hasSeg = data.segments.length > 0;
    const noInfoOk = $("noInfo").checked && $("noInfoText").value.trim().length > 0;
    const ready = st.ok && (hasSeg || noInfoOk);
    const pill = $("compPill");
    pill.textContent = !st.ok ? (modality === "text" ? "Keep reading" : "Keep watching")
      : (hasSeg || noInfoOk) ? "Ready to submit" : "Add a segment or tick the box";
    pill.classList.toggle("ok", ready);
    $("btnSubmit").disabled = !ready;
    $("noInfo").disabled = hasSeg;
  }

  async function submit() {
    if (editorDirty() && !confirm("You have an unsaved segment in the editor. Submit without it?")) return;
    if (!confirm("Submit this item? You can't change it afterwards.")) return;
    await flushProgress();
    try {
      await PA.api(`/api/assignments/${data.assignment.id}/submit`, {
        json: { no_info: $("noInfo").checked, no_info_explanation: $("noInfoText").value } });
      localStorage.removeItem(draftKey());
      if (media) { media.pause(); }
      window.scrollTo(0, 0);
      if (chooseMode()) { PA.toast("Submitted"); await showList(); }
      else { PA.toast("Submitted — loading the next item"); await loadCurrent(); }
    } catch (e) { PA.toast(e.message, true); }
  }

  // ------------------------------------------------------------- help
  function buildHelp() {
    const groups = {};
    cfg.variables.forEach(v => (groups[v.group || "Variables"] ||= []).push(v));
    const sc = cfg.scale;
    const scaleTxt = Object.entries(sc.labels).map(([k, v]) => `${k} = ${PA.esc(v)}`).join(" · ");
    $("helpBody").innerHTML = `
      <p style="white-space:pre-wrap">${PA.esc(cfg.instructions)}</p>
      <h3>How to mark a segment</h3>
      <ol class="small">
        ${cfg.modalities.includes("video") || cfg.modalities.includes("audio")
          ? "<li><strong>Video/audio:</strong> play the item. Press <kbd>[</kbd> at the start of a relevant moment and <kbd>]</kbd> at its end (or type the times, or use the Now buttons; for audio you can also drag across the waveform). Use <em>Preview loop</em> to check the segment.</li>" : ""}
        ${cfg.modalities.includes("text") ? "<li><strong>Text:</strong> select the words with your mouse.</li>" : ""}
        <li>Choose a variable, a score and explain your rating. Add more variables if the segment shows more than one.</li>
        <li>Click <em>Save segment</em>. Segments may overlap.</li>
        <li>When you have played the whole item (or read the whole text), click <em>Submit item</em>.</li>
      </ol>
      <h3>Rating scale</h3><p class="small">${sc.min}–${sc.max}${scaleTxt ? ": " + scaleTxt : ""}</p>
      <h3>Variables</h3>
      ${Object.entries(groups).map(([g, vs]) => `<p class="small" style="margin:.6rem 0 .2rem"><strong>${PA.esc(g)}</strong></p>` +
        vs.map(v => `<p class="small" style="margin:.2rem 0"><span class="chip"><span class="dot" style="background:${PA.varColor(cfg, v.id)}"></span>${PA.esc(v.label)}</span> ${PA.esc(v.definition)}</p>`).join("")).join("")}`;
  }

  // ------------------------------------------------------------- bindings
  function bindStatic() {
    $("btnBack").onclick = async () => {
      if (editorDirty() && !editingId && !confirm("Leave this item? Your unsaved segment form is kept as a draft, and saved segments stay.")) return;
      await showList();
    };
    $("statusFilter").onchange = renderList;
    $("search").oninput = renderList;
    $("btnHelp").onclick = () => $("helpModal").classList.remove("hidden");
    $("btnHelpClose").onclick = () => $("helpModal").classList.add("hidden");
    $("helpModal").onclick = (e) => { if (e.target.id === "helpModal") $("helpModal").classList.add("hidden"); };
    $("btnLogout").onclick = async () => { await flushProgress(); await PA.api("/api/logout", { json: {} }); location.href = "/"; };
    $("btnPlay").onclick = () => { previewing = false; media.paused ? media.play() : media.pause(); };
    document.querySelectorAll("[data-step]").forEach(b => b.onclick = () => step(+b.dataset.step));
    $("speed").onchange = () => { if (media) media.playbackRate = +$("speed").value; };
    $("timeline").onclick = (e) => {
      const d = duration(); if (!d) return;
      const r = $("timeline").getBoundingClientRect();
      media.currentTime = ((e.clientX - r.left) / r.width) * d;
    };
    $("btnSetStart").onclick = () => setBounds(media.currentTime, null);
    $("btnSetEnd").onclick = () => setBounds(null, media.currentTime);
    $("startIn").onchange = $("endIn").onchange = () => onBoundsChanged();
    $("btnPreview").onclick = togglePreview;
    $("btnAddRating").onclick = () => addRatingRow();
    $("btnSave").onclick = saveSegment;
    $("btnDelete").onclick = deleteSegment;
    $("btnCancel").onclick = () => { localStorage.removeItem(draftKey()); clearEditor(); };
    $("noInfo").onchange = () => { $("noInfoText").classList.toggle("hidden", !$("noInfo").checked); updateCompletion(); };
    $("noInfoText").oninput = updateCompletion;
    $("btnSubmit").onclick = submit;
    document.addEventListener("keydown", onKey);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushProgress(); });
    window.addEventListener("pagehide", () => {
      if (!data) return;
      if (media) trackTick();
      const body = modality === "text" ? { scrolled_to_end: scrolledToEnd, reading_time_s: readingTime }
        : { watched_intervals: pending, duration_s: duration() || null };
      fetch(`/api/assignments/${data.assignment.id}/progress`, { method: "POST", keepalive: true,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "same-origin" });
    });
  }

  function step(dt) {
    if (!media) return;
    media.currentTime = Math.max(0, Math.min(duration() || 1e9, media.currentTime + dt));
  }

  function togglePreview() {
    const b = draftBounds();
    if (!b) return PA.toast("Set a start and an end first.", true);
    if (previewing) { previewing = false; media.pause(); return; }
    previewing = true;
    media.currentTime = b[0];
    media.play();
  }

  function onKey(e) {
    if (e.target.closest("input, textarea, select") || !media || $("helpModal").offsetParent) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === " ") { e.preventDefault(); $("btnPlay").click(); }
    else if (k === "ArrowLeft") { e.preventDefault(); step(e.shiftKey ? -0.1 : -1); }
    else if (k === "ArrowRight") { e.preventDefault(); step(e.shiftKey ? 0.1 : 1); }
    else if (k === "[") setBounds(media.currentTime, null);
    else if (k === "]") setBounds(null, media.currentTime);
    else if (k === "p" || k === "P") togglePreview();
  }

  init();
})();
