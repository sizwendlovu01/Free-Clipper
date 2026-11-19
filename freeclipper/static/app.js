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

// ---- Layout picker: picture cards for the Reframing menu ----
// The <select> stays as the real setting (hidden); the cards set it and fire "change", so everything that
// listens to the menu keeps working. Hover a card (computer) or tap it (phone) to see a larger preview.
const LAYOUT_INFO = {
  "auto": ["Follow faces", "Keeps the speaker's face in frame as they move. Best for podcasts, interviews and talking heads."],
  "streamer@9:16": ["Streamer, webcam top-left", "Vertical 9:16. Most of the gameplay with the streamer's webcam in the top-left corner; caption under the webcam."],
  "streamer_bottom@9:16": ["Streamer, webcam bottom-left", "Vertical 9:16. Most of the gameplay with the streamer's webcam in the bottom-left corner; caption above the webcam."],
  "streamer@1:1": ["Streamer, webcam top-left", "Square 1:1. Gameplay fills the frame with the webcam in the top-left corner; caption on the webcam's bottom edge."],
  "streamer_bottom@1:1": ["Streamer, webcam bottom-left", "Square 1:1. Gameplay fills the frame with the webcam in the bottom-left corner; caption just above it."],
  "center": ["Center crop", "Crops the middle of the video to fill the frame."],
  "fit_blur": ["Blurred background", "Shows the whole video, with a soft blur filling the space around it."],
  "fit_black": ["Black bars", "Shows the whole video, with black bars above and below."],
};

