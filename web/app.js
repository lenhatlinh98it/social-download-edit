"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ICONS = {
  check: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  info: '<svg viewBox="0 0 24 24"><path d="M12 8h.01M11 12h1v5h1"/></svg>',
  yt: '<svg viewBox="0 0 24 24"><path d="M21.6 7.2a2.5 2.5 0 0 0-1.8-1.8C18.2 5 12 5 12 5s-6.2 0-7.8.4A2.5 2.5 0 0 0 2.4 7.2 26 26 0 0 0 2 12a26 26 0 0 0 .4 4.8 2.5 2.5 0 0 0 1.8 1.8C5.8 19 12 19 12 19s6.2 0 7.8-.4a2.5 2.5 0 0 0 1.8-1.8A26 26 0 0 0 22 12a26 26 0 0 0-.4-4.8zM10 15V9l5.2 3z"/></svg>',
  fb: '<svg viewBox="0 0 24 24"><path d="M22 12a10 10 0 1 0-11.6 9.9v-7H7.9V12h2.5V9.8c0-2.5 1.5-3.9 3.8-3.9 1.1 0 2.2.2 2.2.2v2.5h-1.3c-1.2 0-1.6.8-1.6 1.6V12h2.8l-.4 2.9h-2.3v7A10 10 0 0 0 22 12z"/></svg>',
  dy: '<svg viewBox="0 0 24 24"><path d="M16.6 3c.4 2.1 1.8 3.6 3.9 3.9v3.2c-1.5 0-2.9-.4-4-1.2v6.4a5.6 5.6 0 1 1-5.6-5.6c.3 0 .6 0 .9.1v3.2a2.5 2.5 0 1 0 1.6 2.3V3z"/></svg>',
  heart: '<svg viewBox="0 0 24 24"><path d="M12 20.5s-7.5-4.6-9.3-9.2C1.4 7.9 3.6 4.5 7 4.5c2 0 3.6 1.1 5 2.9 1.4-1.8 3-2.9 5-2.9 3.4 0 5.6 3.4 4.3 6.8-1.8 4.6-9.3 9.2-9.3 9.2z"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  open: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  external: '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  undo: '<svg viewBox="0 0 24 24"><path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
  scissors: '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"/></svg>',
};

const SOURCES = {
  youtube: {
    placeholder: "Paste a YouTube channel URL — e.g. https://www.youtube.com/@channel/shorts",
    emptyTitle: "Grab YouTube Shorts",
    emptySub: "Paste a channel link above and hit Fetch. All Shorts of the channel will show up here.",
  },
  facebook: {
    placeholder: "Paste a Facebook page URL — e.g. https://www.facebook.com/page/reels",
    emptyTitle: "Grab Facebook Reels",
    emptySub: "Connect your Facebook account once, then paste a fanpage link and hit Fetch.",
  },
  douyin: {
    placeholder: "Paste a Douyin user URL — e.g. https://www.douyin.com/user/MS4wLjABAAAA…",
    emptyTitle: "Grab Douyin videos",
    emptySub: "Paste a Douyin user link and hit Fetch. Guests only get the ~20 newest videos for a few visits — connect Douyin to load every video reliably.",
  },
};

const PLATFORMS = {
  youtube: { name: "YouTube", icon: () => ICONS.yt },
  facebook: { name: "Facebook", icon: () => ICONS.fb },
  douyin: { name: "Douyin", icon: () => ICONS.dy },
};

function kindOfUrl(url) {
  if (/douyin\.com/i.test(url)) return "douyin";
  if (/facebook\.com|fb\.com/i.test(url)) return "facebook";
  return "youtube";
}

const state = {
  view: "youtube",
  busy: false,
  downloading: false,
  engine: { current: "", latest: "", updateAvailable: false, installed: "" },
  dir: "",
  saved: [],
  history: [],
  histFilter: "all",
  histQuery: "",
  fbLoggedIn: false,
  dyLoggedIn: false,
  lists: {
    youtube: { videos: [], selected: new Set(), gen: 0, hidden: 0, folder: "" },
    facebook: { videos: [], selected: new Set(), gen: 0, hidden: 0, folder: "" },
    douyin: { videos: [], selected: new Set(), gen: 0, hidden: 0, folder: "", limited: false },
  },
};
let api = null;

