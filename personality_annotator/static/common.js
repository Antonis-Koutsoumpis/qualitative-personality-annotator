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

  return { api, fmtTime, parseTime, toast, esc, varColor, varLabel, tokenize, renderText, download, PALETTE };
})();
