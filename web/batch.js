"use strict";
/* Editor modes: Batch (apply a saved preset to many videos) and Merge (join clips into one video). */

const batch = { items: [], running: false, dir: "" };
const mergeState = { items: [], running: false, size: "9:16", fit: "blur" };

const ROW_ICONS = {
  up: '<svg viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>',
  remove: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
};

function fmtDur(t) {
  t = Math.max(0, t || 0);
  return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
}

/* ---------- mode switch ---------- */
function setEditorMode(mode) {
  $("#view-editor").dataset.mode = mode;
  $$("#edModes button").forEach((b) => b.classList.toggle("on", b.dataset.m === mode));
  if (mode !== "edit") $("#edVideo").pause();
  if (mode === "batch") window.loadPresets?.();
  window.dispatchEvent(new Event("resize"));
}
$("#edModes").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-m]");
  if (b) setEditorMode(b.dataset.m);
});

function rowHtml(item, i, opts) {
  const meta = item.ok ? `${fmtDur(item.duration)} · ${item.width}×${item.height}` : `Can't read: ${esc(item.error || "")}`;
  const state = item.state ? `<span class="fstate ${item.state}">${{ working: `${item.pct || 0}%`, done: "✓ Done", failed: "Failed",
    cancelled: "Cancelled" }[item.state] || ""}</span>` : "<span></span>";
  const move = opts.reorder ? `<button class="icon-btn" data-a="up" title="Move up">${ROW_ICONS.up}</button>
      <button class="icon-btn" data-a="down" title="Move down">${ROW_ICONS.down}</button>` : "";
  return `<div class="file-row" data-i="${i}" title="${esc(item.error || item.path)}">
    <span class="fidx">${i + 1}</span>
    <div class="finfo"><span class="fname">${esc(item.name)}</span><span class="fmeta mono">${meta}</span></div>
    ${state}
    <div class="factions">${move}<button class="icon-btn" data-a="remove" title="Remove">${ROW_ICONS.remove}</button></div>
    <i class="rowbar" style="width:${item.state === "working" ? item.pct || 0 : item.state === "done" ? 100 : 0}%"></i>
  </div>`;
}

function emptyHtml(title, sub) {
  return `<div class="file-empty"><b>${title}</b>${sub}</div>`;
}

async function probeAndAdd(list, paths) {
  const known = new Set(list.map((x) => x.path.toLowerCase()));
  const fresh = (paths || []).filter((p) => !known.has(p.toLowerCase()));
  const items = await api.editor_probe_many(paths ? fresh : null);
  for (const it of items) if (!known.has(it.path.toLowerCase())) list.push(it);
  return items.length;
}

/* ---------- batch ---------- */
function renderBatch() {
  const list = $("#batchList");
  list.innerHTML = batch.items.length
    ? batch.items.map((it, i) => rowHtml(it, i, { reorder: false })).join("")
    : emptyHtml("Add the videos to edit", "Use “Add videos…” or drop video files here. Each one gets the preset you choose.");
  $("#batchCount").textContent = `${batch.items.length} video${batch.items.length === 1 ? "" : "s"}`;
  $("#batchDir").textContent = batch.dir ? batch.dir : "";
  $$("#batchDirMode button").forEach((b) => b.classList.toggle("on", (b.dataset.d === "custom") === !!batch.dir));
  const ready = batch.items.some((x) => x.ok) && $("#batchPreset").value && !batch.running && !state.busy;
  $("#batchStart").disabled = !ready;
  $("#batchStart").classList.toggle("hidden", batch.running);
  $("#batchCancel").classList.toggle("hidden", !batch.running);
  $("#batchAdd").disabled = $("#batchClear").disabled = batch.running;
}

window.batchAdd = async (paths) => {
  if (batch.running) return;
  await probeAndAdd(batch.items, paths);
  renderBatch();
};
window.onPresetsChanged = (presets) => {
  const current = $("#batchPreset").value;
  $("#batchPreset").innerHTML = `<option value="">— choose a saved preset —</option>`
    + presets.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  if (presets.some((p) => p.name === current)) $("#batchPreset").value = current;
  renderBatch();
};

