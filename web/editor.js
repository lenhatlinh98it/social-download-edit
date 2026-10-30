"use strict";
/* Video editor: canvas previews that mirror the ffmpeg pipeline in editor.py
   (rotate/flip -> crop -> (animated) zoom -> color -> speed -> fades). All crop coordinates are in the
   rotated frame, in source pixels. */

const ed = {
  loaded: false,
  path: "",
  info: null,
  rotate: 0,
  flipH: false,
  flipV: false,
  ratio: "original",
  crop: { x: 0, y: 0, w: 0, h: 0 },
  zoom: 1,
  panX: 0.5,
  panY: 0.5,
  bg: "blur",
  bgColor: "#111827",
  zoomAnimate: false,
  zoomEnd: 1.3,
  zoomEase: true,
  color: {},
  comparing: false,
  speed: 1,
  volume: 1,
  mute: false,
  fadeIn: 0,
  fadeOut: 0,
  music: null, // {path, name, duration, url, volume, offset, loop}
  text: null, // see defaultText()
  logo: null, // {path, name, url, size, opacity, x, y}
  start: 0,
  end: 0,
  exporting: false,
  active: false,
  srcScale: 1,
};
const video = $("#edVideo");
const musicEl = $("#edMusic");
const srcCanvas = $("#srcCanvas");
const outCanvas = $("#outCanvas");
const RATIOS = { "9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1, "4:5": 4 / 5, "4:3": 4 / 3, "3:4": 3 / 4 };
const MIN_CROP = 32;
const POS_STEPS = [0.03, 0.5, 0.97];
const HIST_KEYS = ["rotate", "flipH", "flipV", "ratio", "crop", "zoom", "panX", "panY", "bg", "bgColor", "zoomAnimate",
  "zoomEnd", "zoomEase", "color", "speed", "volume", "mute", "fadeIn", "fadeOut", "music", "text", "logo", "start", "end"];
let fontList = [];
const fontFamilies = {}; // font file path -> loaded CSS family
let defaultFontPath = "";
let logoImg = null;
const overlayBoxes = { text: null, logo: null }; // hit boxes on the output canvas (css px)

function defaultText() {
  return { content: "", font: defaultFontPath, size: 6, color: "#ffffff", opacity: 1, outline: 0, outlineColor: "#000000",
    box: false, boxColor: "#000000", boxOpacity: 0.5, x: 0.5, y: 0.9 };
}
const COLOR_SLIDERS = [
  ["brightness", "Brightness"], ["contrast", "Contrast"], ["saturation", "Saturation"],
  ["temperature", "Temperature"], ["tint", "Tint"], ["gamma", "Midtones"],
  ["fade", "Fade", 0], ["vignette", "Vignette", 0], ["sharpen", "Sharpen", 0],
];
const PRESETS = {
  Original: {},
  Vivid: { brightness: 3, contrast: 15, saturation: 35 },
  Warm: { temperature: 45, saturation: 10, brightness: 2 },
  Cool: { temperature: -45, tint: -5, contrast: 5 },
  "B&W": { saturation: -100, contrast: 20 },
  Vintage: { saturation: -30, temperature: 25, contrast: -10, fade: 35, vignette: 40 },
  Cinematic: { contrast: 20, saturation: -15, temperature: -15, tint: 8, gamma: -10, vignette: 30 },
  Matte: { fade: 45, contrast: -15, saturation: -10 },
};

/* ---------- geometry ---------- */
function frameSize() {
  const { width, height } = ed.info;
  return ed.rotate % 180 ? [height, width] : [width, height];
}

function ratioValue() {
  if (ed.ratio === "free") return null;
  if (ed.ratio === "original") {
    const [rw, rh] = frameSize();
    return rw / rh;
  }
  return RATIOS[ed.ratio];
}

function fitCrop(r) {
  const [rw, rh] = frameSize();
  let w = rw;
  let h = rh;
  if (r) {
    if (rw / rh > r) w = rh * r;
    else h = rw / r;
  }
  ed.crop = { x: (rw - w) / 2, y: (rh - h) / 2, w, h };
}

function animProgress() {
  const p = Math.min(Math.max((video.currentTime - ed.start) / Math.max(ed.end - ed.start, 0.1), 0), 1);
  return ed.zoomEase ? p * p * (3 - 2 * p) : p;
}

/* zoom factor at the current time, same as editor._zoom_expr */
function currentZoom() {
  return ed.zoomAnimate ? ed.zoom + (ed.zoomEnd - ed.zoom) * animProgress() : ed.zoom;
}

function zoomStraddles() {
  return ed.zoomAnimate && Math.min(ed.zoom, ed.zoomEnd) < 1 && Math.max(ed.zoom, ed.zoomEnd) > 1;
}

/* zoom-in window inside the crop, same math as editor.build_geometry */
function zoomWindow(z) {
  const c = ed.crop;
  const sw = c.w / z;
  const sh = c.h / z;
  const sx = c.x + Math.min(Math.max(ed.panX * c.w - sw / 2, 0), c.w - sw);
  const sy = c.y + Math.min(Math.max(ed.panY * c.h - sh / 2, 0), c.h - sh);
  return { x: sx, y: sy, w: sw, h: sh };
}

/* ---------- color: must match editor.color_transform() ---------- */
function matmul(a, b) {
  return a.map((row, i) => [0, 1, 2, 3].map((j) => [0, 1, 2, 3].reduce((acc, k) => acc + a[i][k] * b[k][j], 0)));
}

function colorTransform(color) {
  const v = (k) => (Number(color[k]) || 0) / 100;
  const b = 1 + v("brightness");
  const c = 1 + v("contrast");
  const o = 0.5 * (1 - c);
  const s = 1 + v("saturation");
  const kt = 0.12 * v("temperature");
  const kg = 0.1 * v("tint");
  const f = 0.25 * Math.max(0, v("fade"));
  const steps = [
    [[b, 0, 0, 0], [0, b, 0, 0], [0, 0, b, 0], [0, 0, 0, 1]],
    [[c, 0, 0, o], [0, c, 0, o], [0, 0, c, o], [0, 0, 0, 1]],
    [[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s, 0],
      [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s, 0],
      [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s, 0], [0, 0, 0, 1]],
    [[1 + kt, 0, 0, 0], [0, 1 - kg, 0, 0], [0, 0, 1 - kt, 0], [0, 0, 0, 1]],
    [[1 - f, 0, 0, f], [0, 1 - f, 0, f], [0, 0, 1 - f, f], [0, 0, 0, 1]],
  ];
  let m = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
  for (const step of steps) m = matmul(step, m);
  return { m, gamma: 2 ** -v("gamma") };
}

const gradeKeys = ["brightness", "contrast", "saturation", "temperature", "tint", "gamma", "fade"];
function gradeActive() {
  return gradeKeys.some((k) => Number(ed.color[k]));
}

function applyGradeFilter() {
  const { m, gamma } = colorTransform(ed.color);
  const rows = m.slice(0, 3).map((r) => [r[0], r[1], r[2], 0, r[3]].map((x) => x.toFixed(5)).join(" "));
  $("#edMatrix").setAttribute("values", `${rows.join("  ")}  0 0 0 1 0`);
  $$(".edGamma").forEach((fn) => fn.setAttribute("exponent", gamma.toFixed(5)));
}

function outputSize() {
  const even = (v) => Math.max(2, Math.round(v / 2) * 2);
  return [even(ed.crop.w), even(ed.crop.h)];
}

/* draw region {x,y,w,h} of the rotated+flipped frame into dest rect */
function drawRegion(ctx, region, dx, dy, dw, dh) {
  const { width: vw, height: vh } = ed.info;
  const [rw, rh] = frameSize();
  ctx.save();
  ctx.beginPath();
  ctx.rect(dx, dy, dw, dh);
  ctx.clip();
  ctx.translate(dx, dy);
  ctx.scale(dw / region.w, dh / region.h);
  ctx.translate(-region.x, -region.y);
  if (ed.flipH) { ctx.translate(rw, 0); ctx.scale(-1, 1); }
  if (ed.flipV) { ctx.translate(0, rh); ctx.scale(1, -1); }
  if (ed.rotate === 90) { ctx.translate(rw, 0); ctx.rotate(Math.PI / 2); }
  else if (ed.rotate === 180) { ctx.translate(rw, rh); ctx.rotate(Math.PI); }
  else if (ed.rotate === 270) { ctx.translate(0, rh); ctx.rotate(-Math.PI / 2); }
  ctx.drawImage(video, 0, 0, vw, vh);
  ctx.restore();
}

function sizeCanvas(canvas, wrap, aspect) {
  const W = Math.max(10, wrap.clientWidth - 16);
  const H = Math.max(10, wrap.clientHeight - 16);
  let cw = W;
  let ch = W / aspect;
  if (ch > H) { ch = H; cw = H * aspect; }
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = `${cw}px`;
  canvas.style.height = `${ch}px`;
  if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
  }
  return [cw, ch, dpr];
}