/* ---------- views & tabs ---------- */
function setView(view) {
  state.view = view;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $("#dock").classList.toggle("hidden", ["saved", "downloaded", "editor"].includes(view));
  window.onEditorView?.(view === "editor");
  movePill();
  updateCounts();
  renderDir();
  if (view === "facebook") scheduleCaptions();
  if (view === "downloaded") loadHistory();
}

function movePill() {
  const active = $(".tab.active");
  if (!active) return;
  const pill = $("#tabPill");
  pill.style.width = `${active.offsetWidth}px`;
  pill.style.transform = `translateX(${active.offsetLeft}px)`;
}

function buildSourceView(kind) {
  const view = $(`#view-${kind}`);
  view.append($("#sourceTpl").content.cloneNode(true));
  const cfg = SOURCES[kind];
  const url = $(".url", view);
  url.placeholder = cfg.placeholder;
  url.addEventListener("keydown", (e) => { if (e.key === "Enter") fetchList(kind); });
  $(".fetch", view).addEventListener("click", () => fetchList(kind));
  $(".save-link", view).addEventListener("click", () => saveFromSource(kind));
  $(".select-all", view).addEventListener("click", () => selectAll(kind, true));
  $(".clear", view).addEventListener("click", () => selectAll(kind, false));
  $(".empty-title", view).textContent = cfg.emptyTitle;
  $(".empty-sub", view).textContent = cfg.emptySub;
  $(".empty", view).classList.add("on");
  $(".grid", view).addEventListener("click", (e) => {
    const card = e.target.closest(".card:not(.skeleton)");
    if (card) toggleCard(kind, card);
  });
  if (kind === "facebook") setupCaptionObserver(view);
}

/* ---------- grid ---------- */
function showSkeleton(kind) {
  const view = $(`#view-${kind}`);
  $(".empty", view).classList.remove("on");
  $(".grid", view).innerHTML = Array.from({ length: 14 },
    () => '<div class="card skeleton"><div class="thumb"></div><div class="title"></div></div>').join("");
}

function showSkeletonOff(kind) {
  if ($(`#view-${kind} .card.skeleton`)) renderGrid(kind);
}

function cardHTML(v, i) {
  const title = v.title || "Loading caption…";
  return `<div class="card" data-i="${i}">
    <div class="thumb">
      <img loading="lazy" decoding="async" referrerpolicy="no-referrer" src="${esc(v.thumb)}" onload="this.classList.add('loaded')" alt="">
      <div class="shade"></div>
      <span class="badge"></span>
      <div class="check">${ICONS.check}</div>
      ${v.views ? `<div class="views">${v.stat === "likes" ? ICONS.heart : ICONS.play}${esc(v.views)}</div>` : ""}
      <div class="cbar"><i></i></div>
    </div>
    <div class="title${v.title ? "" : " pending"}" title="${esc(v.title)}">${esc(title)}</div>
  </div>`;
}

function renderGrid(kind) {
  const view = $(`#view-${kind}`);
  const L = state.lists[kind];
  const grid = $(".grid", view);
  $(".grid-wrap", view).scrollTop = 0;
  grid.innerHTML = L.videos.map(cardHTML).join("");
  const empty = $(".empty", view);
  empty.classList.toggle("on", L.videos.length === 0);
  if (!L.videos.length && L.hidden) {
    $(".empty-title", view).textContent = "You're all caught up";
    $(".empty-sub", view).textContent = `All ${L.hidden} videos of this source were already downloaded.`;
  } else if (!L.videos.length) {
    $(".empty-title", view).textContent = "No videos found";
    $(".empty-sub", view).textContent = "Double-check the link, or try another channel / page.";
  }
  if (kind === "facebook") observeCards();
  updateCounts();
}

function cardEl(kind, i) {
  return $(`#view-${kind} .card[data-i="${i}"]`);
}