function layoutArt(value) {
  const square = value.endsWith("@1:1");
  const W = 90, H = square ? 90 : 160;
  const game = (x, y, w, h) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="url(#lpGame)"/>
    <path d="M${x} ${y + h * 0.72} L${x + w * 0.28} ${y + h * 0.45} L${x + w * 0.5} ${y + h * 0.62} L${x + w * 0.75} ${y + h * 0.38} L${x + w} ${y + h * 0.6} L${x + w} ${y + h} L${x} ${y + h} Z" fill="var(--lp-hill)"/>
    <circle cx="${x + w * 0.5}" cy="${y + h * 0.5}" r="${Math.min(w, h) * 0.07}" fill="var(--lp-hero)"/>`;
  const person = (cx, cy, s) => `<circle cx="${cx}" cy="${cy - s * 0.25}" r="${s * 0.22}" fill="var(--lp-face)"/>
    <path d="M${cx - s * 0.42} ${cy + s * 0.55} Q${cx} ${cy - s * 0.1} ${cx + s * 0.42} ${cy + s * 0.55} Z" fill="var(--lp-face)"/>`;
  const cam = (x, y, w, h) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="var(--lp-cam)" stroke="var(--lp-line)" stroke-width="0.8"/>${person(x + w * 0.55, y + h * 0.55, h * 0.75)}`;
  const caption = (cx, y, w) => `<rect x="${cx - w / 2}" y="${y}" width="${w}" height="9" rx="2" fill="#000" opacity=".55"/>
    <rect x="${cx - w / 2 + 4}" y="${y + 3}" width="${w * 0.42}" height="3" rx="1.5" fill="#fff"/>
    <rect x="${cx + 2}" y="${y + 3}" width="${w * 0.36}" height="3" rx="1.5" fill="#ffe600"/>`;
  const blur = `<rect width="${W}" height="${H}" fill="url(#lpBlur)"/>`;
  let body = "";
  const [mode] = value.split("@");
  if (mode === "auto") {
    body = `<rect width="${W}" height="${H}" fill="var(--lp-room)"/>${person(W / 2, H * 0.5, 44)}${caption(W / 2, H * 0.78, 54)}`;
  } else if (mode === "center") {
    body = game(-55, 0, 200, H) + caption(W / 2, H * 0.78, 54);
  } else if (mode === "fit_blur") {
    body = blur + game(0, H / 2 - 26, W, 51) + caption(W / 2, H * 0.78, 54);
  } else if (mode === "fit_black") {
    body = `<rect width="${W}" height="${H}" fill="#000"/>` + game(0, H / 2 - 26, W, 51) + caption(W / 2, H * 0.78, 54);
  } else {  // streamer layouts
    const top = mode === "streamer";
    const bw = W * 0.42, bh = bw / 1.2;
    const boxY = top ? 0 : H - bh;
    const capY = top ? bh - 5 : H - bh - 12;
    body = square
      ? blur + game(0, 6, W, H - 12) + cam(0, boxY, bw, bh) + caption(W / 2, capY, 50)
      : blur + game(0, H / 2 - 30, W, 60) + cam(0, boxY, bw, bh) + caption(W / 2, capY, 50);
  }
  return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <defs><linearGradient id="lpGame" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--lp-sky1)"/><stop offset="1" stop-color="var(--lp-sky2)"/></linearGradient>
    <linearGradient id="lpBlur" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="var(--lp-blur1)"/><stop offset="1" stop-color="var(--lp-blur2)"/></linearGradient></defs>
    <rect width="${W}" height="${H}" fill="var(--lp-room)"/>${body}</svg>`;
}

function layoutPicker(select) {
  if (!select || select.dataset.picker) return;
  select.dataset.picker = "1";
  const field = select.closest("div");
  field.classList.add("layout-field");
  select.classList.add("visually-hidden");
  const wrap = document.createElement("div");
  wrap.className = "layout-picker";
  const values = [...select.options].map(o => o.value).filter(v => LAYOUT_INFO[v]);
  const card = v => {
    const [name] = LAYOUT_INFO[v];
    const fmt = v.includes("@") ? `<span class="lp-fmt">${v.split("@")[1]}</span>` : "";
    return `<button type="button" class="lp-card" role="radio" data-value="${v}" aria-label="${name}${v.includes("@") ? ", " + v.split("@")[1] : ""}">
      <span class="lp-thumb ${v.endsWith("@1:1") ? "square" : ""}">${layoutArt(v)}</span><span class="lp-name">${name}</span>${fmt}</button>`;
  };
  wrap.innerHTML = `<div class="lp-cards" role="radiogroup" aria-label="Reframing">${values.map(card).join("")}</div>
    <div class="lp-preview" aria-live="polite"><div class="lp-big"></div><div><b class="lp-title"></b><p class="lp-desc small muted"></p>
    <p class="lp-hint small muted">Hover a layout to preview it, or tap it on a phone.</p></div></div>`;
  field.appendChild(wrap);
  const big = wrap.querySelector(".lp-big"), title = wrap.querySelector(".lp-title"), desc = wrap.querySelector(".lp-desc");
  const show = v => {
    const [name, text] = LAYOUT_INFO[v] || ["", ""];
    big.className = "lp-big" + (v.endsWith("@1:1") ? " square" : "");
    big.innerHTML = layoutArt(v);
    title.textContent = name + (v.includes("@") ? ` (${v.split("@")[1]})` : "");
    desc.textContent = text;
  };
  const mark = () => {
    wrap.querySelectorAll(".lp-card").forEach(b => {
      const on = b.dataset.value === select.value;
      b.classList.toggle("on", on); b.setAttribute("aria-checked", on);
    });
    show(select.value);
  };
  wrap.querySelectorAll(".lp-card").forEach(b => {
    b.addEventListener("click", () => {
      select.value = b.dataset.value;
      select.dataset.touched = 1;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      select.dispatchEvent(new Event("input", { bubbles: true }));
      mark();
    });
    b.addEventListener("mouseenter", () => show(b.dataset.value));
    b.addEventListener("focus", () => show(b.dataset.value));
    b.addEventListener("mouseleave", () => show(select.value));
    b.addEventListener("blur", () => show(select.value));
  });
  select.addEventListener("change", mark);
  mark();
  return { refresh: mark };
}