$("#batchAdd").addEventListener("click", () => window.batchAdd(null));
$("#batchClear").addEventListener("click", () => { if (!batch.running) { batch.items = []; renderBatch(); } });
$("#batchPreset").addEventListener("change", renderBatch);
$("#batchList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-a]");
  if (!btn || batch.running) return;
  batch.items.splice(Number(btn.closest(".file-row").dataset.i), 1);
  renderBatch();
});
$("#batchDirMode").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-d]");
  if (!b || batch.running) return;
  if (b.dataset.d === "same") batch.dir = "";
  else batch.dir = (await api.batch_pick_dir(batch.dir || state.dir)) || batch.dir;
  renderBatch();
});
$("#batchStart").addEventListener("click", async () => {
  const preset = (window.presetCacheList?.() || []).find((p) => p.name === $("#batchPreset").value);
  const items = batch.items.filter((x) => x.ok);
  batch.items = items;
  items.forEach((x) => { x.state = "queued"; x.pct = 0; });
  const res = await api.batch_start(items.map((x) => x.path), preset?.preset, batch.dir, $("#batchSuffix").value);
  if (!res.ok) return toast("error", "Can't start batch", res.error);
  batch.running = true;
  $("#batchStatus").textContent = `Processing 0/${items.length}…`;
  $("#batchFill").style.width = "0%";
  renderBatch();
});
$("#batchCancel").addEventListener("click", () => api.editor_cancel());

/* ---------- merge ---------- */
function mergeTotal() {
  const ok = mergeState.items.filter((x) => x.ok);
  const t = Number($("#mergeTrans").value);
  const sum = ok.reduce((a, x) => a + x.duration, 0);
  return Math.max(0, sum - (ok.length > 1 ? t * (ok.length - 1) : 0));
}

function renderMerge() {
  const list = $("#mergeList");
  list.innerHTML = mergeState.items.length
    ? mergeState.items.map((it, i) => rowHtml(it, i, { reorder: true })).join("")
    : emptyHtml("Add the clips to join", "Use “Add clips…” or drop video files here, then put them in order.");
  $("#mergeCount").textContent = `${mergeState.items.length} clip${mergeState.items.length === 1 ? "" : "s"}`;
  const t = Number($("#mergeTrans").value);
  $("#mergeTransVal").textContent = t ? `Crossfade ${t.toFixed(1)}s` : "Cut";
  $("#mergeTotal").textContent = mergeState.items.length ? `Total ≈ ${fmtDur(mergeTotal())}` : "";
  $$("#mergeSize button").forEach((b) => b.classList.toggle("on", b.dataset.s === mergeState.size));
  $$("#mergeFit button").forEach((b) => b.classList.toggle("on", b.dataset.f === mergeState.fit));
  const ready = mergeState.items.filter((x) => x.ok).length >= 2 && !mergeState.running && !state.busy;
  $("#mergeStart").disabled = !ready;
  $("#mergeStart").classList.toggle("hidden", mergeState.running);
  $("#mergeCancel").classList.toggle("hidden", !mergeState.running);
  $("#mergeAdd").disabled = $("#mergeClear").disabled = mergeState.running;
}

window.mergeAdd = async (paths) => {
  if (mergeState.running) return;
  await probeAndAdd(mergeState.items, paths);
  if (mergeState.items.length && $("#mergeName").value === "merged") {
    $("#mergeName").value = `merged - ${mergeState.items[0].name.replace(/\.[^.]+$/, "")}`;
  }
  renderMerge();
};

