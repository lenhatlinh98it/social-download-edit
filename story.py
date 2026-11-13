"""Story video: an animated vertical video from AI pictures, a narration and an optional music bed.

A project is a folder with project.json:
    {
      "title": str,
      "voice": "voice.wav",                 # the whole narration, one file
      "music": "music.wav" | null,          # optional bed, ducked under the voice
      "options": {"captions": true, "musicVolume": 0.22, "musicStart": 0, "width": 1080, "height": 1920},
      "scenes": [{
          "text": "Day one. I was glued to my phone.",   # the narration of this scene (to time the cut)
          "image": "images/clean/003.jpg",                # relative to the project folder
          "title": "100 DAYS TO\\nLEVEL UP MY LIFE",      # optional big text over the scene
          "hud": {"left": "DAY 1", "right": "LV.1", "bar": 0.01},   # optional counter + progress bar
          "pop": "LEVEL UP!",                             # optional burst: flash, shake, popping text
          "transition": "cut" | "whip"                    # how this scene comes in
      }]
    }
Scene timing comes from the narration's word timestamps (Whisper): a scene starts just before its first word.
"""
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

import editor

FPS = 30
LEAD = 0.12  # cut a moment before the first word of a scene
WHIP = 0.22


class Cancelled(Exception):
    pass


# ---------- pictures ----------
def read_image(path):
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)  # unicode-safe


def write_image(path, img):
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if ok:
        buf.tofile(path)


def _found_mark(img):
    """Mask of a bright compact star near the bottom-right corner, or None."""
    h, w = img.shape[:2]
    x0, x1, y0, y1 = int(w * 0.87), int(w * 0.98), int(h * 0.925), int(h * 0.99)
    roi = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    lift = roi.astype(np.int16) - cv2.medianBlur(roi, 41).astype(np.int16)
    n, lab, st, cent = cv2.connectedComponentsWithStats((lift > 10).astype(np.uint8), connectivity=8)
    exp = (0.925 * w - x0, 0.958 * h - y0)
    best, best_d = None, 0.04 * w
    for i in range(1, n):
        bw, bh = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if st[i, cv2.CC_STAT_AREA] < 0.0003 * w * w or not 0.6 < bw / max(bh, 1) < 1.6:
            continue
        d = math.hypot(cent[i][0] - exp[0], cent[i][1] - exp[1])
        if d < best_d:
            best, best_d = i, d
    if best is None:
        return None
    m = np.zeros((h, w), np.uint8)
    m[y0:y1, x0:x1][lab == best] = 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    return cv2.dilate(m, np.ones((7, 7), np.uint8), iterations=2)


def _star_mask(w, h):
    """The corner sparkle of Flow / Gemini 9:16 pictures sits at a fixed spot (measured on 21 pictures:
    centre 0.9261 w, 0.9587 h, 0.0306 w across)."""
    cx, cy, r = 0.9261 * w, 0.9587 * h, 0.0306 * w / 2 * 1.25
    pts = [(cx + (r if k % 2 == 0 else r * 0.38) * math.cos(-math.pi / 2 + k * math.pi / 4),
            cy + (r if k % 2 == 0 else r * 0.38) * math.sin(-math.pi / 2 + k * math.pi / 4)) for k in range(8)]
    m = np.zeros((h, w), np.uint8)
    cv2.fillPoly(m, [np.array(pts, np.int32)], 255, cv2.LINE_AA)
    return cv2.dilate(m, np.ones((5, 5), np.uint8))


def remove_corner_mark(img):
    """(picture without the corner sparkle, whether one was removed). The posts are labelled as AI-generated."""
    h, w = img.shape[:2]
    mask = _star_mask(w, h) if abs(w / h - 9 / 16) < 0.02 else _found_mark(img)
    if mask is None:
        return img, False
    return cv2.inpaint(img, mask, 9, cv2.INPAINT_TELEA), True


def clean_file(src, dst):
    out, found = remove_corner_mark(read_image(src))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    write_image(dst, out)
    return found


