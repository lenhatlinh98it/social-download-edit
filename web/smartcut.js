"use strict";
/* Editor mode: Smart cut (owner edition) - a long landscape video -> vertical clips picked by local AI. */

const sc = { source: null, clips: [], plan: [], running: false, len: "20-60", reframe: "face", subPos: "bottom", style: "series", target: 90, ready: false };

function scOpts() {
  const [minLen, maxLen] = sc.len.split("-").map(Number);
  return {
    style: sc.style, target: sc.target, partLabel: $("#scPartLabel").checked, count: Number($("#scCount").value) || 2, minLen, maxLen, reframe: sc.reframe, subtitles: $("#scSubs").checked, subPos: sc.subPos,
    whisper: $("#scWhisper").value, language: $("#scLang").value, useLlm: $("#scLlm").checked,
    llmModel: $("#scLlmModel").value.trim(),
  };
}

function scClock(t) {
  return fmtDur(t);
}

function renderSc() {
  $("#scSource").textContent = !sc.source ? "Choose a long landscape video"
    : sc.source.url ? `Link: ${sc.source.url}`
      : `${sc.source.name} · ${fmtDur(sc.source.duration)} · ${sc.source.width}×${sc.source.height}`;
  $("#scSource").title = sc.source?.path || "";
  const rows = sc.plan.map((m, i) => {
    const clip = sc.clips.find((c) => c.index === i);
    const state = clip ? '<span class="fstate done">✓ Done</span>' : sc.running ? '<span class="fstate working">…</span>' : "<span></span>";
    const len = m.length ?? m.end - m.start;
    const span = m.parts && m.parts.length > 1 ? `${m.parts.length} shots from ${scClock(m.start)}–${scClock(m.end)}` : `${scClock(m.start)}–${scClock(m.end)}`;
    const meta = `${span} · ${fmtDur(len)}${m.score != null ? ` · score ${m.score}` : ""}`;
    return `<div class="file-row sc-row" data-i="${i}">
      <span class="fidx">${i + 1}</span>
      <div class="finfo"><span class="fname">${esc(clip?.title || m.title || clip?.name || `Clip ${i + 1}`)}</span>
        <span class="fmeta mono">${meta}${m.reason ? `<span class="sc-reason">${esc(m.reason)}</span>` : ""}</span></div>
      ${state}
      <div class="factions">${clip ? `<button class="icon-btn" data-a="edit" title="Open in the Editor"><svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></button>
        <button class="icon-btn" data-a="post" title="Post to TikTok">${ROW_ICONS.post}</button>
        <button class="icon-btn" data-a="show" title="Show the file">${ROW_ICONS.show}</button>` : ""}</div>
    </div>`;
  }).join("");
  $("#scList").innerHTML = rows || emptyHtml(sc.source ? "Ready to cut" : "Choose a long video",
    sc.source ? "Set the options on the right, then press “Make clips”." : "Use “Choose video…” or drop a video file here. A 16:9 video becomes 9:16 clips.");
  $("#scStart").disabled = !sc.source || sc.running || state.busy;
  $("#scStart").classList.toggle("hidden", sc.running);
  $("#scCancel").classList.toggle("hidden", !sc.running);
  $("#scPick").disabled = $("#scUseUrl").disabled = $("#scUrl").disabled = sc.running;
  $$("#scLen button").forEach((b) => b.classList.toggle("on", b.dataset.l === sc.len));
  $$("#scStyle button").forEach((b) => b.classList.toggle("on", b.dataset.s === sc.style));
  $$("#scTarget button").forEach((b) => b.classList.toggle("on", Number(b.dataset.t) === sc.target));
  $("#scTarget").classList.toggle("off", sc.style !== "series");
  $("#scLen").classList.toggle("off", sc.style === "series");
  $("#scPartLabelRow").hidden = sc.style !== "series";
  $("#scCountLabel").textContent = sc.style === "series" ? "How many parts" : "How many shorts";
  $$("#scReframe button").forEach((b) => b.classList.toggle("on", b.dataset.r === sc.reframe));
  $$("#scSubPos button").forEach((b) => b.classList.toggle("on", b.dataset.p === sc.subPos));
  $("#scSubPos").classList.toggle("off", !$("#scSubs").checked);
  $("#scLlmModel").disabled = !$("#scLlm").checked;
  $("#scDone").hidden = sc.running || !sc.clips.length;
}