/* ---------- rendering ---------- */
function render() {
  if (!ed.loaded || video.readyState < 2) return;
  const [rw, rh] = frameSize();

  // source: full rotated frame + crop box
  const [scw, sch, sdpr] = sizeCanvas(srcCanvas, $("#srcWrap"), rw / rh);
  const sctx = srcCanvas.getContext("2d");
  sctx.setTransform(sdpr, 0, 0, sdpr, 0, 0);
  sctx.clearRect(0, 0, scw, sch);
  drawRegion(sctx, { x: 0, y: 0, w: rw, h: rh }, 0, 0, scw, sch);
  ed.srcScale = scw / rw;
  const box = $("#cropBox");
  const off = srcCanvas.getBoundingClientRect();
  const wrapRect = $("#srcWrap").getBoundingClientRect();
  box.style.left = `${off.left - wrapRect.left + ed.crop.x * ed.srcScale}px`;
  box.style.top = `${off.top - wrapRect.top + ed.crop.y * ed.srcScale}px`;
  box.style.width = `${ed.crop.w * ed.srcScale}px`;
  box.style.height = `${ed.crop.h * ed.srcScale}px`;

  // output: exactly what ffmpeg will produce
  const [ow, oh] = outputSize();
  const [ocw, och, odpr] = sizeCanvas(outCanvas, $("#outWrap"), ow / oh);
  const octx = outCanvas.getContext("2d");
  octx.setTransform(odpr, 0, 0, odpr, 0, 0);
  octx.clearRect(0, 0, ocw, och);
  const z = currentZoom();
  const grade = gradeActive() && !ed.comparing ? "url(#edGrade)" : "";
  octx.filter = grade || "none";
  if (z >= 1) {
    drawRegion(octx, zoomWindow(z), 0, 0, ocw, och);
  } else {
    if (ed.bg === "blur") {
      octx.filter = `${grade} blur(${Math.max(6, ocw / 25)}px)`.trim();
      drawRegion(octx, ed.crop, -ocw * 0.05, -och * 0.05, ocw * 1.1, och * 1.1);
      octx.filter = grade || "none";
    } else {
      octx.fillStyle = ed.bg === "color" ? ed.bgColor : "#000";
      octx.fillRect(0, 0, ocw, och);
    }
    const fw = ocw * z;
    const fh = och * z;
    drawRegion(octx, ed.crop, (ocw - fw) * ed.panX, (och - fh) * ed.panY, fw, fh);
  }
  octx.filter = "none";
  if (!ed.comparing) drawVignette(octx, ocw, och);
  drawOverlays(octx, ocw, och, ow, oh);
  drawFade(octx, ocw, och);
  $("#outSize").textContent = `${ow}×${oh}`;
  updateTimeline();
}

/* ---------- text & logo overlays ---------- */
const measureCtx = document.createElement("canvas").getContext("2d");

function fontCss(px) {
  const fam = fontFamilies[ed.text?.font];
  return `${px}px ${fam ? `"${fam}"` : "Arial, sans-serif"}`;
}

/* word-wrap so every line fits 92% of the frame width (drawtext doesn't wrap); CJK text without spaces wraps per char */
function wrapLines(content, fs, maxW) {
  measureCtx.font = fontCss(fs);
  const fits = (str) => measureCtx.measureText(str).width <= maxW;
  const lines = [];
  for (const para of content.replace(/\r/g, "").split("\n")) {
    const tokens = para.split(/(\s+)/).flatMap((tok) => (fits(tok) ? [tok] : [...tok]));
    let line = "";
    for (const tok of tokens) {
      if (line && !fits(line + tok)) {
        lines.push(line.trimEnd());
        line = tok.trimStart();
      } else {
        line += tok;
      }
    }
    lines.push(line.trimEnd());
  }
  return lines;
}

/* layout in output pixels, mirrors drawtext: fontsize = h * size%, line_spacing = 15% fs, box border = 35% fs */
function textLayout(ow, oh) {
  const t = ed.text;
  if (!t || !t.content.trim()) return null;
  const fs = Math.max(8, Math.round(oh * t.size / 100));
  const lines = wrapLines(t.content.trim(), fs, ow * 0.92);
  measureCtx.font = fontCss(fs);
  const m = measureCtx.measureText("Hgy");
  const ascent = m.fontBoundingBoxAscent ?? fs * 0.9;
  const natural = ascent + (m.fontBoundingBoxDescent ?? fs * 0.25);
  const spacing = Math.round(fs * 0.15);
  const tw = Math.max(...lines.map((l) => measureCtx.measureText(l).width), 1);
  const th = lines.length * natural + (lines.length - 1) * spacing;
  return { fs, lines, ascent, natural, spacing, tw, th, pad: Math.round(fs * 0.35),
    border: t.outline > 0 ? Math.max(1, Math.round(fs * t.outline / 100)) : 0 };
}