# ---------- timing ----------
_NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
        "eight": "8", "nine": "9", "ten": "10", "hundred": "100"}


def _tokens(text):
    out = []
    for t in re.findall(r"[\w']+", text.lower()):
        out.append(_NUM.get(t, t))
    return out


def scene_starts(scenes, words, total):
    """Start time of each scene from the narration's word timestamps [(start, end, word)].
    The script and the transcript differ a little ("five A.M." vs "5AM"), so they are aligned word by word."""
    script, owner = [], []
    for i, s in enumerate(scenes):
        toks = _tokens(s.get("text") or "")
        script += toks
        owner += [i] * len(toks)
    heard = [_tokens(w)[0] if _tokens(w) else "" for _, _, w in words]
    to_word = {}
    for a, b, n in difflib.SequenceMatcher(None, script, heard, autojunk=False).get_matching_blocks():
        for k in range(n):
            to_word[a + k] = b + k
    starts = []
    for i in range(len(scenes)):
        idx = [k for k, o in enumerate(owner) if o == i]
        t = next((words[to_word[k]][0] for k in idx if k in to_word), None)
        starts.append(t)
    # scenes whose words were not heard: spread them between their neighbours
    starts[0] = 0.0
    for i in range(1, len(starts)):
        if starts[i] is None or starts[i] <= starts[i - 1]:
            j = next((k for k in range(i + 1, len(starts)) if starts[k] is not None and starts[k] > starts[i - 1]), None)
            nxt = starts[j] if j is not None else total
            span = (j if j is not None else len(starts)) - i + 1
            starts[i] = starts[i - 1] + (nxt - starts[i - 1]) / span
        else:
            starts[i] = max(starts[i - 1] + 0.5, starts[i] - LEAD)
    return [round(float(s), 3) for s in starts]


def caption_groups(words, end, breaks=()):
    """Groups of 1-4 spoken words, broken at sentence ends, pauses and scene cuts (`breaks`: scene starts -
    Whisper may drop the full stop between two sentences): [(start, end, [(ws, we, word)])]."""
    merged = []
    for w in words:  # Whisper splits "push-up" into "push" + "-up"
        if merged and w[2].strip().startswith("-"):
            merged[-1] = (merged[-1][0], w[1], merged[-1][2].rstrip() + w[2].strip())
        else:
            merged.append(w)
    words = merged
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        cut = nxt is not None and any(w[0] < b <= nxt[0] + LEAD for b in breaks)
        if len(cur) >= 4 or w[2].rstrip().endswith((".", ",", "!", "?")) or nxt is None or nxt[0] - w[1] > 0.35 or cut:
            groups.append(cur)
            cur = []
    out = []
    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0][0] if gi + 1 < len(groups) else end
        out.append((g[0][0], min(g_end, g[-1][1] + 0.6), g))
    return out


# ---------- picture motion ----------
def _ease_out(u):
    u = min(max(u, 0.0), 1.0)
    return 1 - (1 - u) ** 3


def _window(src, W, H, zoom, fx, fy, dx=0.0, dy=0.0):
    """`src` scaled to fill W x H, zoomed around (fx, fy), never past its edges."""
    sh, sw = src.shape[:2]
    s = max(W / sw, H / sh) * zoom
    cx = min(max(fx * sw, W / (2 * s)), sw - W / (2 * s))
    cy = min(max(fy * sh, H / (2 * s)), sh - H / (2 * s))
    m = np.array([[s, 0, W / 2 - cx * s + dx], [0, s, H / 2 - cy * s + dy]], np.float32)
    return cv2.warpAffine(src, m, (W, H), flags=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REFLECT)