function toggleCard(kind, card) {
  const i = Number(card.dataset.i);
  const sel = state.lists[kind].selected;
  sel.has(i) ? sel.delete(i) : sel.add(i);
  card.classList.toggle("selected", sel.has(i));
  updateCounts();
}

function selectAll(kind, on) {
  const L = state.lists[kind];
  L.selected = on ? new Set(L.videos.map((_, i) => i)) : new Set();
  $$(`#view-${kind} .card:not(.skeleton)`).forEach((c) => c.classList.toggle("selected", on));
  updateCounts();
}

function updateCounts() {
  for (const kind of Object.keys(state.lists)) {
    const L = state.lists[kind];
    const chip = $(`#view-${kind} .count`);
    if (chip) chip.innerHTML = `<b>${L.selected.size}</b>&nbsp;/ ${L.videos.length} selected`
      + (L.hidden ? `&nbsp;·&nbsp;${L.hidden} downloaded hidden` : "");
  }
  const L = state.lists[state.view];
  $("#dlCount").textContent = L ? L.selected.size : 0;
  $("#savedCount").textContent = state.saved.length;
  $("#historyCount").textContent = state.history.length;
  syncDisabled();
}

/* ---------- Facebook captions (lazy, for visible cards) ---------- */
let captionTimer = null;
let captionsRequested = new Set();

function setupCaptionObserver(view) {
  $(".grid-wrap", view).addEventListener("scroll", scheduleCaptions, { passive: true });
  window.addEventListener("resize", scheduleCaptions);
  // scroll events are frame-driven and can stall while the window is occluded; poll as a fallback
  setInterval(requestVisibleCaptions, 1000);
}

function observeCards() {
  captionsRequested = new Set();
  scheduleCaptions();
}

function scheduleCaptions() {
  clearTimeout(captionTimer);
  captionTimer = setTimeout(requestVisibleCaptions, 200);
}

function requestVisibleCaptions() {
  if (state.view !== "facebook" || !api) return;
  const wrap = $("#view-facebook .grid-wrap").getBoundingClientRect();
  const videos = state.lists.facebook.videos;
  const wanted = [];
  for (const card of $$("#view-facebook .card:not(.skeleton)")) {
    const r = card.getBoundingClientRect();
    if (r.bottom < wrap.top - 300 || r.top > wrap.bottom + 300) continue;
    const i = Number(card.dataset.i);
    if (!videos[i]?.title && !captionsRequested.has(i)) wanted.push(i);
  }
  if (!wanted.length) return;
  wanted.forEach((i) => captionsRequested.add(i));
  api.load_captions("facebook", wanted);
}

/* ---------- busy / progress ---------- */
function setBusy(busy) {
  state.busy = busy;
  $("#sysDot").classList.toggle("busy", busy);
  $("#sysText").textContent = busy ? "WORKING" : "READY";
  syncDisabled();
}

function syncDisabled() {
  $$(".fetch").forEach((b) => (b.disabled = state.busy));
  $$(".fb-connect button").forEach((b) => (b.disabled = state.busy));
  $("#engineBtn").disabled = state.busy && !state.engine.installed;
  const L = state.lists[state.view];
  $("#dlBtn").disabled = state.busy || !L || L.selected.size === 0;
  $("#dlBtn").classList.toggle("hidden", state.downloading);
  $("#markBtn").disabled = $("#dlBtn").disabled;
  $("#markBtn").classList.toggle("hidden", state.downloading);
  $("#cancelBtn").classList.toggle("hidden", !state.downloading);
}

function renderEngine() {
  const e = state.engine;
  const btn = $("#engineBtn");
  btn.classList.toggle("update", e.updateAvailable && !e.installed);
  btn.classList.toggle("restart", !!e.installed);
  if (e.installed) {
    $("#engineText").textContent = `Restart to use ${e.installed}`;
    btn.title = "A newer yt-dlp is installed — click to restart the app";
  } else if (e.updateAvailable) {
    $("#engineText").textContent = `Update yt-dlp → ${e.latest}`;
    btn.title = `Installed: ${e.current}. Click to update to ${e.latest}`;
  } else {
    $("#engineText").textContent = `yt-dlp ${e.current}`;
    btn.title = "Download engine (yt-dlp) — click to check for updates";
  }
  syncDisabled();
}

