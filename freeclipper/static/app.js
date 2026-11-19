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

// "in 2 days 5 h" style countdown to a project's automatic deletion.
function untilText(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return "soon";
  const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60;
  if (h >= 48) return `in ${Math.floor(h / 24)} days`;
  if (h >= 1) return `in ${h} h ${m} min`;
  return `in ${Math.max(1, m)} min`;
}
function retentionText(hours) {
  return hours % 24 === 0 ? `${hours / 24} day${hours === 24 ? "" : "s"}` : `${hours} hours`;
}

// Large, colour-coded virality score for a clip card.
function scoreBadge(score) {
  const s = Math.max(0, Math.min(100, Math.round(+score || 0)));
  const tier = s >= 85 ? "hot" : s >= 70 ? "good" : "ok";
  const label = s >= 85 ? "High viral potential" : s >= 70 ? "Good viral potential" : "Some viral potential";
  return `<div class="vscore ${tier}" title="Virality score ${s} out of 100: ${label}" aria-label="Virality score ${s} out of 100">
    <span class="num">${s}</span><span class="of">/100</span><span class="lbl">${label}</span>
    <span class="meter" aria-hidden="true"><span style="width:${s}%"></span></span></div>`;
}

// ---- Share a clip to TikTok, Instagram, YouTube... ----
// Phones (and some desktops) get the system share sheet with the video attached: pick the app and post.
// Elsewhere a panel downloads the clip and opens each platform's upload page. The title and hashtags are
// copied first, so the user just pastes them as the caption.
const UPLOAD_PAGES = [
  ["TikTok", "https://www.tiktok.com/upload"],
  ["YouTube Shorts", "https://studio.youtube.com/"],
  ["Instagram Reels", "https://www.instagram.com/"],
];
const _shareFiles = new Map();  // clip id -> prepared File (so a second tap shares instantly)
function postText(c) { return `${c.title}\n\n${(c.hashtags || []).join(" ")}`.trim(); }
function clipFileName(c) {
  const base = (c.title || "clip").replace(/[“”"]/g, "").replace(/[^\w\s-]+/g, "").trim()
    .replace(/\s+/g, "-").slice(0, 60) || "clip";
  return base + ".mp4";
}
async function shareClip(c, btn) {
  const text = postText(c);
  try { await navigator.clipboard.writeText(text); } catch (_) {}  // caption ready to paste
  const canShareFiles = !!(navigator.canShare && window.File);
  if (!canShareFiles) return sharePanel(c);
  let file = _shareFiles.get(c.id);
  if (!file) {
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Preparing…";
    try {
      const r = await fetch(c.files.video, { credentials: "same-origin" });
      if (!r.ok) throw new Error();
      file = new File([await r.blob()], clipFileName(c), { type: "video/mp4" });
      _shareFiles.set(c.id, file);
    } catch (_) {
      btn.disabled = false; btn.textContent = label;
      return toast("Couldn't load the clip. Check your connection and try again.", true);
    }
    btn.disabled = false; btn.textContent = label;
  }
  if (!navigator.canShare({ files: [file] })) return sharePanel(c);
  try {
    await navigator.share({ files: [file], title: c.title, text });
    toast("Caption copied: paste it in the app before posting");
  } catch (err) {
    if (err && err.name === "AbortError") return;  // the user closed the share sheet
    if (err && err.name === "NotAllowedError") {
      // Some browsers only open the share sheet straight from a tap; the clip is ready now.
      btn.textContent = "Tap to share";
      return toast("Clip ready: tap Share again");
    }
    sharePanel(c);
  }
}
function sharePanel(c) {
  const m = modal(`<h2>Post this clip</h2>
    <p class="small muted">The title and hashtags are copied. Download the clip, open the app, upload it and paste the caption.</p>
    <div class="field"><label>Caption</label><textarea id="sharetext" rows="3" readonly>${esc(postText(c))}</textarea></div>
    <div class="row" style="margin-bottom:12px">
      <a class="btn primary" href="${c.files.video}&download=1">Download MP4</a>
      <button id="sharecopy">Copy caption</button>
    </div>
    <div class="share-targets">${UPLOAD_PAGES.map(([name, url]) =>
      `<a class="btn" href="${url}" target="_blank" rel="noopener">Open ${name}</a>`).join("")}</div>
    <p class="small muted" style="margin-top:12px">On a phone, the Share button sends the video straight to the app instead.</p>
    <div class="row" style="margin-top:8px"><button id="shareclose">Close</button></div>`);
  m.querySelector("#sharecopy").onclick = () => copyText(postText(c));
  m.querySelector("#shareclose").onclick = () => m.remove();
}

// Reframing menus list each streamer layout once per format ("streamer@9:16", "streamer@1:1"): picking one
// also sets the Format menu, and changing the format switches to the matching streamer entry.
function reframeValue(sel) { return sel.value.split("@")[0]; }
function setReframe(sel, reframe, aspect) {
  const opts = [...sel.options];
  const pick = opts.find(o => o.value === `${reframe}@${aspect}`) || opts.find(o => o.value === reframe)
    || opts.find(o => o.value.startsWith(reframe + "@"));
  if (pick) sel.value = pick.value;
}
function linkReframe(reframeSel, aspectSel) {
  reframeSel.addEventListener("change", () => {
    const aspect = reframeSel.value.split("@")[1];
    if (aspect) { aspectSel.value = aspect; aspectSel.dataset.touched = 1; }
  });
  aspectSel.addEventListener("change", () => {
    const [mode, current] = reframeSel.value.split("@");
    if (current && current !== aspectSel.value) setReframe(reframeSel, mode, aspectSel.value);
  });
}