def scene_frame(i, scene, img, t, d, W, H):
    """Slow push / pull (alternating), a punch-in on the cut, shake + flash on a pop."""
    drift = 1.0 + 0.07 * (t / d) if i % 2 == 0 else 1.07 - 0.07 * (t / d)
    punch = 1 + 0.12 * (1 - _ease_out(t / 0.28))
    dx = dy = 0.0
    if scene.get("pop") and t < 0.35:
        k = (1 - t / 0.35) * 18
        dx, dy = math.sin(t * 90) * k, math.cos(t * 77) * k
    out = _window(img, W, H, drift * punch, 0.5, 0.5 + (0.03 if i % 2 else -0.03) * (t / d), dx, dy)
    if scene.get("pop") and t < 0.14:
        out = cv2.addWeighted(out, 1, np.full_like(out, 255), 0.55 * (1 - t / 0.14), 0)
    return out


def whip(a, b, k):
    """`a` slides out to the left and `b` comes in from the right, with motion blur."""
    W = a.shape[1]
    off = int(_ease_out(k) * W)
    canvas = np.empty_like(a)
    canvas[:, :W - off] = a[:, off:]
    canvas[:, W - off:] = b[:, :off]
    return cv2.blur(canvas, (int(10 + 80 * math.sin(k * math.pi)) | 1, 1))


def _rounded(img, x0, y0, x1, y1, r, color):
    cv2.rectangle(img, (x0 + r, y0), (x1 - r, y1), color, -1, cv2.LINE_AA)
    cv2.rectangle(img, (x0, y0 + r), (x1, y1 - r), color, -1, cv2.LINE_AA)
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
        cv2.circle(img, (cx, cy), r, color, -1, cv2.LINE_AA)


def progress_bar(img, frac):
    W = img.shape[1]
    x0, y0, x1, y1 = 70, 262, W - 70, 294
    back = img.copy()
    _rounded(back, x0 - 4, y0 - 4, x1 + 4, y1 + 4, 18, (20, 20, 20))
    cv2.addWeighted(back, 0.75, img, 0.25, 0, img)
    fill = int(x0 + (x1 - x0) * min(max(frac, 0.02), 1))
    _rounded(img, x0, y0, fill, y1, 16, (40, 200, 255))
    cv2.line(img, (x0 + 14, y0 + 8), (max(x0 + 14, fill - 14), y0 + 8), (170, 240, 255), 4, cv2.LINE_AA)


# ---------- text (ASS, burned by ffmpeg) ----------
POP = r"{\fscx135\fscy135\t(0,160,\fscx100\fscy100)}"
GOLD = r"{\c&H0028C8FF&}"
WHITE = r"{\c&HFFFFFF&}"


def _t(sec):
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{sec % 60:05.2f}"