async function engineClick() {
  if (state.engine.installed) return api.restart();
  if (!(await api.update_engine())) toast("error", "Busy", "Please wait for the current task to finish.");
}

function setStatus(text, progress) {
  if (text) $("#statusText").textContent = text;
  const track = $("#dock .track");
  if (progress === null) {
    track.classList.add("indeterminate");
    $("#pctText").textContent = "";
  } else if (typeof progress === "number") {
    track.classList.remove("indeterminate");
    $("#pfill").style.width = `${Math.round(progress * 100)}%`;
    $("#pctText").textContent = progress > 0 ? `${Math.round(progress * 100)}%` : "";
  }
}

/* ---------- toasts ---------- */
function toast(type, title, message = "", action = null) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const icon = type === "success" ? ICONS.check : type === "error" ? ICONS.x : ICONS.info;
  el.innerHTML = `<div class="ti">${icon}</div><div class="tb"><div class="tt">${esc(title)}</div>${message ? `<div class="tm">${esc(message)}</div>` : ""}</div>`;
  if (action) {
    const btn = document.createElement("button");
    btn.className = "btn ghost sm ta";
    btn.textContent = action.label;
    btn.addEventListener("click", action.run);
    $(".tb", el).append(btn);
  }
  $("#toasts").append(el);
  const close = () => { el.classList.add("out"); setTimeout(() => el.remove(), 300); };
  setTimeout(close, type === "error" ? 8000 : 4500);
  el.addEventListener("dblclick", close);
}

/* ---------- actions ---------- */
async function fetchList(kind) {
  if (state.busy) return;
  const input = $(`#view-${kind} .url`);
  const res = await api.fetch(kind, input.value);
  if (!res.ok) return toast("error", "Can't fetch", res.error);
  input.value = res.url;
  state.lists[kind].selected = new Set();
  showSkeleton(kind);
  updateCounts();
}

async function download() {
  const kind = state.view;
  const L = state.lists[kind];
  if (!L || !L.selected.size) return;
  const res = await api.download(kind, [...L.selected], state.dir);
  if (!res.ok) toast("error", "Can't download", res.error);
}

async function markDownloaded() {
  const kind = state.view;
  const L = state.lists[kind];
  if (!L || !L.selected.size) return;
  const res = await api.mark_as_downloaded(kind, [...L.selected]);
  if (!res.ok) return toast("error", "Can't add", res.error);
  const wrap = $(`#view-${kind} .grid-wrap`);
  const top = wrap.scrollTop;
  L.videos = res.videos;
  L.gen = res.gen;
  L.selected = new Set();
  L.hidden += res.count;
  renderGrid(kind);
  wrap.scrollTop = top;
  loadHistory();
  toast("success", `Added ${res.count} video${res.count > 1 ? "s" : ""} to downloaded`,
    "They are hidden from now on. Undo from the Downloaded tab.");
}

async function chooseDir() {
  const dir = await api.choose_dir(state.dir);
  if (dir) {
    state.dir = dir;
    renderDir();
  }
}

function renderDir() {
  const folder = state.lists[state.view]?.folder;
  const sep = state.dir.includes("/") && !state.dir.includes("\\") ? "/" : "\\";
  const full = folder ? `${state.dir.replace(/[\\/]+$/, "")}${sep}${folder}` : state.dir;
  $("#dirText").textContent = full;
  $("#folderBtn").title = folder ? `${full}\n(a subfolder per channel / page is created automatically)` : state.dir;
}

function renderFbConnect() {
  const box = $("#view-facebook .fb-connect");
  box.innerHTML = state.fbLoggedIn
    ? '<span class="status-pill"><i></i>Facebook connected</span><button class="btn ghost sm">Reconnect</button>'
    : '<span class="status-pill off"><i></i>Not connected</span><button class="btn primary sm">Connect Facebook</button>';
  $("button", box).addEventListener("click", async () => {
    if (!(await api.fb_login())) toast("error", "Busy", "Another task is running.");
  });
  syncDisabled();
}