$("#mergeAdd").addEventListener("click", () => window.mergeAdd(null));
$("#mergeClear").addEventListener("click", () => { if (!mergeState.running) { mergeState.items = []; renderMerge(); } });
$("#mergeTrans").addEventListener("input", renderMerge);
$("#mergeSize").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-s]");
  if (b) { mergeState.size = b.dataset.s; renderMerge(); }
});
$("#mergeFit").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (b) { mergeState.fit = b.dataset.f; renderMerge(); }
});
$("#mergeList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-a]");
  if (!btn || mergeState.running) return;
  const i = Number(btn.closest(".file-row").dataset.i);
  const items = mergeState.items;
  if (btn.dataset.a === "remove") items.splice(i, 1);
  else if (btn.dataset.a === "up" && i > 0) [items[i - 1], items[i]] = [items[i], items[i - 1]];
  else if (btn.dataset.a === "down" && i < items.length - 1) [items[i + 1], items[i]] = [items[i], items[i + 1]];
  renderMerge();
});
$("#mergeStart").addEventListener("click", async () => {
  const items = mergeState.items.filter((x) => x.ok);
  const opts = { size: mergeState.size, fit: mergeState.fit, transition: Number($("#mergeTrans").value),
    stripMetadata: $("#mergeStrip").checked };
  const res = await api.merge_start(items.map((x) => x.path), opts, $("#mergeName").value);
  if (!res.ok) return toast("error", "Can't merge", res.error);
  mergeState.running = true;
  $("#mergeStatus").textContent = "Starting…";
  renderMerge();
});
$("#mergeCancel").addEventListener("click", () => api.editor_cancel());

/* ---------- events from Python ---------- */
Object.assign(handlers, {
  batchItem: (e) => {
    const it = batch.items[e.index];
    if (!it) return;
    Object.assign(it, { state: e.state, pct: e.pct, error: e.error || it.error });
    const done = batch.items.filter((x) => ["done", "failed", "cancelled"].includes(x.state)).length;
    const working = batch.items.find((x) => x.state === "working");
    $("#batchStatus").textContent = `Processing ${done}/${batch.items.length}${working ? ` · ${working.name}` : ""}`;
    $("#batchFill").style.width = `${((done + (working ? (working.pct || 0) / 100 : 0)) / batch.items.length) * 100}%`;
    renderBatch();
  },
  batchDone: (e) => {
    batch.running = false;
    $("#batchStatus").textContent = `${e.ok}/${e.total} exported${e.cancelled ? " · cancelled" : ""}`;
    $("#batchFill").style.width = "100%";
    renderBatch();
    if (e.failed.length) toast("error", `Batch: ${e.ok}/${e.total} exported`, e.failed.join("\n"));
    else toast("success", `Batch finished: ${e.ok}/${e.total} exported`, e.cancelled ? "Cancelled before the end." : "Saved next to the originals or in the chosen folder.");
  },
  mergeProgress: (e) => {
    $("#mergeTrack").classList.toggle("indeterminate", e.value === null);
    if (typeof e.value === "number") {
      $("#mergeFill").style.width = `${Math.round(e.value * 100)}%`;
      $("#mergeStatus").textContent = `Merging… ${Math.round(e.value * 100)}%`;
    }
  },
  mergeDone: (e) => {
    mergeState.running = false;
    $("#mergeTrack").classList.remove("indeterminate");
    if (e.cancelled) {
      $("#mergeStatus").textContent = "Cancelled";
      $("#mergeFill").style.width = "0%";
    } else {
      $("#mergeStatus").textContent = `Saved ${e.width}×${e.height} · ${fmtDur(e.duration)} · ${e.encoder}`;
      $("#mergeFill").style.width = "100%";
      toast("success", "Merge finished", e.name, { label: "Show file", run: () => api.reveal_file(e.path) });
    }
    renderMerge();
  },
});

/* a failed batch/merge ends with the generic "error" event, then "busy": reset the running flags */
const busyBeforeBatch = handlers.busy;
handlers.busy = (e) => {
  busyBeforeBatch(e);
  if (!e.busy) {
    if (mergeState.running) { mergeState.running = false; $("#mergeStatus").textContent = "Merge failed"; $("#mergeTrack").classList.remove("indeterminate"); }
    if (batch.running) { batch.running = false; }
  }
  renderBatch();
  renderMerge();
};

renderBatch();
renderMerge();