def _esc(text):
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def write_ass(path, scenes, starts, total, groups, W, H, captions=True):
    ev = []
    for i, s in enumerate(scenes):
        a, b = starts[i], starts[i + 1] if i + 1 < len(scenes) else total
        prev = scenes[i - 1] if i else {}
        if s.get("title"):
            lines = _esc(s["title"]).split("\\N")
            text = lines[0] + "".join("\\N" + GOLD + ln + WHITE for ln in lines[1:])
            below_hud = int(350 * W / 1080) if (s.get("hud") or {}).get("left") or (s.get("hud") or {}).get("right") else 0
            ev.append(f"Dialogue: 2,{_t(a)},{_t(b)},Title,,0,0,{below_hud},,{POP}{text}")  # under the counter
        hud = s.get("hud") or {}
        old = prev.get("hud") or {}
        if hud.get("left"):
            ev.append(f"Dialogue: 1,{_t(a)},{_t(b)},HudL,,0,0,0,,{POP if hud['left'] != old.get('left') else ''}{_esc(hud['left'])}")
        if hud.get("right"):
            ev.append(f"Dialogue: 1,{_t(a)},{_t(b)},HudR,,0,0,0,,{POP if hud['right'] != old.get('right') else ''}{_esc(hud['right'])}")
        if s.get("pop"):
            ev.append(f"Dialogue: 3,{_t(a)},{_t(min(b, a + 1.1))},Pop,,0,0,0,,"
                      r"{\fscx40\fscy40\t(0,180,\fscx115\fscy115)\t(180,320,\fscx100\fscy100)\fad(0,250)}" + _esc(s["pop"]))
    if captions:
        for g_start, g_end, g in groups:
            for wi, (ws, _, _) in enumerate(g):
                we = g[wi + 1][0] if wi + 1 < len(g) else g_end
                text = " ".join((GOLD + _esc(w.strip()) + WHITE) if k == wi else _esc(w.strip())
                                for k, (_, _, w) in enumerate(g))
                ev.append(f"Dialogue: 1,{_t(ws)},{_t(we)},Cap,,0,0,0,,{POP if wi == 0 else ''}{text.upper()}")
    k = W / 1080
    style = ("Style: {name},{font},{size},&H00{color},&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,{sp},0,1,"
             "{ol},{sh},{al},{ml},{ml},{mv},1")
    styles = [
        style.format(name="HudL", font="Impact", size=int(118 * k), color="FFFFFF", sp=2, ol=8, sh=4, al=7, ml=int(70 * k), mv=int(110 * k)),
        style.format(name="HudR", font="Impact", size=int(96 * k), color="28C8FF", sp=2, ol=8, sh=4, al=9, ml=int(70 * k), mv=int(122 * k)),
        style.format(name="Cap", font="Arial Black", size=int(84 * k), color="FFFFFF", sp=0, ol=8, sh=4, al=2, ml=int(70 * k), mv=int(300 * k)),
        style.format(name="Pop", font="Impact", size=int(150 * k), color="28C8FF", sp=3, ol=10, sh=6, al=5, ml=int(60 * k), mv=0),
        style.format(name="Title", font="Impact", size=int(128 * k), color="FFFFFF", sp=2, ol=9, sh=5, al=8, ml=int(60 * k), mv=int(230 * k)),
    ]
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding", *styles, "",
            "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + ev) + "\n")


# ---------- render ----------
def audio_length(ffprobe, path):
    return editor.probe_audio(ffprobe, path)["duration"]


def render(ffmpeg, ffprobe, project_dir, words, dst, on_progress=lambda _v: None, should_cancel=lambda: False,
           log=lambda _m: None):
    """Render the project to `dst`. `words`: the narration's word timestamps [(start, end, word)]."""
    with open(os.path.join(project_dir, "project.json"), encoding="utf-8") as f:
        project = json.load(f)
    opts = project.get("options") or {}
    W, H = int(opts.get("width") or 1080), int(opts.get("height") or 1920)
    scenes = project["scenes"]
    voice = os.path.join(project_dir, project["voice"])
    total = audio_length(ffprobe, voice) + 0.6
    starts = scene_starts(scenes, words, total)
    durs = [b - a for a, b in zip(starts, starts[1:] + [total])]
    log(f"{len(scenes)} scenes over {total:.1f} s")

    workdir = tempfile.mkdtemp(prefix="grabber-story-")
    try:
        write_ass(os.path.join(workdir, "text.ass"), scenes, starts, total, caption_groups(words, total, starts[1:]), W, H,
                  opts.get("captions", True))
        imgs = [read_image(os.path.join(project_dir, s["image"])) for s in scenes]
        label, enc_args, pix = editor._encoder_args(ffmpeg, W, H, FPS)
        music = project.get("music")
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-", "-i", voice]
        graph = [f"[0:v]ass=text.ass,format={pix}[v]"]
        if music:
            cmd += ["-stream_loop", "-1", "-ss", f"{float(opts.get('musicStart') or 0):.2f}",
                    "-i", os.path.join(project_dir, music)]
            vol = float(opts.get("musicVolume", 0.22))
            graph += [f"[2:a]aresample=48000,atrim=duration={total:.3f},volume={vol:.3f},"
                      f"afade=t=out:st={max(total - 1.2, 0):.3f}:d=1.2[mus]",
                      "[1:a]aresample=48000,asplit=2[vo][key]",
                      "[mus][key]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=300[duck]",
                      f"[vo][duck]amix=inputs=2:normalize=0,apad,atrim=duration={total:.3f},loudnorm=I=-14:TP=-1.5:LRA=9[a]"]
        else:
            graph.append(f"[1:a]aresample=48000,apad,atrim=duration={total:.3f},loudnorm=I=-14:TP=-1.5:LRA=9[a]")
        cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", *enc_args,
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", os.path.abspath(dst)]
        proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=editor.CREATE_NO_WINDOW)
        frames = int(round(total * FPS))
        try:
            for f in range(frames):
                if should_cancel():
                    raise Cancelled()
                t = f / FPS
                i = max(k for k, s in enumerate(starts) if s <= t + 1e-9)
                lt, d = t - starts[i], durs[i]
                out = scene_frame(i, scenes[i], imgs[i], lt, d, W, H)
                if i + 1 < len(scenes) and scenes[i + 1].get("transition") == "whip" and lt > d - WHIP:
                    nxt = scene_frame(i + 1, scenes[i + 1], imgs[i + 1], 0.0, durs[i + 1], W, H)
                    out = whip(out, nxt, (lt - (d - WHIP)) / WHIP)
                hud = scenes[i].get("hud") or {}
                if hud.get("bar") is not None:
                    before = ((scenes[i - 1].get("hud") or {}).get("bar") if i else None)
                    a = hud["bar"] if before is None else before
                    progress_bar(out, a + (hud["bar"] - a) * _ease_out(lt / 0.6))
                proc.stdin.write(out.tobytes())
                if f % 15 == 0:
                    on_progress(f / frames)
            proc.stdin.close()
            err = proc.stderr.read().decode("utf-8", "replace")
            if proc.wait() != 0:
                raise RuntimeError("ffmpeg failed: " + err.strip()[-300:])
        except BaseException:
            proc.kill()
            proc.wait()
            if os.path.exists(dst):
                os.remove(dst)
            raise
        on_progress(1.0)
        log(f"Rendered {total:.1f} s with {label}")
        return {"path": dst, "duration": total, "scenes": len(scenes), "starts": starts}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- planning: script -> scenes (Gemini, local Ollama when Gemini fails) ----------
