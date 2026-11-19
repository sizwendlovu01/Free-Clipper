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

// ---- Auto-post: send a clip to connected TikTok, Instagram and YouTube accounts ----
// Each ticked account gets its own caption (different wording per account, so it doesn't look like spam).
// The TikTok section follows TikTok's posting rules: who-can-see has no default, interactions start off,
// commercial content is disclosed, and the Music Usage Confirmation is shown before posting.
const TT_PRIVACY = { PUBLIC_TO_EVERYONE: "Everyone", MUTUAL_FOLLOW_FRIENDS: "Friends", FOLLOWER_OF_CREATOR: "Followers", SELF_ONLY: "Only me" };
const PLATFORM_ICON = { tiktok: "♪", instagram: "◎", youtube: "▶" };
const MUSIC_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en";
const BC_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en";

function acctName(a) { return a.username ? "@" + a.username : a.display_name; }
function acctAvatar(a, size = 28) {
  return a.avatar_url
    ? `<img class="acct-av" src="${esc(a.avatar_url)}" alt="" width="${size}" height="${size}" referrerpolicy="no-referrer" loading="lazy">`
    : `<span class="acct-av ph" style="width:${size}px;height:${size}px">${PLATFORM_ICON[a.platform] || "•"}</span>`;
}

async function autoPost(c) {
  let data;
  try { data = await api("/api/social/accounts"); } catch (e) { return toast(e.message, true); }
  const accounts = data.accounts;
  if (!accounts.length) {
    const m = modal(`<h2>Auto-post this clip</h2>
      <p>Connect your TikTok, Instagram or YouTube accounts first. You can connect as many as you like.</p>
      <div class="row"><a class="btn primary" href="/settings#accounts">Connect accounts</a><button id="apclose">Close</button></div>`);
    m.querySelector("#apclose").onclick = () => m.remove();
    return;
  }
  // Accounts this clip already went to (or is waiting for) can't be picked again.
  const already = new Map();
  try {
    for (const p of (await api(`/api/posts?clip_id=${c.id}`)).posts)
      if (["queued", "posting", "posted"].includes(p.status) && !already.has(p.account_id))
        already.set(p.account_id, p.status === "posted" ? "already posted" : "waiting to post");
  } catch (_) {}
  const note = a => a.status !== "ok" ? "reconnect in Settings" : already.get(a.id) || `${a.posts_today}/${a.daily_limit} today`;
  const off = a => a.status !== "ok" || already.has(a.id);
  const m = modal(`<h2>Auto-post this clip</h2>
    <div class="ap-top"><video src="${c.files.video}" muted playsinline controls preload="metadata" poster="${c.files.thumb}"></video>
      <div><p class="small muted" style="margin-top:0">Tick the accounts to post to. Each one gets its own caption, which you can edit.</p>
        <div class="ap-accounts">${accounts.map(a => `<label class="check ap-acct${off(a) ? " off" : ""}">
          <input type="checkbox" data-acct="${a.id}" ${off(a) ? "disabled" : ""}>${acctAvatar(a)}
          <span><b>${esc(acctName(a))}</b><br><span class="small muted">${esc(a.platform_label)} · ${esc(note(a))}</span></span></label>`).join("")}</div>
        <p class="small"><a href="/settings#accounts">Manage accounts</a></p></div></div>
    <div id="ap-targets"></div>
    <div class="field"><label for="ap-spacing">When</label><select id="ap-spacing">
      <option value="0">Post to all now</option><option value="15" selected>One account every 15 minutes</option>
      <option value="30">One account every 30 minutes</option><option value="60">One account every hour</option></select>
      <p class="small muted" style="margin:6px 0 0">Spacing posts out looks more natural to the platforms. After a clip is sent it can take a few minutes to appear.</p></div>
    <div class="row" style="margin-top:14px"><button class="primary" id="apgo" disabled>Post</button><button id="apcancel">Cancel</button>
      <span class="small muted" id="aphint"></span></div>`);
  m.querySelector(".card").classList.add("wide");
  m.querySelector("#apcancel").onclick = () => m.remove();
  const box = m.querySelector("#ap-targets");
  const state = new Map();  // account id -> { account, el, info }
  const byId = new Map(accounts.map(a => [a.id, a]));

  async function sync() {
    const ticked = [...m.querySelectorAll("[data-acct]:checked")].map(i => +i.dataset.acct);
    for (const id of [...state.keys()]) if (!ticked.includes(id)) { state.get(id).el.remove(); state.delete(id); }
    const added = ticked.filter(id => !state.has(id));
    for (const id of added) {
      const el = document.createElement("div");
      el.className = "ap-target";
      el.innerHTML = `<p class="small muted">Loading ${esc(acctName(byId.get(id)))}…</p>`;
      box.appendChild(el);
      state.set(id, { account: byId.get(id), el, info: null, ready: false });
    }
    if (added.length) {
      // Ask for captions for every ticked account in order, so each new one gets a different variant.
      let variants = [];
      try { variants = (await api(`/api/clips/${c.id}/post-text?accounts=${ticked.join(",")}`)).variants; }
      catch (e) { toast(e.message, true); }
      for (const id of added) {
        const s = state.get(id);
        if (!s) continue;
        const v = variants.find(x => x.account_id === id) || { title: c.title, caption: postText(c) };
        if (s.account.platform === "tiktok") {
          try { s.info = await api(`/api/social/accounts/${id}/tiktok`); }
          catch (e) { s.el.innerHTML = `<p class="small" style="color:var(--danger)">${esc(acctName(s.account))}: ${esc(e.message)}</p>`; s.error = true; continue; }
        }
        s.el.innerHTML = targetForm(s, v, c);
        s.ready = true;
        wire(s);
      }
    }
    check();
  }

  function check() {
    let problem = "";
    const list = [...state.values()];
    if (!list.length) problem = "Tick at least one account.";
    for (const s of list) {
      if (problem) break;
      const name = acctName(s.account);
      if (s.error) problem = `Untick ${name}: it can't post right now.`;
      else if (!s.ready) problem = "Loading…";
      else if (s.tooLong) problem = `${name} can't post a clip this long.`;
      else if (s.account.platform === "tiktok") {
        if (!s.el.querySelector(".tt-privacy").value) problem = `Choose who can see the TikTok post on ${name}.`;
        else if (s.el.querySelector(".tt-commercial").checked && !s.el.querySelector(".tt-organic").checked && !s.el.querySelector(".tt-branded").checked)
          problem = "You need to indicate if your content promotes yourself, a third party, or both.";
      } else if (s.account.platform === "youtube" && !s.el.querySelector(".yt-title").value.trim()) problem = `Give the YouTube video on ${name} a title.`;
    }
    m.querySelector("#apgo").disabled = !!problem;
    m.querySelector("#aphint").textContent = problem;
    m.querySelector("#apgo").title = problem;
  }

  function wire(s) {
    s.el.querySelectorAll("input, select, textarea").forEach(i => i.addEventListener("input", () => { tiktokRules(s); check(); }));
    s.el.querySelectorAll("input[type=checkbox], select").forEach(i => i.addEventListener("change", () => { tiktokRules(s); check(); }));
    tiktokRules(s);
  }

  m.querySelectorAll("[data-acct]").forEach(i => i.addEventListener("change", sync));
  m.querySelector("#apgo").onclick = e => withBusy(e.currentTarget, async () => {
    const targets = [...state.values()].map(s => {
      const q = sel => s.el.querySelector(sel);
      const t = { account_id: s.account.id, caption: q(".ap-caption").value };
      if (s.account.platform === "tiktok") Object.assign(t, {
        privacy: q(".tt-privacy").value, allow_comment: q(".tt-comment").checked, allow_duet: q(".tt-duet").checked,
        allow_stitch: q(".tt-stitch").checked, commercial: q(".tt-commercial").checked,
        brand_organic: q(".tt-organic").checked, brand_content: q(".tt-branded").checked, consent: true });
      if (s.account.platform === "youtube") Object.assign(t, { title: q(".yt-title").value, privacy: q(".yt-privacy").value });
      return t;
    });
    const r = await api(`/api/clips/${c.id}/posts`, { method: "POST",
      body: { targets, spacing_minutes: +m.querySelector("#ap-spacing").value } });
    m.remove();
    toast(r.posts.length === 1 ? "Queued: it will post in a moment" : `Queued ${r.posts.length} posts`);
    if (typeof loadPosts === "function") loadPosts();
  }, "Queuing…");
  check();
}