function drawOverlays(ctx, cw, ch, ow, oh) {
  const k = cw / ow;
  overlayBoxes.text = overlayBoxes.logo = null;
  const L = textLayout(ow, oh);
  if (L) {
    const t = ed.text;
    const x0 = (ow - L.tw) * t.x;
    const y0 = (oh - L.th) * t.y;
    ctx.save();
    ctx.scale(k, k);
    if (t.box) {
      ctx.globalAlpha = t.boxOpacity * t.opacity;
      ctx.fillStyle = t.boxColor;
      ctx.fillRect(x0 - L.pad, y0 - L.pad, L.tw + 2 * L.pad, L.th + 2 * L.pad);
    }
    ctx.globalAlpha = t.opacity;
    ctx.font = fontCss(L.fs);
    ctx.textBaseline = "alphabetic";
    L.lines.forEach((line, i) => {
      const y = y0 + i * (L.natural + L.spacing) + L.ascent;
      if (L.border) {
        ctx.lineJoin = "round";
        ctx.lineWidth = 2 * L.border;
        ctx.strokeStyle = t.outlineColor;
        ctx.strokeText(line, x0, y);
      }
      ctx.fillStyle = t.color;
      ctx.fillText(line, x0, y);
    });
    ctx.restore();
    overlayBoxes.text = { x: (x0 - L.pad) * k, y: (y0 - L.pad) * k, w: (L.tw + 2 * L.pad) * k, h: (L.th + 2 * L.pad) * k,
      freeW: (ow - L.tw) * k, freeH: (oh - L.th) * k };
  }
  const g = ed.logo;
  if (g && logoImg?.complete && logoImg.naturalWidth) {
    const lw = Math.max(2, Math.round(ow * g.size / 200) * 2);
    const lh = lw * logoImg.naturalHeight / logoImg.naturalWidth;
    const x0 = (ow - lw) * g.x;
    const y0 = (oh - lh) * g.y;
    ctx.save();
    ctx.globalAlpha = g.opacity;
    ctx.drawImage(logoImg, x0 * k, y0 * k, lw * k, lh * k);
    ctx.restore();
    overlayBoxes.logo = { x: x0 * k, y: y0 * k, w: lw * k, h: lh * k, freeW: (ow - lw) * k, freeH: (oh - lh) * k };
  }
}

async function ensureFont(path) {
  if (!path || fontFamilies[path]) return;
  try {
    const url = await api.font_url(path);
    const family = `gf${Object.keys(fontFamilies).length}_${Date.now() % 100000}`;
    const face = new FontFace(family, `url("${url}")`);
    await face.load();
    document.fonts.add(face);
    fontFamilies[path] = family;
  } catch (err) {
    toast("error", "Can't load font", String(err));
  }
  kick();
}

/* approximation of ffmpeg's vignette filter (angle = 0.15 + 0.7 * strength) */
function drawVignette(ctx, w, h) {
  const strength = (Number(ed.color.vignette) || 0) / 100;
  if (!strength) return;
  const angle = 0.15 + 0.7 * strength;
  const r = Math.hypot(w, h) / 2;
  const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, r);
  for (const d of [0, 0.25, 0.5, 0.75, 1]) g.addColorStop(d, `rgba(0,0,0,${(1 - Math.cos(angle * d) ** 4).toFixed(3)})`);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
}

/* fade in/out on the output timeline (after speed change), like ffmpeg's fade filter */
function drawFade(ctx, w, h) {
  if (!ed.fadeIn && !ed.fadeOut) return;
  const outLen = (ed.end - ed.start) / ed.speed;
  const t = (video.currentTime - ed.start) / ed.speed;
  let alpha = 1;
  if (ed.fadeIn) alpha = Math.min(alpha, t / ed.fadeIn);
  if (ed.fadeOut) alpha = Math.min(alpha, (outLen - t) / ed.fadeOut);
  alpha = Math.min(Math.max(alpha, 0), 1);
  if (alpha >= 1) return;
  ctx.fillStyle = `rgba(0,0,0,${(1 - alpha).toFixed(3)})`;
  ctx.fillRect(0, 0, w, h);
}

/* ---------- background music preview ---------- */
function musicPosition() {
  const m = ed.music;
  const t = Math.max(0, (video.currentTime - ed.start) / ed.speed); // output time; music is not sped up
  const span = Math.max(m.duration - m.offset, 0.1);
  if (t >= span && !m.loop) return null;
  return m.offset + (t % span);
}

function syncMusic(force) {
  if (!ed.music || !ed.loaded || !ed.active) {
    musicEl.pause();
    return;
  }
  const pos = musicPosition();
  if (pos === null) {
    musicEl.pause();
    return;
  }
  if (force || Math.abs(musicEl.currentTime - pos) > 0.25) musicEl.currentTime = pos;
  if (video.paused) musicEl.pause();
  else if (musicEl.paused) musicEl.play().catch(() => {});
}

let rafId = 0;
function loop() {
  render();
  if (!video.paused) syncMusic(false);
  rafId = ed.active && !video.paused ? requestAnimationFrame(loop) : 0;
}
function kick() {
  if (!rafId) rafId = requestAnimationFrame(loop);
}