async function scLoadState() {
  const res = await api.smartcut_state();
  if (!res.ok) return;
  if (!sc.ready) {
    const o = res.opts;
    $("#scCount").value = o.count;
    sc.len = `${o.minLen}-${o.maxLen}`;
    if (![...$$("#scLen button")].some((b) => b.dataset.l === sc.len)) sc.len = "20-60";
    sc.reframe = o.reframe;
    sc.style = o.style === "moments" ? "moments" : "series";
    $("#scPartLabel").checked = o.partLabel !== false;
    sc.target = Number(o.target) || 90;
    sc.subPos = o.subPos || "bottom";
    $("#scSubs").checked = o.subtitles;
    $("#scWhisper").value = o.whisper;
    $("#scLang").value = o.language || "";
    $("#scLlm").checked = o.useLlm;
    $("#scLlmModel").value = o.llmModel;
    sc.ready = true;
  }
  const model = $("#scLlmModel").value.trim();
  const gpu = res.cuda ? `<span class="ok">NVIDIA GPU found</span> — Whisper runs on the GPU.` : "No NVIDIA GPU — Whisper runs on the CPU (slower).";
  let llm;
  if (res.ollama === null) llm = `<span class="bad">Ollama is not running</span> — install it from ollama.com, or moments are picked with heuristics.`;
  else if (!res.ollama.some((m) => m === model || m.split(":")[0] === model)) llm = `<span class="bad">Ollama has no “${esc(model)}”</span> — run <span class="mono">ollama pull ${esc(model)}</span>.`;
  else llm = `<span class="ok">Ollama ready</span> with ${esc(model)}.`;
  $("#scAi").innerHTML = `${gpu}<br>${llm}<br>Models are downloaded on first use.`;
  renderSc();
}

window.scSetSource = async (path) => {
  if (sc.running) return;
  const items = await api.editor_probe_many(path ? [path] : null);
  const it = items.find((x) => x.ok);
  if (!it) {
    if (items.length) toast("error", "Can't read this video", items[0].error);
    return;
  }
  if (it.width <= it.height) toast("info", "This video is already vertical", "Smart cut will only cut it into clips (no reframing needed).");
  sc.source = it;
  sc.plan = [];
  sc.clips = [];
  $("#scLog").textContent = "";
  $("#scStatus").textContent = "Ready";
  $("#scFill").style.width = "0%";
  renderSc();
};

$("#scPick").addEventListener("click", () => window.scSetSource(null));

/* a YouTube / Douyin link: downloaded by Make clips, then cut */
async function scUseUrl() {
  const url = $("#scUrl").value.trim();
  if (!url || sc.running) return;
  const kind = await api.detect_kind(url);
  if (kind !== "youtube" && kind !== "douyin") {
    toast("error", "Not a YouTube or Douyin link", "Paste the link of one video, e.g. youtube.com/watch?v=… or douyin.com/video/…");
    return;
  }
  sc.source = { url, path: url };
  sc.plan = [];
  sc.clips = [];
  $("#scLog").textContent = "";
  $("#scStatus").textContent = "Ready — the video is downloaded when you press Make clips";
  $("#scFill").style.width = "0%";
  renderSc();
}
$("#scUseUrl").addEventListener("click", scUseUrl);
$("#scUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") scUseUrl(); });
$("#scLen").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-l]");
  if (b) { sc.len = b.dataset.l; renderSc(); }
});
$("#scStyle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-s]");
  if (b) { sc.style = b.dataset.s; renderSc(); }
});
$("#scTarget").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-t]");
  if (b) { sc.target = Number(b.dataset.t); renderSc(); }
});
$("#scReframe").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-r]");
  if (b) { sc.reframe = b.dataset.r; renderSc(); }
});
$("#scLlm").addEventListener("change", renderSc);
$("#scSubs").addEventListener("change", renderSc);
$("#scSubPos").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-p]");
  if (b) { sc.subPos = b.dataset.p; renderSc(); }
});
$("#scLlmModel").addEventListener("change", scLoadState);
$("#scStart").addEventListener("click", async () => {
  if (!sc.source || sc.running) return;
  // running before the call: a run that fails at once (a bad link) can send its "busy: false" before this returns
  sc.running = true;
  sc.plan = [];
  sc.clips = [];
  $("#scLog").textContent = "";
  $("#scStatus").textContent = "Starting…";
  $("#scFill").style.width = "0%";
  renderSc();
  const res = await api.smartcut_start(sc.source.path, scOpts());
  if (!res.ok) {
    sc.running = false;
    $("#scStatus").textContent = "Ready";
    renderSc();
    toast("error", "Can't start Smart cut", res.error);
  }
});
$("#scCancel").addEventListener("click", () => api.editor_cancel());

