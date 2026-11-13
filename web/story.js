"use strict";
/* Editor mode: Story video (owner edition) - a script becomes an animated vertical video:
   1. a main character (a sheet drawn in Google Flow), 2. the script split into scenes, 3. render
   (AI Studio voice, Flow pictures, corner mark removed, timed on the narration). */

const st = { ready: false, chars: [], scenes: [], running: false, music: "none", musicFile: "", result: null };

function stOpts() {
  return {
    profile: $("#stProfile").value, voice: $("#stVoice").value.trim() || "Knox", voiceStyle: $("#stVoiceStyle").value.trim(),
    music: st.music, musicPrompt: $("#stMusicPrompt").value.trim(), musicFile: st.musicFile,
    captions: $("#stCaptions").checked, counter: $("#stCounter").checked, removeMark: $("#stRemoveMark").checked,
    hires: $("#stHires").checked, character: $("#stChar").value,
  };
}

function stRenderChars() {
  const sel = $("#stChar");
  const cur = sel.value || st.character || "";
  sel.innerHTML = `<option value="">＋ New character</option>` +
    st.chars.map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`).join("");
  sel.value = st.chars.some((c) => c.slug === cur) ? cur : "";
  stShowChar();
}

function stShowChar() {
  const c = st.chars.find((x) => x.slug === $("#stChar").value);
  $("#stCharImg").hidden = !c || !c.url;
  if (c && c.url) $("#stCharImg").src = c.url;
  $("#stCharName").value = c ? c.name : $("#stCharName").value;
  $("#stCharDesc").value = c ? c.description : $("#stCharDesc").value;
  $("#stCharStyle").value = c ? c.style : $("#stCharStyle").value;
  $(".st-char-new").classList.toggle("existing", !!c);
}

function stSceneHtml(s, i) {
  const hud = s.hud || {};
  return `<div class="st-scene" data-i="${i}">
    <span class="fidx">${i + 1}</span>
    <textarea data-k="text" title="Narration of this scene (read by the voice, word for word)">${esc(s.text)}</textarea>
    <textarea data-k="visual" title="What the picture shows">${esc(s.visual)}</textarea>
    <div class="st-extra">
      <input data-k="title" placeholder="Big title (optional)" value="${esc((s.title || "").replace(/\n/g, " / "))}">
      <div class="st-hud"><input data-k="hudL" placeholder="DAY 1" value="${esc(hud.left || "")}"><input data-k="hudR" placeholder="LV.1" value="${esc(hud.right || "")}"><input data-k="hudB" placeholder="0.1" value="${hud.bar ?? ""}"></div>
      <input data-k="pop" placeholder="Pop word (LEVEL UP!)" value="${esc(s.pop || "")}">
      <select data-k="transition"><option value="cut"${s.transition === "whip" ? "" : " selected"}>Cut</option><option value="whip"${s.transition === "whip" ? " selected" : ""}>Whip</option></select>
    </div>
    <button class="icon-btn" data-a="del" title="Remove this scene">${ROW_ICONS.remove || "✕"}</button>
  </div>`;
}

function stRenderScenes() {
  $("#stScenes").innerHTML = st.scenes.length ? st.scenes.map(stSceneHtml).join("")
    : emptyHtml("No scenes yet", "Write the narration above and press “Split into scenes”. You can edit every scene before rendering.");
}

function stReadScene(row) {
  const s = st.scenes[Number(row.dataset.i)];
  const v = (k) => row.querySelector(`[data-k="${k}"]`).value;
  s.text = v("text").trim();
  s.visual = v("visual").trim();
  s.title = v("title").trim().replace(/\s*\/\s*/g, "\n");
  s.pop = v("pop").trim();
  s.transition = v("transition");
  const bar = parseFloat(v("hudB"));
  s.hud = v("hudL").trim() || v("hudR").trim()
    ? { left: v("hudL").trim(), right: v("hudR").trim(), bar: Number.isFinite(bar) ? Math.min(Math.max(bar, 0), 1) : null } : null;
}

function stRender() {
  const busy = st.running || state.busy;
  $("#stStart").classList.toggle("hidden", st.running);
  $("#stCancel").classList.toggle("hidden", !st.running);
  $("#stStart").disabled = busy || !st.scenes.length;
  $("#stPlan").disabled = $("#stCharMake").disabled = $("#stCharFile").disabled = busy;
  $$("#stMusic button").forEach((b) => b.classList.toggle("on", b.dataset.v === st.music));
  $("#stMusicPrompt").hidden = st.music !== "flow";
  $("#stMusicPick").hidden = st.music !== "file";
  $("#stMusicName").textContent = st.music === "file" ? (st.musicFile ? st.musicFile.split(/[\\/]/).pop() : "No file chosen") : "";
  $("#stDone").hidden = st.running || !st.result;
}

async function stLoad() {
  const res = await api.story_state();
  if (!res.ok) return;
  st.chars = res.characters;
  if (!st.ready) {
    const o = res.opts;
    st.character = o.character;
    $("#stVoice").value = o.voice;
    $("#stVoiceStyle").value = o.voiceStyle;
    $("#stMusicPrompt").value = o.musicPrompt;
    st.music = o.music;
    st.musicFile = o.musicFile || "";
    $("#stCaptions").checked = o.captions;
    $("#stCounter").checked = o.counter;
    $("#stRemoveMark").checked = o.removeMark;
    $("#stHires").checked = o.hires;
    st.profile = o.profile;
    st.ready = true;
  }
  const prof = $("#stProfile");
  const keep = prof.value || st.profile;
  prof.innerHTML = res.chromeRoot
    ? `<option value="">Choose…</option>` + res.profiles.map((p) => `<option value="${esc(p.dir)}">${esc(p.name)}</option>`).join("")
    : `<option value="">Set the Chrome Portable folder in the TikTok tab first</option>`;
  prof.value = res.profiles.some((p) => p.dir === keep) ? keep : "";
  $("#stEngine").textContent = res.gemini ? "Scenes: Gemini (local model if it fails)" : "Scenes: local model (add a Gemini key in the TikTok tab for better scenes)";
  stRenderChars();
  stRender();
}

/* ---------- step 1: character ---------- */
$("#stChar").addEventListener("change", stShowChar);
$("#stCharMake").addEventListener("click", async () => {
  const res = await api.story_make_character($("#stCharName").value, $("#stCharDesc").value, $("#stCharStyle").value, stOpts());
  if (!res.ok) return toast("error", "Can't draw the character", res.error);
  st.running = true;
  stStart("Drawing the character…");
});
$("#stCharFile").addEventListener("click", async () => {
  const res = await api.story_character_file($("#stCharName").value, $("#stCharDesc").value, $("#stCharStyle").value);
  if (!res.ok) { if (!res.cancelled) toast("error", "Can't use this picture", res.error); return; }
  st.chars = res.characters;
  st.character = res.slug;
  stRenderChars();
  $("#stChar").value = res.slug;
  stShowChar();
});

/* ---------- step 2: script -> scenes ---------- */
$("#stPlan").addEventListener("click", async () => {
  const res = await api.story_plan($("#stScript").value, $("#stChar").value, $("#stCounter").checked);
  if (!res.ok) return toast("error", "Can't split the script", res.error);
  st.running = true;
  stStart("Splitting the script into scenes…");
});
$("#stScenes").addEventListener("input", (e) => {
  const row = e.target.closest(".st-scene");
  if (row) stReadScene(row);
});
$("#stScenes").addEventListener("change", (e) => {
  const row = e.target.closest(".st-scene");
  if (row) stReadScene(row);
});
$("#stScenes").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-a=del]");
  if (!b) return;
  st.scenes.splice(Number(b.closest(".st-scene").dataset.i), 1);
  stRenderScenes();
  stRender();
});

/* ---------- settings ---------- */
$("#stMusic").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]");
  if (b) { st.music = b.dataset.v; stRender(); }
});
$("#stMusicPick").addEventListener("click", async () => {
  const res = await api.story_pick_music();
  if (res.ok) { st.musicFile = res.path; stRender(); }
});
["#stProfile", "#stVoice", "#stVoiceStyle", "#stMusicPrompt", "#stCaptions", "#stCounter", "#stRemoveMark", "#stHires"]
  .forEach((id) => $(id).addEventListener("change", () => api.story_save(stOpts())));

/* ---------- step 3: render ---------- */
function stStart(text) {
  st.result = null;
  $("#stLog").textContent = "";
  $("#stStatus").textContent = text;
  $("#stFill").style.width = "0%";
  $("#stTrack").classList.add("indeterminate");
  stRender();
}
$("#stStart").addEventListener("click", async () => {
  $$("#stScenes .st-scene").forEach(stReadScene);
  const res = await api.story_render($("#stTitle").value, $("#stChar").value, st.scenes, stOpts());
  if (!res.ok) return toast("error", "Can't render", res.error);
  st.running = true;
  stStart("Starting…");
});
$("#stCancel").addEventListener("click", () => api.editor_cancel());
$("#stEdit").addEventListener("click", () => st.result && window.openQueue([{ file: st.result.path, title: st.result.title }]));
$("#stPost").addEventListener("click", () => st.result && window.openPostManyDialog([{ file: st.result.path, caption: st.result.title }]));
$("#stShow").addEventListener("click", () => st.result && api.reveal_file(st.result.path));

function stFinish(text, full) {
  st.running = false;
  $("#stTrack").classList.remove("indeterminate");
  $("#stFill").style.width = full ? "100%" : "0%";
  $("#stStatus").textContent = text;
  stRender();
}

Object.assign(handlers, {
  stProgress: (e) => {
    $("#stStatus").textContent = e.text;
    const known = typeof e.value === "number";
    $("#stTrack").classList.toggle("indeterminate", !known);
    if (known) $("#stFill").style.width = `${Math.round(e.value * 100)}%`;
  },
  stLog: (e) => {
    const log = $("#stLog");
    log.textContent += (log.textContent ? "\n" : "") + e.message;
    log.scrollTop = log.scrollHeight;
  },
  stCharacter: (e) => {
    st.chars = e.characters;
    st.character = e.slug;
    stRenderChars();
    $("#stChar").value = e.slug;
    stShowChar();
    api.story_save(stOpts());
    stFinish("Character ready", true);
  },
  stScenes: (e) => {
    st.scenes = e.scenes;
    stRenderScenes();
    stFinish(`${e.scenes.length} scenes — check them, then render`, true);
  },
  stDone: (e) => {
    st.result = e;
    stFinish(`Done · ${fmtDur(e.duration)} · ${e.name}`, true);
    toast("success", "Story video ready", e.name, { label: "Show file", run: () => api.reveal_file(e.path) });
  },
});

/* a failed task ends with the generic "error" event, then "busy" */
const busyBeforeSt = handlers.busy;
handlers.busy = (e) => {
  busyBeforeSt(e);
  if (!e.busy && st.running) stFinish("Stopped — see the log", false);
  stRender();
};

const stModeBefore = window.setEditorMode;
window.setEditorMode = (mode) => {
  stModeBefore(mode);
  if (mode === "story") stLoad();
};
stRenderScenes();