/* ---------- side panel ---------- */
function syncPanel() {
  $$("#ratioChips button").forEach((b) => b.classList.toggle("on", b.dataset.r === ed.ratio));
  $$("#bgChips button").forEach((b) => b.classList.toggle("on", b.dataset.bg === ed.bg));
  $("#flipH").classList.toggle("on", ed.flipH);
  $("#flipV").classList.toggle("on", ed.flipV);
  $("#zoom").value = Math.round(ed.zoom * 100);
  $("#zoomVal").textContent = `${Math.round(ed.zoom * 100)}%`;
  $("#zoomLabel").textContent = ed.zoomAnimate ? "Start zoom" : "Zoom";
  $("#zoomAnim").checked = ed.zoomAnimate;
  $("#animRow").classList.toggle("on", ed.zoomAnimate);
  $("#zoomEnd").value = Math.round(ed.zoomEnd * 100);
  $("#zoomEndVal").textContent = `${Math.round(ed.zoomEnd * 100)}%`;
  $("#zoomEase").checked = ed.zoomEase;
  $("#zoomWarn").classList.toggle("on", zoomStraddles());
  $("#bgRow").classList.toggle("disabled", (ed.zoomAnimate ? Math.min(ed.zoom, ed.zoomEnd) : ed.zoom) >= 1);
  $("#bgColor").value = ed.bgColor;
  for (const [key] of COLOR_SLIDERS) {
    const value = Number(ed.color[key]) || 0;
    $(`#c_${key}`).value = value;
    $(`#c_${key}_val`).textContent = value > 0 && $(`#c_${key}`).min !== "0" ? `+${value}` : `${value}`;
    $(`#c_${key}`).closest(".slider-row").classList.toggle("changed", value !== 0);
  }
  const preset = Object.entries(PRESETS).find(([, p]) => COLOR_SLIDERS.every(([k]) => (Number(ed.color[k]) || 0) === (p[k] || 0)));
  $$("#presetChips button").forEach((b) => b.classList.toggle("on", !!preset && b.dataset.p === preset[0]));
  applyGradeFilter();
  $("#speed").value = Math.round(ed.speed * 100);
  $("#speedVal").textContent = `${+ed.speed.toFixed(2)}×`;
  $$("#speedChips button").forEach((b) => b.classList.toggle("on", Number(b.dataset.s) === ed.speed));
  $("#volume").value = Math.round(ed.volume * 100);
  $("#volumeVal").textContent = `${Math.round(ed.volume * 100)}%`;
  $("#fadeIn").value = ed.fadeIn;
  $("#fadeInVal").textContent = `${ed.fadeIn.toFixed(1)}s`;
  $("#fadeOut").value = ed.fadeOut;
  $("#fadeOutVal").textContent = `${ed.fadeOut.toFixed(1)}s`;
  $("#mute").checked = ed.mute;
  video.playbackRate = ed.speed;
  video.volume = Math.min(ed.volume, 1);
  video.muted = ed.mute;
  const m = ed.music;
  $("#musicBox").classList.toggle("on", !!m);
  $("#musicName").textContent = m ? `${m.name} · ${fmt(m.duration)}` : "No background music";
  $("#musicName").title = m ? m.path : "";
  if (m) {
    $("#musicVol").value = Math.round(m.volume * 100);
    $("#musicVolVal").textContent = `${Math.round(m.volume * 100)}%`;
    $("#musicOffset").max = Math.max(0, m.duration - 0.5).toFixed(1);
    $("#musicOffset").value = m.offset;
    $("#musicOffsetVal").textContent = fmt(m.offset).replace(/\.\d$/, "");
    $("#musicLoop").checked = m.loop;
    musicEl.volume = Math.min(m.volume, 1);
  }
  const t = ed.text || defaultText();
  if (document.activeElement !== $("#txtContent")) $("#txtContent").value = t.content;
  if (t.font && $("#txtFont").value !== t.font) $("#txtFont").value = t.font;
  $("#txtSize").value = t.size;
  $("#txtSizeVal").textContent = t.size;
  $("#txtOpacity").value = Math.round(t.opacity * 100);
  $("#txtOpacityVal").textContent = `${Math.round(t.opacity * 100)}%`;
  $("#txtOutline").value = t.outline;
  $("#txtOutlineVal").textContent = t.outline;
  $("#txtColor").value = t.color;
  $("#txtOutlineColor").value = t.outlineColor;
  $("#txtBox").checked = t.box;
  $("#txtBoxColor").value = t.boxColor;
  $("#txtBoxOpacity").value = Math.round(t.boxOpacity * 100);
  $("#txtBoxOpacityVal").textContent = `${Math.round(t.boxOpacity * 100)}%`;
  $("#txtBoxRow").classList.toggle("disabled", !t.box);
  const g = ed.logo;
  $("#logoBox").classList.toggle("on", !!g);
  $("#logoName").textContent = g ? g.name : "No logo";
  if (g) {
    $("#logoSize").value = g.size;
    $("#logoSizeVal").textContent = `${g.size}%`;
    $("#logoOpacity").value = Math.round(g.opacity * 100);
    $("#logoOpacityVal").textContent = `${Math.round(g.opacity * 100)}%`;
  }
  for (const grid of $$(".pos-grid")) {
    const o = grid.dataset.target === "text" ? t : g;
    grid.querySelectorAll("button").forEach((b) => b.classList.toggle("on", !!o
      && Math.abs(o.x - Number(b.dataset.x)) < 0.02 && Math.abs(o.y - Number(b.dataset.y)) < 0.02));
  }
  $("#edUndo").disabled = editHistory.index <= 0;
  $("#edRedo").disabled = editHistory.index >= editHistory.stack.length - 1;
  if (!editHistory.restoring && ed.loaded) scheduleCommit();
  $$("#cropBox i").forEach((h) => (h.style.display = ratioValue() && h.dataset.h.length === 1 ? "none" : ""));
  const ready = ed.loaded && !ed.exporting && !state.busy;
  $("#edExport").disabled = !ready || zoomStraddles();
  $("#edStripOne").disabled = !ready;
  $("#edReset").disabled = !ed.loaded || ed.exporting;
  $("#edStripBatch").disabled = ed.exporting || state.busy;
  $("#edExport").classList.toggle("hidden", ed.exporting);
  $("#edCancel").classList.toggle("hidden", !ed.exporting);
  $("#edSide").classList.toggle("disabled", !ed.loaded);
  kick();
}

function resetEdits() {
  Object.assign(ed, { rotate: 0, flipH: false, flipV: false, ratio: "original", zoom: 1, panX: 0.5, panY: 0.5,
    bg: "blur", zoomAnimate: false, zoomEnd: 1.3, zoomEase: true, color: {}, speed: 1, volume: 1, mute: false,
    fadeIn: 0, fadeOut: 0, music: null, text: null, logo: null, start: 0, end: ed.info ? ed.info.duration : 0 });
  logoImg = null;
  musicEl.pause();
  musicEl.removeAttribute("src");
  if (ed.info) fitCrop(ratioValue());
  syncPanel();
}

$("#ratioChips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-r]");
  if (!b || !ed.loaded) return;
  ed.ratio = b.dataset.r;
  if (ed.ratio !== "free") fitCrop(ratioValue());
  syncPanel();
});

function rotateBy(deg) {
  if (!ed.loaded) return;
  ed.rotate = (ed.rotate + deg + 360) % 360;
  fitCrop(ratioValue());
  syncPanel();
}
$("#rotL").addEventListener("click", () => rotateBy(-90));
$("#rotR").addEventListener("click", () => rotateBy(90));
$("#flipH").addEventListener("click", () => { ed.flipH = !ed.flipH; syncPanel(); });
$("#flipV").addEventListener("click", () => { ed.flipV = !ed.flipV; syncPanel(); });
$("#zoom").addEventListener("input", (e) => {
  ed.zoom = Number(e.target.value) / 100;
  syncPanel();
});
$("#bgChips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-bg]");
  if (!b) return;
  ed.bg = b.dataset.bg;
  syncPanel();
});
$("#bgColor").addEventListener("input", (e) => {
  ed.bgColor = e.target.value;
  ed.bg = "color";
  syncPanel();
});
$("#zoomAnim").addEventListener("change", (e) => { ed.zoomAnimate = e.target.checked; syncPanel(); });
$("#zoomEnd").addEventListener("input", (e) => { ed.zoomEnd = Number(e.target.value) / 100; syncPanel(); });
$("#zoomEase").addEventListener("change", (e) => { ed.zoomEase = e.target.checked; syncPanel(); });

/* color sliders + presets */
$("#colorSliders").innerHTML = COLOR_SLIDERS.map(([key, label, min = -100]) =>
  `<div class="slider-row"${key === "sharpen" ? ' title="Applied on export (not shown in the preview)"' : ""}>
    <span>${label}</span><input type="range" id="c_${key}" min="${min}" max="100" step="1" value="0"><b class="mono" id="c_${key}_val">0</b>
  </div>`).join("");
$("#presetChips").innerHTML = Object.keys(PRESETS).map((name) => `<button data-p="${name}">${name}</button>`).join("");
for (const [key] of COLOR_SLIDERS) {
  const input = $(`#c_${key}`);
  input.addEventListener("input", () => { ed.color = { ...ed.color, [key]: Number(input.value) }; syncPanel(); });
  input.addEventListener("dblclick", () => { ed.color = { ...ed.color, [key]: 0 }; syncPanel(); });
}
$("#presetChips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-p]");
  if (!b) return;
  ed.color = { ...PRESETS[b.dataset.p] };
  syncPanel();
});
$("#colorReset").addEventListener("click", () => { ed.color = {}; syncPanel(); });
const compare = (on) => { ed.comparing = on; $("#edCompare").classList.toggle("on", on); kick(); };
$("#edCompare").addEventListener("pointerdown", () => compare(true));
["pointerup", "pointerleave", "pointercancel"].forEach((ev) => $("#edCompare").addEventListener(ev, () => compare(false)));

/* speed + audio */
$("#speed").addEventListener("input", (e) => { ed.speed = Number(e.target.value) / 100; syncPanel(); });
$("#speedChips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-s]");
  if (!b) return;
  ed.speed = Number(b.dataset.s);
  syncPanel();
});
$("#volume").addEventListener("input", (e) => { ed.volume = Number(e.target.value) / 100; syncPanel(); });
$("#fadeIn").addEventListener("input", (e) => { ed.fadeIn = Number(e.target.value); syncPanel(); });
$("#fadeOut").addEventListener("input", (e) => { ed.fadeOut = Number(e.target.value); syncPanel(); });
$("#mute").addEventListener("change", (e) => { ed.mute = e.target.checked; syncPanel(); });