function scDoneClips() {
  return sc.clips.slice().sort((a, b) => a.index - b.index);
}
$("#scEdit").addEventListener("click", () => window.openQueue(scDoneClips().map((c) => ({ file: c.path, title: c.title }))));
$("#scPost").addEventListener("click", () => window.openPostManyDialog(scDoneClips().map((c) => ({ file: c.path, caption: c.title }))));
$("#scFolder").addEventListener("click", () => { const c = sc.clips[0]; if (c) api.reveal_file(c.path); });
$("#scList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-a]");
  if (!btn) return;
  const clip = sc.clips.find((c) => c.index === Number(btn.closest(".file-row").dataset.i));
  if (!clip) return;
  if (btn.dataset.a === "edit") window.openQueue([{ file: clip.path, title: clip.title }]);
  else if (btn.dataset.a === "post") window.openPostManyDialog([{ file: clip.path, caption: clip.title }]);
  else if (btn.dataset.a === "show") api.reveal_file(clip.path);
});

Object.assign(handlers, {
  scSource: (e) => {  // the link finished downloading: from now on it is this file
    sc.source = { ok: true, path: e.path, name: e.name, duration: e.duration, width: e.width, height: e.height };
    $("#scUrl").value = "";
    renderSc();
  },
  scProgress: (e) => {
    $("#scStatus").textContent = e.text;
    if (typeof e.value === "number") $("#scFill").style.width = `${Math.round(e.value * 100)}%`;
  },
  scLog: (e) => {
    const log = $("#scLog");
    log.textContent += (log.textContent ? "\n" : "") + e.message;
    log.scrollTop = log.scrollHeight;
  },
  scPlan: (e) => {
    sc.plan = e.moments;
    renderSc();
  },
  scClip: (e) => {
    sc.clips.push(e);
    renderSc();
  },
  scDone: (e) => {
    sc.running = false;
    $("#scFill").style.width = e.cancelled ? "0%" : "100%";
    $("#scStatus").textContent = e.cancelled ? `Cancelled · ${e.clips.length} clip(s) made` : `${e.clips.length} clip(s) made`;
    renderSc();
    if (!e.cancelled) {
      toast("success", `Smart cut: ${e.clips.length} clips`, "Open them all in the Editor, or post them straight to TikTok.",
        { label: "Open in Editor", run: () => $("#scEdit").click() });
    }
  },
});

/* a failed run ends with the generic "error" event, then "busy" */
const busyBeforeSc = handlers.busy;
handlers.busy = (e) => {
  busyBeforeSc(e);
  if (!e.busy && sc.running) {
    sc.running = false;
    $("#scStatus").textContent = "Smart cut failed";
  }
  renderSc();
};

const scModeBefore = window.setEditorMode;
window.setEditorMode = (mode) => {
  scModeBefore(mode);
  if (mode === "smartcut") scLoadState();
};
renderSc();