DEFAULT_STYLE = "2D hand-drawn cartoon, clean bold outlines, flat colors with soft shading"

PLAN_PROMPT = """You are the storyboard artist of a vertical animated story video (TikTok / Shorts, 9:16).
The main character is {name}: {description}

Split the narration below into scenes and answer with JSON only:
{{"scenes": [{{"text": "...", "visual": "...", "title": "", "hud": null, "pop": "", "transition": "cut"}}]}}

Rules:
- "text": the narration of the scene, copied EXACTLY as written, every word, in order. Together the scenes must
  contain the whole narration. Cut at sentence ends; 1-2 short sentences per scene (2-5 seconds when spoken).
- "visual": one sentence describing the picture: what {name} does, where, the expression, the camera shot
  (close-up, wide shot, low angle...) and the light. Keep places consistent across scenes. Never ask for text.
- "title": only on the first scene (a 2-line hook in CAPITALS, lines separated by \n, max 5 words per line) and
  optionally on the last scene (a short call to action). Empty elsewhere.
{counter}- "pop": a short burst word like "LEVEL UP!" on at most 3 big milestone scenes, else "".
- "transition": "whip" when the story jumps in time or place, else "cut".

Narration:
{script}"""

COUNTER_RULE = """- "hud": when the story counts days / steps / levels, a counter shown on the scene:
  {{"left": "DAY 14", "right": "LV.17", "bar": 0.17}} (bar = progress 0..1, never going down). null when the
  scene has no count (the hook, the ending).
"""


def _json_from(text):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def _sentences(script):
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", script.strip()) if s.strip()]