/* text overlay */
function setText(patch) {
  ed.text = { ...(ed.text || defaultText()), ...patch };
  syncPanel();
}
$("#txtContent").addEventListener("input", (e) => setText({ content: e.target.value }));
$("#txtFont").addEventListener("change", async (e) => { setText({ font: e.target.value }); await ensureFont(e.target.value); });
$("#txtSize").addEventListener("input", (e) => setText({ size: Number(e.target.value) }));
$("#txtOpacity").addEventListener("input", (e) => setText({ opacity: Number(e.target.value) / 100 }));
$("#txtOutline").addEventListener("input", (e) => setText({ outline: Number(e.target.value) }));
$("#txtColor").addEventListener("input", (e) => setText({ color: e.target.value }));
$("#txtOutlineColor").addEventListener("input", (e) => setText({ outlineColor: e.target.value }));
$("#txtBox").addEventListener("change", (e) => setText({ box: e.target.checked }));
$("#txtBoxColor").addEventListener("input", (e) => setText({ boxColor: e.target.value }));
$("#txtBoxOpacity").addEventListener("input", (e) => setText({ boxOpacity: Number(e.target.value) / 100 }));
for (const grid of $$(".pos-grid")) {
  grid.innerHTML = POS_STEPS.flatMap((y) => POS_STEPS.map((x) => `<button data-x="${x}" data-y="${y}" title="Place here"></button>`)).join("");
  grid.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const pos = { x: Number(b.dataset.x), y: Number(b.dataset.y) };
    if (grid.dataset.target === "text") setText(pos);
    else if (ed.logo) { ed.logo = { ...ed.logo, ...pos }; syncPanel(); }
  });
}

/* logo overlay */
async function setLogo(path) {
  const res = await api.editor_pick_logo(path || null);
  if (!res.ok) {
    if (!res.cancelled) toast("error", "Can't add logo", res.error);
    return;
  }
  ed.logo = { path: res.path, name: res.name, url: res.url, size: ed.logo?.size || 20, opacity: ed.logo?.opacity ?? 1,
    x: ed.logo?.x ?? 0.97, y: ed.logo?.y ?? 0.03 };
  loadLogoImage();
  syncPanel();
}
function loadLogoImage() {
  if (!ed.logo) { logoImg = null; return; }
  if (logoImg?.dataset?.src === ed.logo.url) return;
  logoImg = new Image();
  logoImg.dataset.src = ed.logo.url;
  logoImg.crossOrigin = "anonymous";
  logoImg.onload = kick;
  logoImg.src = ed.logo.url;
}
$("#logoAdd").addEventListener("click", () => setLogo());
$("#logoRemove").addEventListener("click", () => { ed.logo = null; logoImg = null; syncPanel(); });
$("#logoSize").addEventListener("input", (e) => { ed.logo = { ...ed.logo, size: Number(e.target.value) }; syncPanel(); });
$("#logoOpacity").addEventListener("input", (e) => { ed.logo = { ...ed.logo, opacity: Number(e.target.value) / 100 }; syncPanel(); });

/* background music */
async function setMusic(path) {
  const res = await api.editor_pick_music(path || null);
  if (!res.ok) {
    if (!res.cancelled) toast("error", "Can't add music", res.error);
    return;
  }
  ed.music = { path: res.path, name: res.name, duration: res.duration, url: res.url, volume: 1, offset: 0, loop: true };
  musicEl.src = res.url;
  syncPanel();
  syncMusic(true);
}
$("#musicAdd").addEventListener("click", () => setMusic());
$("#musicRemove").addEventListener("click", () => {
  ed.music = null;
  musicEl.pause();
  musicEl.removeAttribute("src");
  syncPanel();
});
$("#musicVol").addEventListener("input", (e) => { ed.music.volume = Number(e.target.value) / 100; syncPanel(); });
$("#musicOffset").addEventListener("input", (e) => { ed.music.offset = Number(e.target.value); syncPanel(); syncMusic(true); });
$("#musicLoop").addEventListener("change", (e) => { ed.music.loop = e.target.checked; syncPanel(); syncMusic(true); });
$("#edReset").addEventListener("click", resetEdits);

/* ---------- crop box drag / resize ---------- */
$("#cropBox").addEventListener("pointerdown", (e) => {
  if (!ed.loaded) return;
  e.preventDefault();
  const handle = e.target.dataset.h || "move";
  const start = { ...ed.crop };
  const px = e.clientX;
  const py = e.clientY;
  const [rw, rh] = frameSize();
  const r = ratioValue();
  const box = e.currentTarget;
  box.setPointerCapture(e.pointerId);

  const onMove = (ev) => {
    const dx = (ev.clientX - px) / ed.srcScale;
    const dy = (ev.clientY - py) / ed.srcScale;
    let { x, y, w, h } = start;
    if (handle === "move") {
      x = Math.min(Math.max(start.x + dx, 0), rw - w);
      y = Math.min(Math.max(start.y + dy, 0), rh - h);
    } else {
      const left = handle.includes("w");
      const top = handle.includes("n");
      const ax = left ? start.x + start.w : start.x; // anchor = opposite edge
      const ay = top ? start.y + start.h : start.y;
      const availW = left ? ax : rw - ax;
      const availH = top ? ay : rh - ay;
      if (handle.length === 2) {
        w = Math.min(Math.max((left ? -dx : dx) + start.w, MIN_CROP), availW);
        h = Math.min(Math.max((top ? -dy : dy) + start.h, MIN_CROP), availH);
        if (r) {
          w = Math.min(Math.max(w, h * r), availW, availH * r);
          h = w / r;
        }
        x = left ? ax - w : ax;
        y = top ? ay - h : ay;
      } else if (handle === "e" || handle === "w") {
        w = Math.min(Math.max((left ? -dx : dx) + start.w, MIN_CROP), availW);
        x = left ? ax - w : ax;
      } else {
        h = Math.min(Math.max((top ? -dy : dy) + start.h, MIN_CROP), availH);
        y = top ? ay - h : ay;
      }
    }
    ed.crop = { x, y, w, h };
    kick();
  };
  const onUp = () => {
    box.removeEventListener("pointermove", onMove);
    box.removeEventListener("pointerup", onUp);
    scheduleCommit();
  };
  box.addEventListener("pointermove", onMove);
  box.addEventListener("pointerup", onUp);
});

/* drag on the output preview moves the zoom focus (zoom in) or the video position (zoom out) */
function hit(box, x, y) {
  return box && x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h;
}

