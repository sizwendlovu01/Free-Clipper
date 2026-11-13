// Public pages: navigation, hero carousel and tabs, scroll reveals, and the "drop a link" start forms.
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Sticky nav border + mobile menu
  const nav = $("#nav");
  const onScroll = () => nav && nav.classList.toggle("scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  const burger = $("#burger"), menu = $("#mobile-menu");
  if (burger && menu) {
    burger.addEventListener("click", () => {
      const open = menu.classList.toggle("open");
      burger.setAttribute("aria-expanded", String(open));
    });
    $$("a", menu).forEach(a => a.addEventListener("click", () => {
      menu.classList.remove("open");
      burger.setAttribute("aria-expanded", "false");
    }));
  }
  const year = $("#year");
  if (year) year.textContent = new Date().getFullYear();

  // Reveal on scroll
  const reveals = $$(".reveal");
  if ("IntersectionObserver" in window && !reduceMotion) {
    const io = new IntersectionObserver(entries => entries.forEach(e => {
      if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }), { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    reveals.forEach(el => io.observe(el));
  } else {
    reveals.forEach(el => el.classList.add("in"));
  }

  // Hero carousel: center one clip, dim the others
  const track = $("#track");
  if (track) {
    const cards = $$(".clip-card", track);
    let index = 2;
    const show = () => {
      const card = cards[index];
      const stage = track.parentElement;
      const offset = card.offsetLeft + card.offsetWidth / 2 - stage.clientWidth / 2;
      track.style.transform = `translateX(${-offset}px)`;
      cards.forEach((c, i) => c.classList.toggle("dim", i !== index));
    };
    const go = step => { index = (index + step + cards.length) % cards.length; show(); };
    $("#prev") && $("#prev").addEventListener("click", () => go(-1));
    $("#next") && $("#next").addEventListener("click", () => go(1));
    window.addEventListener("resize", show);
    show();
    let startX = null;  // swipe on touch screens
    track.parentElement.addEventListener("touchstart", e => { startX = e.touches[0].clientX; }, { passive: true });
    track.parentElement.addEventListener("touchend", e => {
      if (startX === null) return;
      const dx = e.changedTouches[0].clientX - startX;
      if (Math.abs(dx) > 40) go(dx < 0 ? 1 : -1);
      startX = null;
    });
    let timer = reduceMotion ? null : setInterval(() => go(1), 4500);
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    ["click", "touchstart"].forEach(ev => track.parentElement.parentElement.addEventListener(ev, stop, { passive: true }));

    // Feature tabs change the caption of the featured clip
    const tabs = $$(".tab");
    tabs.forEach(tab => tab.addEventListener("click", () => {
      stop();
      tabs.forEach(t => t.setAttribute("aria-selected", String(t === tab)));
      const cap = $(".cap", cards[index]);
      if (cap) cap.innerHTML = tab.dataset.cap;
    }));
    tabs.forEach((tab, i) => tab.addEventListener("keydown", e => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      next.focus();
      next.click();
    }));
  }

  // Editor waveform
  const wave = $("#wave");
  if (wave) {
    const frag = document.createDocumentFragment();
    for (let i = 0; i < 120; i++) {
      const bar = document.createElement("i");
      const h = 18 + Math.abs(Math.sin(i * 0.45) * 55 + Math.sin(i * 1.7) * 20);
      bar.style.height = Math.min(92, h) + "%";
      frag.appendChild(bar);
    }
    wave.insertBefore(frag, wave.firstChild);
  }

  // "Drop a video link" forms: keep the link through sign-up and hand it to the dashboard.
  const start = window.CF_START || "/signup";
  $$(".js-start").forEach(form => {
    const input = $("input", form);
    const msg = form.nextElementSibling && form.nextElementSibling.classList.contains("form-msg")
      ? form.nextElementSibling : null;
    form.addEventListener("submit", e => {
      e.preventDefault();
      const value = (input.value || "").trim();
      if (!value) { go(); return; }
      let url;
      try { url = new URL(value); } catch (_) { url = null; }
      if (!url || !/^https?:$/.test(url.protocol)) {
        if (msg) msg.textContent = "Enter a full link that starts with https://";
        input.setAttribute("aria-invalid", "true");
        input.focus();
        return;
      }
      try { sessionStorage.setItem("cf_pending_url", url.href); } catch (_) { /* storage blocked: still continue */ }
      go();
    });
    input.addEventListener("input", () => { if (msg) msg.textContent = ""; input.removeAttribute("aria-invalid"); });
    const up = $(".js-upload", form);
    if (up) up.addEventListener("click", () => {
      try { sessionStorage.setItem("cf_pending_upload", "1"); } catch (_) { /* ignore */ }
      go();
    });
  });
  function go() { window.location.href = start; }
})();