def _clean_scenes(raw, script):
    scenes = []
    for s in raw.get("scenes") or []:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        hud = s.get("hud") if isinstance(s.get("hud"), dict) else None
        if hud:
            try:
                hud = {"left": str(hud.get("left") or "")[:16], "right": str(hud.get("right") or "")[:16],
                       "bar": min(max(float(hud.get("bar") or 0), 0), 1)}
            except (TypeError, ValueError):
                hud = None
        scenes.append({"text": text, "visual": str(s.get("visual") or text).strip(),
                       "title": str(s.get("title") or "").strip()[:60], "hud": hud,
                       "pop": str(s.get("pop") or "").strip()[:16],
                       "transition": "whip" if s.get("transition") == "whip" else "cut"})
    # the narration must survive the split word for word, or the timing breaks: else fall back to sentences
    if _tokens(" ".join(s["text"] for s in scenes)) != _tokens(script):
        return None
    return scenes


def plan_scenes(script, character, gemini_key, ollama_model, counter=True, log=lambda _m: None):
    """(scenes, engine). Gemini first; the local Ollama model when Gemini fails; plain sentences as a last resort."""
    import hashtags
    import smartcut
    prompt = PLAN_PROMPT.format(name=character.get("name") or "the hero", description=character.get("description") or "",
                                counter=COUNTER_RULE if counter else "", script=script.strip())
    if gemini_key:
        for model, _ in hashtags.ATTEMPTS:
            try:
                answer = hashtags._ask(gemini_key, model, prompt, False)
                scenes = _clean_scenes(_json_from(list(hashtags._texts(answer))[-1]), script)
                if scenes:
                    return scenes, f"Gemini ({model})"
                log(f"Gemini {model} changed the narration - trying again")
            except Exception as ex:  # noqa: BLE001 - busy, out of free requests, bad JSON: next one
                log(f"Gemini {model} failed: {type(ex).__name__} {str(ex)[:120]}")
    if ollama_model:
        try:
            log(f"Planning with the local model {ollama_model}…")
            scenes = _clean_scenes(_json_from(smartcut._ollama_chat(ollama_model, prompt, timeout=900)), script)
            if scenes:
                return scenes, f"Ollama ({ollama_model})"
            log("The local model changed the narration")
        except Exception as ex:  # noqa: BLE001
            log(f"Ollama failed: {type(ex).__name__} {str(ex)[:120]}")
    log("Using one scene per sentence (no AI planner answered)")
    return [{"text": s, "visual": s, "title": "", "hud": None, "pop": "", "transition": "cut"} for s in _sentences(script)], "sentences"


def character_prompt(description, style=DEFAULT_STYLE):
    return (f"{style} character sheet for an animated storytelling series. One character: {description}. "
            "Show the same character 4 times side by side, full body, same size, evenly spaced and not overlapping: "
            "1) front view, 2) three-quarter view, 3) side view, 4) back view. Neutral standing pose. "
            "Plain solid pure white background, no ground shadow, no text, no labels.")


def scene_prompt(scene, character, style=DEFAULT_STYLE, ratio="9:16"):
    name = character.get("name") or "the hero"
    return (f"Use the same {style} style and the same character {name} from the character sheet. "
            f"{'Vertical 9:16' if ratio == '9:16' else 'Horizontal 16:9'} frame, cinematic composition, expressive, "
            f"no text, no letters. Shot: {scene['visual'].rstrip('.')}.")


def narration_chunks(scenes, limit=700):
    """The narration in pieces of whole scenes, short enough for one TTS run each."""
    chunks, cur = [], ""
    for s in scenes:
        t = s["text"].strip()
        if cur and len(cur) + 1 + len(t) > limit:
            chunks.append(cur)
            cur = t
        else:
            cur = f"{cur} {t}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def join_audio(ffmpeg, parts, dst, gap=0.25):
    if len(parts) == 1:
        shutil.copyfile(parts[0], dst)
        return dst
    inputs, labels = [], []
    for i, p in enumerate(parts):
        inputs += ["-i", p]
        labels.append(f"[{i}:a]aresample=48000,apad=pad_dur={gap}[p{i}]")
    graph = ";".join(labels) + ";" + "".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    r = editor._run([ffmpeg, "-hide_banner", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[a]", dst])
    if r.returncode != 0:
        raise RuntimeError("Joining the narration failed: " + r.stderr.strip()[-200:])
    return dst