outCanvas.addEventListener("pointerdown", (e) => {
  if (!ed.loaded) return;
  const which = hit(overlayBoxes.text, e.offsetX, e.offsetY) ? "text" : hit(overlayBoxes.logo, e.offsetX, e.offsetY) ? "logo" : null;
  if (which) {
    e.preventDefault();
    outCanvas.setPointerCapture(e.pointerId);
    let lx = e.clientX;
    let ly = e.clientY;
    const move = (ev) => {
      const box = overlayBoxes[which];
      const o = which === "text" ? ed.text : ed.logo;
      if (!box || !o) return;
      const nx = box.freeW > 1 ? Math.min(Math.max(o.x + (ev.clientX - lx) / box.freeW, 0), 1) : o.x;
      const ny = box.freeH > 1 ? Math.min(Math.max(o.y + (ev.clientY - ly) / box.freeH, 0), 1) : o.y;
      lx = ev.clientX;
      ly = ev.clientY;
      if (which === "text") ed.text = { ...ed.text, x: nx, y: ny };
      else ed.logo = { ...ed.logo, x: nx, y: ny };
      kick();
    };
    const up = () => {
      outCanvas.removeEventListener("pointermove", move);
      outCanvas.removeEventListener("pointerup", up);
      syncPanel();
    };
    outCanvas.addEventListener("pointermove", move);
    outCanvas.addEventListener("pointerup", up);
    return;
  }
  if (ed.zoom === 1 && (!ed.zoomAnimate || ed.zoomEnd === 1)) return;
  e.preventDefault();
  outCanvas.setPointerCapture(e.pointerId);
  let lx = e.clientX;
  let ly = e.clientY;
  const W = outCanvas.clientWidth;
  const H = outCanvas.clientHeight;
  const onMove = (ev) => {
    const dx = ev.clientX - lx;
    const dy = ev.clientY - ly;
    lx = ev.clientX;
    ly = ev.clientY;
    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
    const z = Math.max(currentZoom(), ed.zoomAnimate ? Math.max(ed.zoom, ed.zoomEnd) : ed.zoom);
    if (z > 1) {
      ed.panX = clamp(ed.panX - dx / (W * z), 0, 1);
      ed.panY = clamp(ed.panY - dy / (H * z), 0, 1);
    } else {
      const zo = Math.min(currentZoom(), 0.99);
      ed.panX = clamp(ed.panX + dx / (W * (1 - zo)), 0, 1);
      ed.panY = clamp(ed.panY + dy / (H * (1 - zo)), 0, 1);
    }
    kick();
  };
  const onUp = () => {
    outCanvas.removeEventListener("pointermove", onMove);
    outCanvas.removeEventListener("pointerup", onUp);
    scheduleCommit();
  };
  outCanvas.addEventListener("pointermove", onMove);
  outCanvas.addEventListener("pointerup", onUp);
});

/* ---------- playback & trim timeline ---------- */
const fmt = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60);
  const s = (t % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
};

function updateTimeline() {
  const d = ed.info?.duration || 0;
  if (!d) return;
  const pct = (t) => `${(t / d) * 100}%`;
  $("#tlRange").style.left = pct(ed.start);
  $("#tlRange").style.width = pct(ed.end - ed.start);
  $("#tlStart").style.left = pct(ed.start);
  $("#tlEnd").style.left = pct(ed.end);
  $("#tlHead").style.left = pct(video.currentTime);
  $("#edTime").textContent = fmt(video.currentTime);
  $("#edTrim").textContent = `${fmt(ed.start)} – ${fmt(ed.end)}  (${fmt(ed.end - ed.start)})`;
}

function timeAt(clientX) {
  const r = $("#timeline").getBoundingClientRect();
  return Math.min(Math.max((clientX - r.left) / r.width, 0), 1) * (ed.info?.duration || 0);
}

$("#timeline").addEventListener("pointerdown", (e) => {
  if (!ed.loaded) return;
  e.preventDefault();
  const tl = e.currentTarget;
  tl.setPointerCapture(e.pointerId);
  const target = e.target.id === "tlStart" ? "start" : e.target.id === "tlEnd" ? "end" : "seek";
  const apply = (ev) => {
    const t = timeAt(ev.clientX);
    if (target === "start") ed.start = Math.min(t, ed.end - 0.1);
    else if (target === "end") ed.end = Math.max(t, ed.start + 0.1);
    video.currentTime = target === "end" ? ed.end : target === "start" ? ed.start : Math.min(Math.max(t, ed.start), ed.end);
    kick();
  };
  apply(e);
  const onUp = () => {
    tl.removeEventListener("pointermove", apply);
    tl.removeEventListener("pointerup", onUp);
    scheduleCommit();
  };
  tl.addEventListener("pointermove", apply);
  tl.addEventListener("pointerup", onUp);
});

function togglePlay() {
  if (!ed.loaded) return;
  if (video.paused) {
    if (video.currentTime < ed.start || video.currentTime >= ed.end - 0.05) video.currentTime = ed.start;
    video.play();
  } else {
    video.pause();
  }
}
$("#edPlay").addEventListener("click", togglePlay);
video.addEventListener("play", () => { $("#edPlay").classList.add("on"); syncMusic(true); kick(); });
video.addEventListener("pause", () => { $("#edPlay").classList.remove("on"); syncMusic(true); kick(); });
video.addEventListener("seeked", () => { syncMusic(true); kick(); });
video.addEventListener("loadeddata", kick);
video.addEventListener("timeupdate", () => {
  if (!video.paused && video.currentTime >= ed.end) video.currentTime = ed.start; // loop inside the trim
});
video.addEventListener("error", () => {
  if (ed.loaded) toast("error", "Preview not supported", "This codec can't be previewed here, but exporting still works.");
});
document.addEventListener("keydown", (e) => {
  if (!ed.active || e.code !== "Space" || /INPUT|TEXTAREA/.test(document.activeElement?.tagName)) return;
  e.preventDefault();
  togglePlay();
});
new ResizeObserver(kick).observe($("#view-editor"));

/* ---------- open / export ---------- */
// caption suggestion: the exported file name without the extension and the " (edited)" suffix
function postExported(path, name) {
  const base = (name || path.split(/[\\/]/).pop()).replace(/\.[^.]+$/, "");
  openPostDialog(path, base.replace(/\s*\(edited\)(\s*\(\d+\))?$/, ""));
}
window.postExported = postExported;

function postLastExport() {
  if (ed.lastExport) postExported(ed.lastExport.path, ed.lastExport.name);
}
$("#edPostTT").addEventListener("click", postLastExport);
$("#edShowFile").addEventListener("click", () => { if (ed.lastExport) api.reveal_file(ed.lastExport.path); });

async function openInEditor(path) {
  setView("editor");
  const res = await api.editor_open(path || null);
  if (!res.ok) {
    if (!res.cancelled) toast("error", "Can't open video", res.error);
    return;
  }
  video.pause();
  Object.assign(ed, { loaded: true, path: res.path, info: res.info });
  $("#edName").textContent = res.name;
  $("#edName").title = res.path;
  const i = res.info;
  $("#edMeta").textContent = `${i.width}×${i.height} · ${i.vcodec}${i.acodec ? " + " + i.acodec : ""} · ${fmt(i.duration)} · ${(i.size / 1048576).toFixed(1)} MB`;
  $("#edOutName").value = res.suggestedName;
  $("#edEmpty").classList.remove("on");
  $(".ed-panes").classList.add("on");
  $(".ed-transport").classList.add("on");
  if (!fontList.length) {
    fontList = await api.list_fonts();
    $("#txtFont").innerHTML = fontList.map((f) => `<option value="${esc(f.path)}">${esc(f.name)}</option>`).join("");
    defaultFontPath = (fontList.find((f) => f.name === "Arial Bold") || fontList.find((f) => /^Arial/.test(f.name)) || fontList[0] || {}).path || "";
    loadPresets();
  }
  resetEdits();
  ensureFont(defaultFontPath);
  video.src = res.url;
  video.currentTime = 0;
  setEdStatus("Ready", 0);
  resetHistory();
}
window.openInEditor = openInEditor;

