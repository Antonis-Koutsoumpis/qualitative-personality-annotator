// Shared helpers for the annotator and admin pages.
const PA = (() => {
  async function api(path, opts = {}) {
    const init = { credentials: "same-origin", ...opts };
    if (opts.json !== undefined) {
      init.method = opts.method || "POST";
      init.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
      init.body = JSON.stringify(opts.json);
    }
    const res = await fetch(path, init);
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
      const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
      err.status = res.status;
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res;
  }

  function fmtTime(t) {
    if (t == null || isNaN(t)) return "--:--.-";
    t = Math.max(0, t);
    const m = Math.floor(t / 60), s = t - m * 60;
    return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
  }

  function parseTime(str) {
    str = String(str).trim();
    if (!str) return NaN;
    const parts = str.split(":").map(Number);
    if (parts.some(isNaN)) return NaN;
    return parts.reduce((acc, p) => acc * 60 + p, 0);
  }

  let toastTimer;
  function toast(msg, isError = false) {
    let el = document.querySelector(".toast");
    if (!el) { el = document.createElement("div"); el.className = "toast"; document.body.appendChild(el); }
    el.textContent = msg;
    el.classList.toggle("error", isError);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 5000 : 2200);
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Categorical colours for variables (stable by index).
  const PALETTE = ["#0f7c74", "#c25a1d", "#4561b3", "#a63b6b", "#6b8e23", "#8a5ac2", "#b08a12", "#2f8fbf", "#9b4a3a", "#3f7a52"];
  function varColor(cfg, varId) {
    const i = cfg.variables.findIndex(v => v.id === varId);
    return PALETTE[(i < 0 ? 0 : i) % PALETTE.length];
  }
  function varLabel(cfg, varId) {
    const v = cfg.variables.find(v => v.id === varId);
    return v ? v.label : varId;
  }

  // Word tokens; must match analysis.tokenize on the server (\S+).
  function tokenize(text) {
    const out = [];
    const re = /\S+/g;
    let m;
    while ((m = re.exec(text))) out.push([m.index, m.index + m[0].length, m[0]]);
    return out;
  }

  // Render text into a container as word spans + whitespace text nodes.
  function renderText(container, text) {
    container.textContent = "";
    const words = [];
    const re = /\S+|\s+/g;
    let m, i = 0;
    const frag = document.createDocumentFragment();
    while ((m = re.exec(text))) {
      if (/\S/.test(m[0][0])) {
        const sp = document.createElement("span");
        sp.className = "w";
        sp.textContent = m[0];
        sp.dataset.i = i; sp.dataset.s = m.index; sp.dataset.e = m.index + m[0].length;
        frag.appendChild(sp);
        words.push(sp);
        i++;
      } else {
        frag.appendChild(document.createTextNode(m[0]));
      }
    }
    container.appendChild(frag);
    return words;
  }

  async function download(path, opts) {
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    const cd = res.headers.get("content-disposition") || "";
    const name = (cd.match(/filename="?([^"]+)"?/) || [])[1] || "download";
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  const TYPE_LABEL = { video: "Video", audio: "Audio", text: "Text" };
  const TYPE_ICON = { video: "▶", audio: "♪", text: "¶" };

  const FORMATS_HTML = `
    <strong>Video</strong>: .mp4 .m4v .webm .ogv play directly; .avi .mov .mkv .wmv .mpg .mpeg .flv .3gp .mts .ts are converted automatically the first time they are opened.<br>
    <strong>Audio</strong>: .mp3 .wav .m4a .aac .ogg .opus .flac play directly; .wma .aif .aiff .amr are converted.<br>
    <strong>Text</strong>: .txt .md, and text is read from .docx .pdf .odt .rtf .html (scanned PDFs without a text layer can't be read).`;

  // "Add files" dialog shared by the annotator and admin pages.
  // onDone(report) is called after files were added.
  function addFilesDialog(onDone, { isLocal = true, asAdmin = false } = {}) {
    let bg = document.getElementById("addFilesModal");
    if (bg) bg.remove();
    bg = document.createElement("div");
    bg.className = "modal-bg"; bg.id = "addFilesModal";
    bg.innerHTML = `
      <div class="card modal stack">
        <div class="row" style="justify-content:space-between"><h1 style="margin:0">Add files</h1>
          <button class="ghost" data-close>Close ✕</button></div>
        <div class="tabs">
          <button data-pane="local" class="on">From this computer</button>
          <button data-pane="link">From a link</button>
        </div>
        <div data-panel="local" class="stack">
          ${isLocal ? `<p class="small muted" style="margin:0">Choose a folder (every supported file in it is added) or individual files. The files stay where they are; the tool only reads them.</p>
          <div class="row"><button class="primary" data-pick="folder">Choose a folder…</button><button data-pick="files">Choose files…</button></div>
          <div><label class="field">…or paste the path of a folder or file</label>
            <div class="row" style="flex-wrap:nowrap"><input type="text" data-path placeholder="C:\\Users\\you\\Documents\\interviews"><button data-add-path>Add</button></div></div>
          <label class="check small"><input type="checkbox" data-recursive> include sub-folders</label>`
          : `<p class="small">Files on a computer can only be added on the computer that runs the tool. Use a link instead.</p>`}
        </div>
        <div data-panel="link" class="stack hidden">
          <div><label class="field">Zenodo record or direct link to a file</label>
            <div class="row" style="flex-wrap:nowrap"><input type="text" data-url placeholder="https://zenodo.org/records/1234567  or  https://…/interview_01.mp4"><button class="primary" data-add-url>Add</button></div></div>
          <p class="small muted" style="margin:0">A Zenodo record link adds every supported file in the record (public records only). Any other link must point directly to one file.</p>
        </div>
        <div data-result class="small hidden"></div>
        <details class="small muted"><summary>Supported formats</summary><p>${FORMATS_HTML}</p></details>
      </div>`;
    document.body.appendChild(bg);
    const q = (sel) => bg.querySelector(sel);
    const close = () => bg.remove();
    q("[data-close]").onclick = close;
    bg.onclick = (e) => { if (e.target === bg) close(); };
    bg.querySelectorAll("[data-pane]").forEach(b => b.onclick = () => {
      bg.querySelectorAll("[data-pane]").forEach(x => x.classList.toggle("on", x === b));
      bg.querySelectorAll("[data-panel]").forEach(p => p.classList.toggle("hidden", p.dataset.panel !== b.dataset.pane));
    });
    const busy = (on) => bg.querySelectorAll("button").forEach(b => { if (!b.dataset.close) b.disabled = on; });
    const showResult = (html, isErr) => {
      const r = q("[data-result]");
      r.innerHTML = html; r.classList.remove("hidden");
      r.style.color = isErr ? "var(--danger)" : "";
    };
    async function add(bodies) {
      busy(true);
      showResult("Adding… (large folders or Zenodo records can take a moment)");
      let added = 0, found = 0, lastReport = null;
      const errs = [];
      for (const body of bodies) {
        try {
          const r = await api("/api/sources", { json: { ...body, as_admin: asAdmin } });
          lastReport = r.report;
          added += r.report.added; found += r.report.source_files;
          errs.push(...(r.report.source_file_errors || []));
        } catch (e) { errs.push(e.message); }
      }
      busy(false);
      if (lastReport) {
        showResult(`<strong>${found} file(s) found, ${added} new item(s) added.</strong>` +
          (errs.length ? `<br><span style="color:var(--danger)">${errs.map(esc).join("<br>")}</span>` : ""));
        onDone && onDone(lastReport);
        if (!errs.length) setTimeout(close, 1400);
      } else {
        showResult(errs.map(esc).join("<br>"), true);
      }
    }
    if (isLocal) {
      bg.querySelectorAll("[data-pick]").forEach(b => b.onclick = async () => {
        busy(true);
        showResult("A file dialog has opened (it may be behind this browser window)…");
        let paths = [];
        try { paths = (await api("/api/pick", { json: { kind: b.dataset.pick, as_admin: asAdmin } })).paths; }
        catch (e) { busy(false); showResult(esc(e.message), true); return; }
        busy(false);
        if (!paths.length) { showResult("Nothing chosen."); return; }
        const rec = q("[data-recursive]").checked;
        add(paths.map(p => ({ type: "local", path: p, recursive: rec })));
      });
      q("[data-add-path]").onclick = () => {
        const p = q("[data-path]").value.trim();
        if (!p) return showResult("Paste a folder or file path first.", true);
        add([{ type: "local", path: p, recursive: q("[data-recursive]").checked }]);
      };
      q("[data-path]").onkeydown = (e) => { if (e.key === "Enter") q("[data-add-path]").click(); };
    } else {
      bg.querySelector('[data-pane="link"]').click();
    }
    q("[data-add-url]").onclick = () => {
      const u = q("[data-url]").value.trim();
      if (!u) return showResult("Paste a link first.", true);
      add([{ type: "url", url: u }]);
    };
    q("[data-url]").onkeydown = (e) => { if (e.key === "Enter") q("[data-add-url]").click(); };
  }

  return { api, fmtTime, parseTime, toast, esc, varColor, varLabel, tokenize, renderText, download, PALETTE,
           TYPE_LABEL, TYPE_ICON, FORMATS_HTML, addFilesDialog };
})();