function targetForm(s, v, c) {
  const a = s.account;
  const head = `<div class="ap-head">${acctAvatar(a, 24)}<b>${esc(a.platform_label)}: ${esc(acctName(a))}</b></div>`;
  const caption = (label, rows = 3) => `<div class="field"><label>${label}</label><textarea class="ap-caption" rows="${rows}" maxlength="2200">${esc(v.caption)}</textarea></div>`;
  if (a.platform === "youtube") return head + `
    <div class="field"><label>Title</label><input class="yt-title" type="text" maxlength="100" value="${esc(v.title.slice(0, 100))}"></div>
    ${caption("Description")}
    <div class="field"><label>Visibility</label><select class="yt-privacy"><option value="public">Public</option><option value="unlisted">Unlisted</option><option value="private">Private</option></select></div>`;
  if (a.platform === "instagram") return head + caption("Caption") + `<p class="small muted">Posted as a Reel and shared to the feed.</p>`;
  // TikTok
  const i = s.info;
  const dur = c.duration;
  s.tooLong = !!(i.max_duration && dur > i.max_duration + 0.5);
  const opts = i.privacy_options.map(p => `<option value="${p}">${TT_PRIVACY[p] || p}</option>`).join("");
  const box = (cls, label, off) => `<label class="check${off ? " off" : ""}" ${off ? 'title="This account has turned this off on TikTok."' : ""}><input type="checkbox" class="${cls}" ${off ? "disabled" : ""}> ${label}</label>`;
  return head + `
    <p class="small muted" style="margin-top:-4px">Posting as <b>${esc(i.nickname)}</b>${i.username ? ` (@${esc(i.username)})` : ""}.</p>
    ${s.tooLong ? `<p class="small" style="color:var(--danger)">This account can post videos up to ${i.max_duration} seconds; this clip is ${Math.round(dur)} seconds.</p>` : ""}
    ${caption("Caption")}
    <div class="field"><label>Who can see this video</label><select class="tt-privacy"><option value="" selected disabled>Choose…</option>${opts}</select>
      ${i.audited ? "" : `<p class="small muted" style="margin:6px 0 0">Until TikTok approves this app, TikTok posts can only be private (Only me).</p>`}</div>
    <div class="field"><label>Allow users to</label><div class="row">
      ${box("tt-comment", "Comment", i.comment_disabled)}${box("tt-duet", "Duet", i.duet_disabled)}${box("tt-stitch", "Stitch", i.stitch_disabled)}</div></div>
    <div class="field"><label class="check"><input type="checkbox" class="tt-commercial"> Disclose video content</label>
      <p class="small muted" style="margin:4px 0 0">Turn this on if this video promotes goods or services in exchange for something of value.</p>
      <div class="tt-commercial-opts" hidden>
        <label class="check"><input type="checkbox" class="tt-organic"> Your brand <span class="small muted">: you are promoting yourself or your own business</span></label>
        <label class="check"><input type="checkbox" class="tt-branded"> Branded content <span class="small muted">: you are promoting another brand or a third party</span></label>
        <p class="small tt-label" style="color:var(--warn);margin:6px 0 0"></p></div></div>
    <p class="small muted tt-declare"></p>`;
}