function editSpec() {
  const r = (v) => Math.round(v);
  return {
    rotate: ed.rotate, flipH: ed.flipH, flipV: ed.flipV,
    crop: { x: r(ed.crop.x), y: r(ed.crop.y), w: r(ed.crop.w), h: r(ed.crop.h) },
    zoom: ed.zoom, panX: ed.panX, panY: ed.panY,
    zoomAnimate: ed.zoomAnimate, zoomEnd: ed.zoomEnd, zoomEase: ed.zoomEase,
    background: ed.bg === "color" ? ed.bgColor : ed.bg,
    color: { ...ed.color },
    speed: ed.speed, volume: ed.volume, mute: ed.mute, fadeIn: ed.fadeIn, fadeOut: ed.fadeOut,
    music: ed.music ? { path: ed.music.path, volume: ed.music.volume, offset: ed.music.offset, loop: ed.music.loop } : null,
    text: textSpec(),
    logo: ed.logo ? { path: ed.logo.path, size: ed.logo.size, opacity: ed.logo.opacity, x: ed.logo.x, y: ed.logo.y } : null,
    start: ed.start, end: ed.end,
    stripMetadata: $("#edStrip").checked,
  };
}

/* text as ffmpeg should draw it: already wrapped to the output width */
function textSpec() {
  const t = ed.text;
  if (!t || !t.content.trim() || !t.font) return null;
  const [ow, oh] = outputSize();
  const L = textLayout(ow, oh);
  return { ...t, content: L.lines.join("\n"), raw: t.content };
}

/* ---------- undo / redo ---------- */
const editHistory = { stack: [], index: -1, timer: 0, restoring: false };

function snapshot() {
  return JSON.stringify(Object.fromEntries(HIST_KEYS.map((k) => [k, ed[k]])));
}

function commitNow() {
  clearTimeout(editHistory.timer);
  const snap = snapshot();
  if (editHistory.stack[editHistory.index] === snap) return;
  editHistory.stack = editHistory.stack.slice(0, editHistory.index + 1);
  editHistory.stack.push(snap);
  if (editHistory.stack.length > 100) editHistory.stack.shift();
  editHistory.index = editHistory.stack.length - 1;
  $("#edUndo").disabled = editHistory.index <= 0;
  $("#edRedo").disabled = true;
}

function scheduleCommit() {
  clearTimeout(editHistory.timer);
  editHistory.timer = setTimeout(commitNow, 400);
}

function restoreSnapshot(snap) {
  editHistory.restoring = true;
  const prevMusic = ed.music?.url;
  Object.assign(ed, JSON.parse(snap));
  if (ed.music?.url !== prevMusic) {
    if (ed.music) musicEl.src = ed.music.url;
    else { musicEl.pause(); musicEl.removeAttribute("src"); }
  }
  loadLogoImage();
  if (ed.text?.font) ensureFont(ed.text.font);
  syncPanel();
  syncMusic(true);
  editHistory.restoring = false;
}

function undo() {
  commitNow();
  if (editHistory.index <= 0) return;
  editHistory.index -= 1;
  restoreSnapshot(editHistory.stack[editHistory.index]);
}

function redo() {
  if (editHistory.index >= editHistory.stack.length - 1) return;
  editHistory.index += 1;
  restoreSnapshot(editHistory.stack[editHistory.index]);
}

function resetHistory() {
  editHistory.stack = [snapshot()];
  editHistory.index = 0;
  syncPanel();
}

$("#edUndo").addEventListener("click", undo);
$("#edRedo").addEventListener("click", redo);
$("#edKeys").addEventListener("click", () => $("#kbdPop").classList.toggle("on"));

/* ---------- keyboard shortcuts ---------- */
document.addEventListener("keydown", (e) => {
  if (!ed.active || $("#view-editor").dataset.mode !== "edit") return;
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  const ctrl = e.ctrlKey || e.metaKey;
  const key = e.key.toLowerCase();
  if (ctrl && key === "o") { e.preventDefault(); openInEditor(); return; }
  if (typing || !ed.loaded) return;
  if (ctrl && key === "z" && !e.shiftKey) { e.preventDefault(); undo(); return; }
  if (ctrl && (key === "y" || (key === "z" && e.shiftKey))) { e.preventDefault(); redo(); return; }
  if (ctrl && key === "e") { e.preventDefault(); if (!$("#edExport").disabled) $("#edExport").click(); return; }
  if (ctrl || e.altKey) return;
  const step = e.shiftKey ? 1 : 1 / (ed.info.fps || 30);
  if (key === "arrowleft" || key === "arrowright") {
    e.preventDefault();
    video.pause();
    video.currentTime = Math.min(Math.max(video.currentTime + (key === "arrowleft" ? -step : step), 0), ed.info.duration);
  } else if (key === "i") { ed.start = Math.min(video.currentTime, ed.end - 0.1); syncPanel(); }
  else if (key === "o") { ed.end = Math.max(video.currentTime, ed.start + 0.1); syncPanel(); }
  else if (key === "r") rotateBy(e.shiftKey ? -90 : 90);
  else if (key === "h") { ed.flipH = !ed.flipH; syncPanel(); }
  else if (key === "v") { ed.flipV = !ed.flipV; syncPanel(); }
  else if (key === "m") { ed.mute = !ed.mute; syncPanel(); }
  else if (key === "c" && !e.repeat) compare(true);
});
document.addEventListener("keyup", (e) => {
  if (e.key.toLowerCase() === "c" && ed.comparing) compare(false);
});

/* ---------- presets ---------- */
let presetCache = [];
async function loadPresets() {
  presetCache = await api.presets_list();
  const options = presetCache.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  $("#presetSelect").innerHTML = `<option value="">— saved presets —</option>${options}`;
  window.onPresetsChanged?.(presetCache);
}
window.loadPresets = loadPresets;
window.presetCacheList = () => presetCache;

function makePreset() {
  const [rw, rh] = frameSize();
  const c = ed.crop;
  return {
    rotate: ed.rotate, flipH: ed.flipH, flipV: ed.flipV, ratio: ed.ratio,
    cropRel: { x: c.x / rw, y: c.y / rh, w: c.w / rw, h: c.h / rh },
    zoom: ed.zoom, panX: ed.panX, panY: ed.panY, zoomAnimate: ed.zoomAnimate, zoomEnd: ed.zoomEnd, zoomEase: ed.zoomEase,
    background: ed.bg === "color" ? ed.bgColor : ed.bg, color: { ...ed.color },
    speed: ed.speed, volume: ed.volume, mute: ed.mute, fadeIn: ed.fadeIn, fadeOut: ed.fadeOut,
    music: ed.music ? { path: ed.music.path, volume: ed.music.volume, offset: ed.music.offset, loop: ed.music.loop } : null,
    text: textSpec(), logo: editSpec().logo, stripMetadata: $("#edStrip").checked,
  };
}