function renderDyConnect() {
  const box = $("#view-douyin .fb-connect");
  box.innerHTML = state.dyLoggedIn
    ? '<span class="status-pill"><i></i>Douyin connected</span><button class="btn ghost sm">Reconnect</button>'
    : '<span class="status-pill guest" title="Douyin only shows the ~20 newest videos to guests"><i></i>Guest mode</span><button class="btn primary sm">Connect Douyin</button>';
  $("button", box).addEventListener("click", async () => {
    if (!(await api.dy_login())) toast("error", "Busy", "Another task is running.");
  });
  syncDisabled();
}

/* ---------- saved ---------- */
function renderSaved() {
  const list = $("#savedList");
  if (!state.saved.length) {
    list.innerHTML = `<div class="saved-row" style="justify-content:center;color:var(--muted)">No saved sources yet — fetch a channel and press ☆, or add one above.</div>`;
  } else {
    list.innerHTML = state.saved.map((it, i) => {
      const kind = kindOfUrl(it.url);
      return `<div class="saved-row" style="animation-delay:${Math.min(i * 30, 300)}ms" data-url="${esc(it.url)}" data-kind="${kind}">
        <div class="platform ${kind}">${PLATFORMS[kind].icon()}</div>
        <div class="saved-info">
          <div class="saved-title">${esc(it.title)}</div>
          <div class="saved-url mono">${esc(it.url)}</div>
        </div>
        <button class="btn primary sm open">${ICONS.open}Open</button>
        <button class="icon-btn del" title="Delete">${ICONS.trash}</button>
      </div>`;
    }).join("");
  }
  updateCounts();
}

$("#savedList").addEventListener("click", async (e) => {
  const row = e.target.closest(".saved-row[data-url]");
  if (!row) return;
  const { url, kind } = row.dataset;
  if (e.target.closest(".open")) {
    if (state.busy) return toast("error", "Busy", "Please wait for the current task to finish.");
    $(`#view-${kind} .url`).value = url;
    setView(kind);
    fetchList(kind);
  } else if (e.target.closest(".del")) {
    const btn = e.target.closest(".del");
    if (!btn.classList.contains("confirm")) {
      btn.classList.add("confirm");
      btn.textContent = "Delete?";
      setTimeout(() => { if (btn.isConnected) { btn.classList.remove("confirm"); btn.innerHTML = ICONS.trash; } }, 3000);
      return;
    }
    state.saved = await api.saved_delete(url);
    renderSaved();
  }
});

$("#savedForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await api.saved_add($("#sTitle").value, $("#sLink").value);
  if (!res.ok) return toast("error", "Can't save", res.error);
  state.saved = res.saved;
  toast("success", res.updated ? "Title updated" : "Saved", $("#sTitle").value.trim());
  $("#sTitle").value = "";
  $("#sLink").value = "";
  renderSaved();
});

function saveFromSource(kind) {
  const url = $(`#view-${kind} .url`).value.trim();
  setView("saved");
  $("#sLink").value = url;
  $("#sTitle").value = state.saved.find((it) => url && it.url === url)?.title || "";
  $("#sTitle").focus();
}

/* ---------- downloaded history ---------- */
async function loadHistory() {
  if (!api) return;
  state.history = await api.history_list();
  renderHistory();
}