// TikTok's rules for the commercial content disclosure and the declaration shown before posting.
function tiktokRules(s) {
  const q = sel => s.el.querySelector(sel);
  if (s.account.platform !== "tiktok" || !q(".tt-privacy")) return;
  const on = q(".tt-commercial").checked, organic = on && q(".tt-organic").checked, branded = on && q(".tt-branded").checked;
  q(".tt-commercial-opts").hidden = !on;
  q(".tt-label").textContent = branded ? "Your video will be labeled “Paid partnership”."
    : organic ? "Your video will be labeled “Promotional content”." : on ? "You need to indicate if your content promotes yourself, a third party, or both." : "";
  const priv = q(".tt-privacy"), self = priv.querySelector('option[value="SELF_ONLY"]');
  if (self) {
    self.disabled = branded;
    self.title = branded ? "Branded content visibility cannot be set to private." : "";
    if (branded && priv.value === "SELF_ONLY") { priv.value = ""; toast("Branded content visibility cannot be set to private.", true); }
  }
  q(".tt-declare").innerHTML = branded
    ? `By posting, you agree to TikTok's <a href="${BC_URL}" target="_blank" rel="noopener">Branded Content Policy</a> and <a href="${MUSIC_URL}" target="_blank" rel="noopener">Music Usage Confirmation</a>.`
    : `By posting, you agree to TikTok's <a href="${MUSIC_URL}" target="_blank" rel="noopener">Music Usage Confirmation</a>.`;
}

// Post status under each clip: where it went, and whether it worked.
const POST_STATUS = { queued: ["Waiting", "queued"], posting: ["Posting", "queued"], posted: ["Posted", "done"],
  failed: ["Failed", "failed"], cancelled: ["Cancelled", "cancelled"] };
function postRows(posts) {
  return posts.map(p => {
    const [label, cls] = POST_STATUS[p.status] || [p.status, ""];
    const when = p.status === "queued" && new Date(p.scheduled_at) > Date.now()
      ? ` at ${new Date(p.scheduled_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
    const detail = p.status === "failed" ? p.error : p.status === "posted" ? (p.stage !== "Posted" ? p.stage : "") : p.stage;
    return `<div class="post-row"><span class="pf">${esc(p.platform_label)}</span>
      <span class="who">${esc(p.account)}</span>
      <span class="badge ${cls}">${label}${when}</span>
      ${p.url ? `<a class="small" href="${esc(p.url)}" target="_blank" rel="noopener">View</a>` : ""}
      ${p.status === "failed" || p.status === "cancelled" ? `<button class="sm" onclick="retryPost(${p.id}, this)">Retry</button>` : ""}
      ${p.status === "queued" ? `<button class="sm" onclick="cancelPost(${p.id}, this)">Cancel</button>` : ""}
      ${detail ? `<div class="small muted detail">${esc(detail)}</div>` : ""}</div>`;
  }).join("");
}
function retryPost(id, btn) {
  withBusy(btn, async () => { await api(`/api/posts/${id}/retry`, { method: "POST" }); toast("Trying again"); if (typeof loadPosts === "function") loadPosts(); });
}
function cancelPost(id, btn) {
  withBusy(btn, async () => { await api(`/api/posts/${id}`, { method: "DELETE" }); toast("Cancelled"); if (typeof loadPosts === "function") loadPosts(); });
}