async function applyPreset(preset) {
  const res = await api.preset_resolve(preset, ed.path);
  if (!res.ok) return toast("error", "Can't apply preset", res.error);
  const sp = res.spec;
  const bg = sp.background || "blur";
  Object.assign(ed, {
    rotate: sp.rotate || 0, flipH: !!sp.flipH, flipV: !!sp.flipV, ratio: sp.ratio || "original", crop: sp.crop,
    zoom: sp.zoom ?? 1, panX: sp.panX ?? 0.5, panY: sp.panY ?? 0.5, zoomAnimate: !!sp.zoomAnimate, zoomEnd: sp.zoomEnd ?? 1.3,
    zoomEase: sp.zoomEase ?? true, bg: bg.startsWith("#") ? "color" : bg, bgColor: bg.startsWith("#") ? bg : ed.bgColor,
    color: { ...(sp.color || {}) }, speed: sp.speed ?? 1, volume: sp.volume ?? 1, mute: !!sp.mute,
    fadeIn: sp.fadeIn || 0, fadeOut: sp.fadeOut || 0,
    text: sp.text ? { ...defaultText(), ...sp.text, content: sp.text.raw ?? sp.text.content } : null,
  });
  $("#edStrip").checked = sp.stripMetadata ?? true;
  ed.music = null;
  musicEl.pause();
  musicEl.removeAttribute("src");
  if (sp.music?.path) {
    await setMusic(sp.music.path);
    if (ed.music) Object.assign(ed.music, { volume: sp.music.volume ?? 1, offset: sp.music.offset || 0, loop: sp.music.loop ?? true });
  }
  ed.logo = null;
  if (sp.logo?.path) {
    await setLogo(sp.logo.path);
    if (ed.logo) Object.assign(ed.logo, { size: sp.logo.size, opacity: sp.logo.opacity, x: sp.logo.x, y: sp.logo.y });
  }
  if (ed.text?.font) await ensureFont(ed.text.font);
  syncPanel();
  syncMusic(true);
}

$("#presetSave").addEventListener("click", async () => {
  if (!ed.loaded) return;
  const name = $("#presetName").value.trim();
  const res = await api.preset_save(name, makePreset());
  if (!res.ok) return toast("error", "Can't save preset", res.error);
  $("#presetName").value = "";
  await loadPresets();
  $("#presetSelect").value = name;
  toast("success", "Preset saved", `"${name}" — use it in Batch to edit many videos the same way.`);
});
$("#presetApply").addEventListener("click", () => {
  const p = presetCache.find((x) => x.name === $("#presetSelect").value);
  if (p && ed.loaded) applyPreset(p.preset);
});
$("#presetDelete").addEventListener("click", (e) => {
  const name = $("#presetSelect").value;
  if (!name) return;
  confirmButton(e.currentTarget, "?", async () => {
    await api.preset_delete(name);
    await loadPresets();
    toast("info", "Preset deleted", name);
  });
});

/* ---------- drag & drop (paths come from pywebview) ---------- */
handlers.filesDropped = async (e) => {
  const videos = e.files.filter((f) => f.kind === "video");
  const audio = e.files.find((f) => f.kind === "audio");
  const image = e.files.find((f) => f.kind === "image");
  if (state.view !== "editor") {
    if (!videos.length) return toast("info", "Open the Editor first", "Music and logo files can be dropped while editing a video.");
    setView("editor");
  }
  const mode = $("#view-editor").dataset.mode;
  if (mode === "batch") return window.batchAdd(videos.map((f) => f.path));
  if (mode === "merge") return window.mergeAdd(videos.map((f) => f.path));
  if (videos.length) await openInEditor(videos[0].path);
  if (audio && ed.loaded) await setMusic(audio.path);
  if (image && ed.loaded) await setLogo(image.path);
};

function setEdStatus(text, value) {
  $("#edStatus").textContent = text;
  $("#edTrack").classList.toggle("indeterminate", value === null);
  if (typeof value === "number") $("#edFill").style.width = `${Math.round(value * 100)}%`;
}

$("#edOpen").addEventListener("click", () => openInEditor());
$("#edExport").addEventListener("click", async () => {
  video.pause();
  const res = await api.editor_export(ed.path, editSpec(), $("#edOutName").value);
  if (!res.ok) return toast("error", "Can't export", res.error);
  ed.exporting = true;
  setEdStatus("Starting…", null);
  syncPanel();
});
$("#edCancel").addEventListener("click", () => api.editor_cancel());

function confirmButton(btn, label, action) {
  if (!btn.dataset.armed) {
    btn.dataset.armed = btn.innerHTML;
    btn.textContent = label;
    btn.classList.add("confirm");
    setTimeout(() => {
      if (btn.dataset.armed) { btn.innerHTML = btn.dataset.armed; delete btn.dataset.armed; btn.classList.remove("confirm"); }
    }, 3500);
    return;
  }
  btn.innerHTML = btn.dataset.armed;
  delete btn.dataset.armed;
  btn.classList.remove("confirm");
  action();
}

let reloadAfterStrip = false;
$("#edStripOne").addEventListener("click", (e) => confirmButton(e.currentTarget, "Rewrite this file? Click again", async () => {
  // release the preview's open handle, otherwise Windows won't let the file be replaced
  video.pause();
  video.removeAttribute("src");
  video.load();
  reloadAfterStrip = true;
  const res = await api.strip_files([ed.path]);
  if (!res.ok) {
    reloadAfterStrip = false;
    openInEditor(ed.path);
    toast("error", "Can't remove metadata", res.error);
  }
}));
$("#edStripBatch").addEventListener("click", async () => {
  const res = await api.strip_files(null);
  if (!res.ok && !res.cancelled) toast("error", "Can't remove metadata", res.error);
});

Object.assign(handlers, {
  editEncoder: (e) => { $("#edEncoder").textContent = `Encoder: ${e.label}`; },
  editProgress: (e) => setEdStatus(e.text, e.value),
  editDone: (e) => {
    ed.exporting = false;
    setEdStatus(`Saved ${e.width}×${e.height} · ${e.encoder}`, 1);
    syncPanel();
    ed.lastExport = { path: e.path, name: e.name };
    $("#edDoneName").textContent = e.name;
    $("#edDoneName").title = e.path;
    $("#edDone").hidden = false;
    $("#edDone").scrollIntoView({ block: "nearest", behavior: "smooth" });
    toast("success", "Export finished", e.name, { label: "Post to TikTok", run: postLastExport });
  },
  editCancelled: () => {
    ed.exporting = false;
    setEdStatus("Cancelled", 0);
    syncPanel();
  },
  stripDone: (e) => {
    if (reloadAfterStrip) {
      reloadAfterStrip = false;
      openInEditor(ed.path);
    }
    setEdStatus(`Metadata removed from ${e.ok}/${e.total} file${e.total > 1 ? "s" : ""}`, 1);
    if (e.failed.length) toast("error", `Metadata removed from ${e.ok}/${e.total}`, e.failed.join("\n"));
    else toast("success", `Metadata removed from ${e.total} file${e.total > 1 ? "s" : ""}`, "Files were rewritten in place, quality unchanged.");
  },
});

/* a failed export ends with the generic "error" event; the "busy" event resets the panel */
const baseBusy = handlers.busy;
handlers.busy = (e) => {
  baseBusy(e);
  if (!e.busy && ed.exporting) {
    ed.exporting = false;
    setEdStatus("Export failed", 0);
  }
  syncPanel();
};

window.onEditorView = (active) => {
  ed.active = active;
  if (!active) video.pause();
  syncMusic(true);
  kick();
};

$("#edEmpty").classList.add("on");
syncPanel();