function renderHistory() {
  const q = state.histQuery.trim().toLowerCase();
  const items = state.history.filter((it) =>
    (state.histFilter === "all" || it.kind === state.histFilter)
    && (!q || it.title.toLowerCase().includes(q) || it.id.toLowerCase().includes(q)));
  $("#histCountChip").textContent = `${items.length} video${items.length === 1 ? "" : "s"}`;
  const list = $("#histList");
  if (!items.length) {
    list.innerHTML = state.history.length
      ? '<div class="hist-empty"><b>No matches</b>Try another keyword or platform filter.</div>'
      : '<div class="hist-empty"><b>Nothing downloaded yet</b>Videos you download will be listed here.</div>';
  } else {
    list.innerHTML = items.map((it) => {
      const icon = (PLATFORMS[it.kind] || PLATFORMS.youtube).icon();
      const name = (PLATFORMS[it.kind] || PLATFORMS.youtube).name;
      return `<div class="hist-row" data-kind="${esc(it.kind)}" data-id="${esc(it.id)}">
        <div class="hist-thumb"><span class="ph">${icon}</span>${it.thumb
          ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(it.thumb)}" onerror="this.remove()" alt="">` : ""}</div>
        <div class="hist-info">
          <div class="hist-title" title="${esc(it.title)}">${esc(it.title)}</div>
          <div class="hist-meta"><span class="pf-tag ${esc(it.kind)}">${icon}${name}</span>
            <span class="mono">${esc(it.at)}</span><span class="mono">· ${esc(it.id)}</span>${it.manual ? '<span class="manual-tag">added manually</span>' : ""}</div>
        </div>
        <div class="hist-actions">
          <button class="icon-btn h-edit" title="${it.fileExists ? "Edit in the Editor" : "File not found"}" ${it.fileExists ? "" : "disabled"}>${ICONS.scissors}</button>
          <button class="icon-btn h-open" title="Open on ${name}">${ICONS.external}</button>
          <button class="icon-btn h-file" title="${it.fileExists ? "Show file in folder" : it.manual ? "No file (added manually)" : "File not found"}" ${it.fileExists ? "" : "disabled"}>${ICONS.folder}</button>
          <button class="icon-btn h-del" title="Remove from history — it will show up again on the next fetch">${ICONS.undo}</button>
        </div>
      </div>`;
    }).join("");
  }
  updateCounts();
}

$("#histList").addEventListener("click", async (e) => {
  const row = e.target.closest(".hist-row");
  if (!row) return;
  const it = state.history.find((h) => h.kind === row.dataset.kind && h.id === row.dataset.id);
  if (!it) return;
  if (e.target.closest(".h-open")) api.open_url(it.url);
  else if (e.target.closest(".h-file")) api.reveal_file(it.file);
  else if (e.target.closest(".h-edit")) window.openInEditor(it.file);
  else if (e.target.closest(".h-del")) {
    const btn = e.target.closest(".h-del");
    if (!btn.classList.contains("confirm")) {
      btn.classList.add("del", "confirm");
      btn.textContent = "Remove?";
      setTimeout(() => { if (btn.isConnected) { btn.classList.remove("del", "confirm"); btn.innerHTML = ICONS.undo; } }, 3000);
      return;
    }
    state.history = await api.history_remove(it.kind, it.id);
    renderHistory();
    toast("info", "Removed from history", "It will show up again the next time you fetch this source.");
  }
});

$("#histFilter").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-f]");
  if (!btn) return;
  state.histFilter = btn.dataset.f;
  $$("#histFilter button").forEach((b) => b.classList.toggle("on", b === btn));
  renderHistory();
});

$("#histSearch").addEventListener("input", (e) => {
  state.histQuery = e.target.value;
  renderHistory();
});

/* ---------- events from Python ---------- */
const handlers = {
  busy: (e) => setBusy(e.busy),
  status: (e) => setStatus(e.text, e.progress),
  error: (e) => {
    toast("error", "Something went wrong", e.message);
    setStatus("Error", 0);
    Object.keys(state.lists).forEach(showSkeletonOff);
  },
  fbLogin: (e) => {
    state.fbLoggedIn = e.loggedIn;
    renderFbConnect();
    if (e.loggedIn) toast("success", "Facebook connected");
  },
  dyLogin: (e) => {
    const changed = state.dyLoggedIn !== e.loggedIn;
    state.dyLoggedIn = e.loggedIn;
    renderDyConnect();
    if (changed && e.loggedIn) toast("success", "Douyin connected");
  },
  dyLoginRequired: (e) => {
    state.dyLoggedIn = e.loggedIn;
    renderDyConnect();
    showSkeletonOff("douyin");
    toast("error", "Douyin login required", e.message, {
      label: "Connect Douyin",
      run: async () => { if (!(await api.dy_login())) toast("error", "Busy", "Another task is running."); },
    });
  },
  fbExpired: (e) => {
    state.fbLoggedIn = false;
    renderFbConnect();
    showSkeletonOff("facebook");
    toast("error", "Facebook session expired", e.message, {
      label: "Reconnect Facebook",
      run: async () => { if (!(await api.fb_login())) toast("error", "Busy", "Another task is running."); },
    });
  },
  engine: (e) => {
    Object.assign(state.engine, { current: e.current, latest: e.latest, updateAvailable: e.updateAvailable });
    if (e.installed) {
      state.engine.installed = e.installed;
      toast("success", `yt-dlp ${e.installed} installed`, "Restart the app to start using it.", { label: "Restart now", run: () => api.restart() });
    } else if (e.upToDate) {
      toast("info", "yt-dlp is up to date", e.current);
    }
    renderEngine();
  },
  downloading: (e) => {
    state.downloading = e.active;
    $("#cancelBtn").disabled = false;
    syncDisabled();
  },
  list: (e) => {
    const L = state.lists[e.kind];
    L.videos = e.videos;
    L.gen = e.gen;
    L.selected = new Set();
    L.hidden = e.hidden || 0;
    L.folder = e.folder || "";
    $(`#view-${e.kind} .url`).value = e.url;
    renderGrid(e.kind);
    renderDir();
    const note = L.hidden ? `${L.hidden} already downloaded — hidden` : e.url;
    toast("success", `Found ${e.videos.length} new videos`, note);
    if (e.limited) {
      toast("info", "Only the newest videos are shown",
        "Douyin limits guests to the first page of a profile. Connect Douyin to load every video.",
        { label: "Connect Douyin", run: async () => { if (!(await api.dy_login())) toast("error", "Busy", "Another task is running."); } });
    }
  },
  caption: (e) => {
    const L = state.lists[e.kind];
    if (e.gen !== L.gen || !L.videos[e.index]) return;
    L.videos[e.index].title = e.title;
    const t = $(".title", cardEl(e.kind, e.index) || document.createElement("div"));
    if (t) { t.textContent = e.title; t.title = e.title; t.classList.remove("pending"); }
  },
  item: (e) => {
    const card = cardEl(e.kind, e.index);
    if (!card) return;
    const badge = $(".badge", card);
    const bar = $(".cbar", card);
    badge.className = `badge on ${e.state}`;
    badge.textContent = { done: "✓ Saved", failed: "Failed", cancelled: "Cancelled" }[e.state] || `${e.pct}%`;
    bar.classList.toggle("on", e.state === "downloading");
    $("i", bar).style.width = `${e.pct}%`;
  },
  done: (e) => {
    loadHistory();
    const action = { label: "Open folder", run: () => api.open_folder(e.dir) };
    if (e.failed.length) toast("error", `${e.ok}/${e.total} downloaded`, e.failed.join("\n"), action);
    else if (e.cancelled) toast("info", `Cancelled — ${e.ok}/${e.total} downloaded`, e.dir, action);
    else toast("success", `Downloaded ${e.total} video${e.total > 1 ? "s" : ""}`, e.dir, action);
  },
};
window.onPy = (e) => handlers[e.type]?.(e);

/* ---------- boot ---------- */
buildSourceView("youtube");
buildSourceView("facebook");
buildSourceView("douyin");
$$(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));
$("#folderBtn").addEventListener("click", chooseDir);
$("#openDirBtn").addEventListener("click", () => api.open_folder(state.dir));
$("#dlBtn").addEventListener("click", download);
$("#markBtn").addEventListener("click", markDownloaded);
$("#cancelBtn").addEventListener("click", () => { $("#cancelBtn").disabled = true; api.cancel_download(); });
$("#engineBtn").addEventListener("click", engineClick);
window.addEventListener("resize", movePill);
document.fonts?.ready.then(movePill);
setView("youtube");

window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  const init = await api.init();
  state.dir = init.dir;
  state.saved = init.saved;
  state.fbLoggedIn = init.fbLoggedIn;
  state.dyLoggedIn = init.dyLoggedIn;
  state.engine.current = init.engine;
  renderEngine();
  renderDir();
  renderSaved();
  renderFbConnect();
  renderDyConnect();
  loadHistory();
});
