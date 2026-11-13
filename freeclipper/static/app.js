// Shared helpers for every page.
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.form) {
    init.body = opts.form;
  } else if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    const err = new Error("Can't reach the server. Check your connection and try again.");
    err.network = true;
    throw err;
  }
  if (res.status === 401 && !path.startsWith("/api/login")) {
    location.href = "/login";  // session expired: sign in again
    throw new Error("Please sign in again.");
  }
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    const detail = data && typeof data === "object" ? data.detail : null;
    const err = new Error(typeof detail === "string" ? detail :
      res.status >= 500 ? "The server had a problem. Please try again in a moment." : res.statusText || "Request failed");
    err.status = res.status;
    throw err;
  }
  return data;
}

// Run an action from a button: disables it while running (no double submits), optionally shows a busy
// label, and reports errors. Returns the action's result, or undefined if it failed.
async function withBusy(btn, fn, busyText) {
  if (btn && btn.disabled) return undefined;
  const label = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    if (busyText) btn.textContent = busyText;
  }
  try {
    return await fn();
  } catch (e) {
    toast(e.message || "Something went wrong.", true);
    return undefined;
  } finally {
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      if (busyText) btn.textContent = label;
    }
  }
}

function toast(msg, isError = false) {
  let stack = document.getElementById("toasts");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "toasts";
    stack.setAttribute("role", "status");
    stack.setAttribute("aria-live", "polite");
    document.body.appendChild(stack);
  }
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  stack.appendChild(el);
  while (stack.children.length > 3) stack.firstElementChild.remove();
  setTimeout(() => el.remove(), isError ? 7000 : 3500);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtPrecise(sec) {
  const r = Math.round((Number(sec) || 0) * 10) / 10;
  return fmtTime(Math.floor(r)) + "." + Math.round((r % 1) * 10);
}

function parseTime(text) {
  const parts = String(text).trim().split(":").map(Number);
  if (parts.some(isNaN)) return NaN;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
}

function modal(html) {
  const wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML = `<div class="card">${html}</div>`;
  wrap.addEventListener("click", e => { if (e.target === wrap) wrap.remove(); });
  document.body.appendChild(wrap);
  return wrap;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { toast("Copy failed", true); }
}
