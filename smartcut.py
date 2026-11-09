"""Smart cut (owner edition): turn a long landscape video into vertical 9:16 clips, all on this machine.

1. transcribe the speech with faster-whisper (GPU when CUDA is available, else CPU)
2. pick the moments: a local LLM served by Ollama reads the transcript, or heuristics (speech, loudness, scene cuts)
3. reframe: YuNet face detection drives a smoothed 9:16 crop window (centre crop when nobody is on screen)
4. render each clip with ffmpeg (optional burned-in captions from the word timestamps)
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import wave

import editor

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_LLM = "qwen2.5:7b"
WHISPER_MODELS = ("small", "medium", "large-v3-turbo")
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = f"https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/{YUNET_FILE}"
OUT_W, OUT_H = 1080, 1920
TRACK_STEP = 0.2  # seconds between face detections
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
SUB_FONTS = {"ja": "Yu Gothic UI", "zh": "Microsoft YaHei", "ko": "Malgun Gothic"}


class Cancelled(Exception):
    pass


def defaults():
    return {"style": "series", "target": 90, "count": 2, "partLabel": True, "reframe": "face", "subtitles": True, "subPos": "bottom", "whisper": "small",
            "language": "", "useLlm": True, "llmModel": DEFAULT_LLM}


# ---------- environment ----------
def _add_cuda_dlls():
    """CUDA 12 / cuDNN 9 from the pip wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12) live under site-packages/nvidia."""
    for base in sys.path:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass


def cuda_devices():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - no CUDA runtime
        return 0


def ollama_models(timeout=2):
    """Names of the models installed in Ollama, or None when Ollama is not running."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _video_key(path, *extra):
    st = os.stat(path)
    raw = "|".join([os.path.abspath(path).lower(), str(st.st_size), str(int(st.st_mtime)), *map(str, extra)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------- 1. speech ----------
def extract_audio(ffmpeg, src, dst):
    r = editor._run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                     "-f", "wav", dst])
    if r.returncode != 0:
        raise RuntimeError("Cannot read the audio: " + r.stderr.strip()[-200:])


_whisper_models = {}  # (name, device, compute) -> WhisperModel, kept for the next video


def _whisper(model_name, device, compute, models_dir):
    """Load a Whisper model once per app run: loading it again for the next video asks Hugging Face again and,
    with CUDA on Windows, freeing the previous model can hang - so a model is never dropped."""
    key = (model_name, device, compute)
    if key not in _whisper_models:
        from faster_whisper import WhisperModel
        _whisper_models[key] = WhisperModel(model_name, device=device, compute_type=compute,
                                            download_root=os.path.join(models_dir, "whisper"))
    return _whisper_models[key]


def transcribe(wav, model_name, models_dir, language, on_progress, should_cancel, log):
    """Segments [{start, end, text, words: [{start, end, word}]}] and the detected language."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    _add_cuda_dlls()
    attempts = [("cuda", "float16"), ("cpu", "int8")] if cuda_devices() else [("cpu", "int8")]
    for i, (device, compute) in enumerate(attempts):
        try:
            model = _whisper(model_name, device, compute, models_dir)
            segments, info = model.transcribe(wav, language=language or None, vad_filter=True, word_timestamps=True,
                                              beam_size=5 if device == "cuda" else 1)
            out = []
            for s in segments:
                if should_cancel():
                    raise Cancelled()
                text = s.text.strip()
                if text:
                    out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text,
                                "words": [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
                                          for w in (s.words or [])]})
                on_progress(min(s.end / max(info.duration, 1), 1.0))
            log(f"Speech: {len(out)} sentences ({info.language}) with Whisper {model_name} on {device.upper()}")
            return out, info.language
        except Cancelled:
            raise
        except Exception as ex:  # noqa: BLE001 - e.g. CUDA present but cuDNN missing: retry on the CPU
            if i == len(attempts) - 1:
                raise
            log(f"Whisper on {device.upper()} failed ({type(ex).__name__}: {str(ex)[:150]}) - using the CPU")
    return [], ""


def loudness(wav):
    """Loudness (dB) for each second of the 16 kHz mono wav."""
    import numpy as np
    with wave.open(wav, "rb") as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = len(data) // rate
    if n == 0:
        return []
    rms = np.sqrt(np.mean(data[:n * rate].reshape(n, rate) ** 2, axis=1) + 1e-10)
    return [round(float(v), 1) for v in 20 * np.log10(rms)]


# ---------- 2. scene cuts ----------
def scene_scan(ffmpeg, src, should_cancel):
    """One pass at 5 fps: scene cut times, and how much the picture changes in each second (0-100, mean of
    scdet's frame-to-frame score) - the visual 'action' used to rank highlights."""
    cmd = [ffmpeg, "-hide_banner", "-v", "info", "-nostats", "-i", src, "-an", "-sn",
           "-vf", "fps=5,scale=320:-2,scdet=threshold=12,metadata=mode=print:key=lavfi.scd.score", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=editor.CREATE_NO_WINDOW)
    cuts, per_sec, t = [], {}, 0.0
    try:
        for line in proc.stderr:
            if should_cancel():
                proc.kill()
                raise Cancelled()
            m = re.search(r"lavfi\.scd\.time:\s*([\d.]+)", line)
            if m:
                cuts.append(round(float(m.group(1)), 2))
                continue
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                t = float(m.group(1))
                continue
            m = re.search(r"lavfi\.scd\.score=([\d.]+)", line)
            if m:
                per_sec.setdefault(int(t), []).append(float(m.group(1)))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    n = max(per_sec) + 1 if per_sec else 0
    motion = [round(sum(per_sec[s]) / len(per_sec[s]), 2) if s in per_sec else 0.0 for s in range(n)]
    return cuts, motion


# ---------- 3. choosing the moments ----------
END_OF_THOUGHT = re.compile(r"[.!?。！？…」』)\"]\s*$")
SPONSOR = re.compile(r"\b(sponsor(ed|s)?|brought to you by|use (my |the )?code|promo code|discount code|free trial|"
                     r"link(ed)? (it )?in the description|links? (is )?(down )?below|in the description below|"
                     r"apply to win|giveaway|sign up (today|now|for free))\b", re.I)
OUTRO = re.compile(r"\b(subscribe|hit the bell|notification bell|patreon|merch)\b", re.I)
NAME_STOP = {"The", "This", "That", "These", "Those", "And", "But", "So", "It", "If", "We", "You", "They", "He", "She",
             "What", "When", "Where", "Why", "How", "Now", "Then", "Okay", "Alright", "Right", "Oh", "Yeah", "Well",
             "Here", "There", "Let", "Just", "My", "Our", "Your", "Today", "All", "Before", "After", "With", "Being",
             "Don", "Get", "Time", "Its", "One", "Not", "Also", "Because", "Maybe", "Wow", "Yes", "Hey", "Thank"}


def sponsor_marks(segments):
    """Sentence id -> -1 for sponsor reads and "like and subscribe", without an LLM (English only).
    A sponsor read is found by its call to action ("link in the description", "use code"...), then reaches back
    to where the brand that it keeps naming first comes up - a name that is said there and hardly anywhere else."""
    def names(text):
        return {w for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text) if w not in NAME_STOP}
    everywhere = {}
    for x in segments:
        for w in names(x["text"]):
            everywhere[w] = everywhere.get(w, 0) + 1
    marks = {}
    for h, x in enumerate(segments):
        if OUTRO.search(x["text"]):
            marks[h] = -1
        if not SPONSOR.search(x["text"]):
            continue
        near = [i for i, y in enumerate(segments) if x["start"] - 90 <= y["start"] <= x["end"] + 20]
        here = {}
        for i in near:
            for w in names(segments[i]["text"]):
                here[w] = here.get(w, 0) + 1
        brands = {w for w, n in here.items() if n >= 2 and n >= 0.7 * everywhere[w]}
        first = next((i for i in near if names(segments[i]["text"]) & brands), h)
        last = max([h] + [i for i in near if i > h and names(segments[i]["text"]) & brands])
        while last + 1 < len(segments) and not END_OF_THOUGHT.search(segments[last]["text"]):
            last += 1
        for i in range(min(first, h), last + 1):
            marks[i] = -1
    return marks


def build_units(data, duration, piece=6.0, marks=None, min_talk=8.0):
    """Blocks the series is built from; nothing is ever cut inside one.
    Speech: sentences chained into one thought until a sentence really ends (. ? ! 。) - so a clip never stops
    mid-sentence and the next part never starts with the rest of it - and short thoughts joined up to ~`min_talk` s,
    so a talk is not cut into 2-second scraps. Filler (sponsor reads...) is never joined with the talk around it.
    Between the talking: shots from the scene cuts, long shots split into ~`piece` s."""
    segs, cuts = data["segments"], sorted(data["cuts"])
    marks = marks or {}
    pieces = []  # (sentence id, sentence): a Whisper line can end one sentence and start the next
    for i, s in enumerate(segs):
        words, first = s.get("words") or [], 0
        for k, w in enumerate(words[:-1]):
            if END_OF_THOUGHT.search(w["word"]):
                part = words[first:k + 1]
                pieces.append((i, {"start": s["start"] if not first else part[0]["start"], "end": w["end"],
                                   "text": "".join(x["word"] for x in part).strip()}))
                first = k + 1
        pieces.append((i, s if not first else {"start": words[first]["start"], "end": s["end"],
                                               "text": "".join(x["word"] for x in words[first:]).strip()}))
    chains = []
    for i, s in pieces:
        last = chains[-1] if chains else None
        filler = marks.get(i) == -1
        if last and s["start"] - last["b"] < 1.5 and s["end"] - last["a"] < 45 and last["filler"] == filler \
                and (not END_OF_THOUGHT.search(last["text"]) or last["b"] - last["a"] < min_talk):
            last["b"], last["text"] = s["end"], last["text"] + " " + s["text"]
            if last["ids"][-1] != i:
                last["ids"].append(i)
        else:
            chains.append({"a": s["start"], "b": s["end"], "text": s["text"], "ids": [i], "kind": "speech", "filler": filler})
    units, pos = [], 0.0
    for k, c in enumerate(chains + [None]):
        gap_end = c["a"] if c else duration
        # visual shots in the gap before this thought
        bounds = sorted({pos, *[x for x in cuts if pos < x < gap_end], gap_end})
        shots = []
        for a, b in zip(bounds, bounds[1:]):
            if shots and shots[-1][1] - shots[-1][0] < 2.0:  # flashes under 2 s join the next shot
                shots[-1][1] = b
            else:
                shots.append([a, b])
        for a, b in shots:
            n = max(1, round((b - a) / piece))
            units += [{"a": a + j * (b - a) / n, "b": a + (j + 1) * (b - a) / n, "text": "", "ids": [], "kind": "visual",
                   "filler": False}
                      for j in range(n)]
        if c:
            units.append(dict(c))
            pos = c["b"]
    return [u for u in units if u["b"] - u["a"] >= 1.0 or u["kind"] == "speech"]


def score_units(units, data, duration, llm_marks=None):
    """Content-agnostic score: talk-heavy videos weigh the words, footage weighs motion and sound."""
    import numpy as np
    motion = np.array(data.get("motion") or [0.0])
    loud = np.array(data.get("loudness") or [0.0])

    def z(arr, a, b):
        part = arr[int(a):max(int(a) + 1, int(b))]
        if not len(part):
            return 0.0
        return max(-1.5, min((float(np.mean(part)) - float(np.median(arr))) / (float(np.std(arr)) or 1.0), 2.5))

    talk = sum(u["b"] - u["a"] for u in units if u["kind"] == "speech") / max(duration, 1)
    w_speech = 0.2 + 0.5 * min(talk / 0.6, 1.0)  # 0.2 (footage) .. 0.7 (podcast, tutorial)
    w_motion, w_loud = (1 - w_speech) * 0.55, (1 - w_speech) * 0.45
    marks = llm_marks or {}
    for u in units:
        d = u["b"] - u["a"]
        words = 0.0
        if u["kind"] == "speech":
            words = 0.5 + 0.5 * min(len(u["text"]) / d / 12, 1.0) + (0.2 if re.search(r"[?!？！]", u["text"]) else 0)
        u["score"] = w_speech * words + w_motion * z(motion, u["a"], u["b"]) + w_loud * z(loud, u["a"], u["b"])
        u["score"] += 0.8 * sum(marks.get(i, 0) for i in u["ids"]) / max(1, len(u["ids"]))
    return talk


def _break_score(prev, nxt, cuts):
    """How natural a cut between two kept blocks is: a pause, a scene change, a finished thought."""
    s = min(nxt["a"] - prev["b"], 3.0) / 3.0
    s += 0.6 if any(abs(c - nxt["a"]) < 0.6 for c in cuts) else 0.0
    if prev["kind"] == "speech":
        s += 0.6 if END_OF_THOUGHT.search(prev["text"]) else -1.0
    return s


def _parts(units, data, duration):
    """Kept blocks -> [[start, end], ...] to play one after another."""
    parts = []
    for u in units:  # blocks that follow each other play as one continuous part
        if parts and u["a"] - parts[-1][1] < 0.05:
            parts[-1][1] = u["b"]
        else:
            parts.append([u["a"], u["b"]])
    for j, (a, b) in enumerate(parts):  # a blink-long piece ("Enjoy.") gets some room on both sides
        if b - a < 1.5:
            room = (1.5 - (b - a)) / 2
            lo = max(parts[j - 1][1] if j else 0.0, max((s["end"] for s in data["segments"] if s["end"] <= a), default=0.0))
            hi = min(parts[j + 1][0] if j + 1 < len(parts) else duration,
                     min((s["start"] for s in data["segments"] if s["start"] >= b), default=duration))
            parts[j] = [max(lo, a - room), min(hi, b + room)]
    return [[round(a, 2), round(b, 2)] for a, b in parts]


def plan_series(data, duration, count, target, llm_marks=None):
    """Part 1..N in story order: the best blocks spread over the whole video (about count x target seconds),
    then split where the story pauses. Returns [{parts, start, end, length, score, text, ...}]."""
    units = build_units(data, duration, marks=llm_marks)
    if not units:
        return []
    score_units(units, data, duration, llm_marks)
    budget = count * target
    keep = set()
    if sum(u["b"] - u["a"] for u in units if not u["filler"]) <= budget * 1.05:
        keep = {i for i, u in enumerate(units) if not u["filler"]}
    else:
        bins = max(count * 3, round(budget / 8))  # one strong block from each stretch first: the series covers it all
        for j in range(bins):
            lo, hi = duration * j / bins, duration * (j + 1) / bins
            best = max((i for i, u in enumerate(units) if lo <= u["a"] < hi and u["b"] - u["a"] <= target * 0.6
                        and not u["filler"]),
                       key=lambda i: units[i]["score"], default=None)
            if best is not None and units[best]["score"] > -0.3:
                keep.add(best)
        total = sum(units[i]["b"] - units[i]["a"] for i in keep)
        for i in sorted(range(len(units)), key=lambda i: -units[i]["score"]):
            if total >= budget:
                break
            if i not in keep and units[i]["b"] - units[i]["a"] <= target * 0.6 and not units[i]["filler"]:
                keep.add(i)
                total += units[i]["b"] - units[i]["a"]
        # fill short holes between kept blocks: smoother story, fewer jumps
        for i in range(1, len(units) - 1):
            if i not in keep and i - 1 in keep and i + 1 in keep and units[i]["b"] - units[i]["a"] < 3 and not units[i]["filler"] \
                    and total < budget * 1.05:
                keep.add(i)
                total += units[i]["b"] - units[i]["a"]
        while total > budget + count * 4 and len(keep) > count:
            weakest = min(keep, key=lambda i: units[i]["score"])
            keep.discard(weakest)
            total -= units[weakest]["b"] - units[weakest]["a"]
    kept = [units[i] for i in sorted(keep)]
    # split into parts at the most natural break near each ideal boundary
    lengths = [u["b"] - u["a"] for u in kept]
    total = sum(lengths)
    cum = [0.0]
    for x in lengths:
        cum.append(cum[-1] + x)
    splits, last = [], 0
    for k in range(1, count):
        ideal, window = total * k / count, max(12.0, target * 0.2)
        options = [i for i in range(last + 1, len(kept)) if abs(cum[i] - ideal) <= window]
        if not options:
            options = [min(range(last + 1, len(kept)), key=lambda i: abs(cum[i] - ideal), default=None)]
            if options[0] is None:
                break
        best = max(options, key=lambda i: _break_score(kept[i - 1], kept[i], data["cuts"])
                   - abs(cum[i] - ideal) / window * 0.5)
        splits.append(best)
        last = best
    groups = [kept[a:b] for a, b in zip([0, *splits], [*splits, len(kept)]) if b > a]
    series = []
    for k, g in enumerate(groups):
        parts = _parts(g, data, duration)
        series.append({"parts": parts, "start": parts[0][0], "end": parts[-1][1],
                       "length": round(sum(b - a for a, b in parts), 1),
                       "score": round(sum(u["score"] for u in g) / len(g), 2),
                       "text": " ".join(u["text"] for u in g if u["text"]),
                       "part": k + 1, "of": len(groups), "title": "",
                       "reason": f"Part {k + 1}/{len(groups)}: {_fmt(parts[0][0])}–{_fmt(parts[-1][1])}, {len(parts)} cut(s)"})
    return series


def plan_moments(data, duration, count, target, llm_marks=None):
    """Highlight reels: each short is about `target` s of the best moments from the whole video, in time order.
    The video is split into equal stretches and every stretch gives each short one of its best blocks, so a
    short shows every stage of the video (arriving, making the fire, cooking, eating...) rather than only the
    loudest or busiest one, and ends on the result. Shorts never share a block. Returns [{parts, start, end, length, score, text, ...}]."""
    units = build_units(data, duration, piece=5.0, marks=llm_marks)
    if not units:
        return []
    talk = score_units(units, data, duration, llm_marks)
    longest = max(8.0, target * 0.35)  # a long talk would eat the whole reel
    ok = [i for i, u in enumerate(units) if u["b"] - u["a"] <= longest and not u["filler"]]
    owner = {}  # unit index -> short
    # footage usually ends on the result (the dish served, the clean car): a calm shot that never scores high,
    # so every reel ends on the start of the video's last scene. Talks end on "subscribe" instead - not there.
    finale = set()
    if talk < 0.4:
        last_scene = max((c for c in data["cuts"] if duration * 0.85 <= c < duration - 4), default=duration - 20)
        ending = [i for i in ok if units[i]["a"] >= last_scene - 0.05 and units[i]["b"] - units[i]["a"] >= 2]
        k, held = 0, 0.0
        for i in ending:  # about 8 s of it for each reel
            if k >= count:
                break
            owner[i] = k
            finale.add(i)
            held += units[i]["b"] - units[i]["a"]
            if held >= 7:
                k, held = k + 1, 0.0
    stretches = max(3, round(target / 7))  # about one 5-8 s moment per stretch
    for j in range(stretches):
        lo, hi = duration * j / stretches, duration * (j + 1) / stretches
        ranked = sorted((i for i in ok if lo <= units[i]["a"] < hi and i not in finale), key=lambda i: -units[i]["score"])
        for r, i in enumerate(ranked[:count]):
            owner[i] = (r + j) % count  # rotate who gets the best one, so every short is as good as the others
    reels = []
    for k in range(count):
        keep = {i for i, o in owner.items() if o == k}
        total = sum(units[i]["b"] - units[i]["a"] for i in keep)
        while total < target - 3:  # grow the strongest moments into the blocks next to them
            free = [i for i in ok if i not in owner and (i - 1 in keep or i + 1 in keep)]
            if not free:
                free = [i for i in ok if i not in owner]
            if not free:
                break
            i = max(free, key=lambda i: units[i]["score"])
            keep.add(i)
            owner[i] = k
            total += units[i]["b"] - units[i]["a"]
        while total > target + 4 and len(keep - finale) > 1:
            weakest = min(keep - finale, key=lambda i: units[i]["score"])
            keep.discard(weakest)
            del owner[weakest]
            total -= units[weakest]["b"] - units[weakest]["a"]
        if not keep:
            continue
        g = [units[i] for i in sorted(keep)]
        parts = _parts(g, data, duration)
        reels.append({"parts": parts, "start": parts[0][0], "end": parts[-1][1],
                      "length": round(sum(b - a for a, b in parts), 1),
                      "score": round(sum(u["score"] for u in g) / len(g), 2),
                      "text": " ".join(u["text"] for u in g if u["text"]), "title": "",
                      "reason": f"{len(parts)} moments from {_fmt(parts[0][0])}–{_fmt(parts[-1][1])}"})
    return reels


SERIES_PROMPT = """A long video is being cut into a short-video series (Part 1, Part 2, ...) that keeps only what matters.
Below is its transcript, one numbered sentence per line.
List the sentences that carry the story or the useful information ("important"), and the filler that can go:
greetings, sponsor reads, "like and subscribe", repetition, off-topic chatter ("filler").
Leave ordinary sentences out of both lists.
Answer with JSON only: {{"important": [ids], "filler": [ids]}}

Transcript:
{lines}"""

TITLE_PROMPT = """Write a short catchy caption for part {k} of {n} of a short-video series cut from one long video.
Use the language of the text below, at most 70 characters, no hashtags, no "Part {k}".
What this part says:
{text}
Answer with JSON only: {{"title": "<caption>"}}"""


def llm_marks(segments, model, should_cancel, log, chunk_chars=24000):
    """Sentence id -> +1 important / -1 filler, from the local LLM."""
    lines = [f"[{i}] {s['text']}" for i, s in enumerate(segments)]
    chunks, cur = [], []
    for ln in lines:
        if cur and sum(len(x) + 1 for x in cur) + len(ln) > chunk_chars:
            chunks.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        chunks.append(cur)
    marks = {}
    for k, chunk in enumerate(chunks):
        if should_cancel():
            raise Cancelled()
        log(f"Asking {model} which sentences matter" + (f" (part {k + 1}/{len(chunks)})" if len(chunks) > 1 else ""))
        try:
            ans = json.loads(_ollama_chat(model, SERIES_PROMPT.format(lines="\n".join(chunk))))
        except ValueError:
            continue
        for key, val in (("important", 1), ("filler", -1)):
            for i in ans.get(key) or []:
                try:
                    if 0 <= int(i) < len(segments):
                        marks[int(i)] = val
                except (TypeError, ValueError):
                    pass
    log(f"The LLM marked {sum(v > 0 for v in marks.values())} important and {sum(v < 0 for v in marks.values())} filler sentences")
    return marks


def llm_title(model, text, k, n):
    try:
        return str(json.loads(_ollama_chat(model, TITLE_PROMPT.format(k=k, n=n, text=text[:3000]), timeout=300))
                   .get("title") or "").strip()[:90]
    except (OSError, ValueError, AttributeError):
        return ""


def series(data, info, opts, should_cancel, log):
    """(parts of the series, or highlight reels for the "moments" style, method) - LLM marks and titles when Ollama is ready, heuristics otherwise."""
    model = None
    if opts.get("useLlm") and len(data["segments"]) >= 3:
        installed = ollama_models()
        want = opts.get("llmModel") or DEFAULT_LLM
        if installed is None:
            log("Ollama is not running - choosing the blocks with heuristics")
        elif not any(m == want or m.split(":")[0] == want for m in installed):
            log(f"Ollama has no model “{want}” - run “ollama pull {want}”. Choosing the blocks with heuristics")
        else:
            model = want
    marks = sponsor_marks(data["segments"])
    if marks:
        log(f"Left out {len(marks)} sentence(s) of sponsor reads and “subscribe” asks")
    if model:
        try:
            marks = {**llm_marks(data["segments"], model, should_cancel, log), **marks}
        except Cancelled:
            raise
        except Exception as ex:  # noqa: BLE001 - heuristics still work
            log(f"LLM failed ({type(ex).__name__}: {str(ex)[:150]}) - using heuristics")
            model = None
    planner = plan_moments if opts.get("style") == "moments" else plan_series
    plan = planner(data, info["duration"], int(opts["count"]), float(opts["target"]), marks)
    for k, p in enumerate(plan):
        if model and p["text"]:
            p["title"] = llm_title(model, p["text"], p.get("part", k + 1), p.get("of", len(plan)))
        if not p["title"] and p["text"]:  # the first whole sentence of the part that says something
            # CJK text has no space after its full stops, and Whisper often joins whole paragraphs with commas
            commas = r"|(?<=[，、,])(?=.{30})" if CJK.search(p["text"]) else ""
            sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|(?<=[。！？])" + commas, p["text"].strip()) if x.strip()]
            title = next((x for x in sentences if len(x) >= (8 if CJK.search(x) else 20)), sentences[0])
            limit = 40 if CJK.search(title) else 80
            p["title"] = title if len(title) <= limit else title[:limit - 1].rstrip("，、, ") + "…"
    return plan, "llm" if model else "heuristic"


def _fmt(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _ollama_chat(model, prompt, timeout=900):
    body = json.dumps({"model": model, "stream": False, "format": "json",
                       "options": {"temperature": 0.3, "num_ctx": 16384},
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["message"]["content"]


# ---------- 4. reframing ----------
def detect_bars(src, duration, samples=12, limit=24):
    """Letterbox/pillarbox bars baked into the picture: (w, h, x, y) of the real image, or None.
    A row/column is a bar only if it stays dark in every sampled frame, so one dark scene does not count.
    (OpenCV instead of ffmpeg's cropdetect, which the bundled LGPL ffmpeg does not have.)"""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(src)
    peak = None
    try:
        for k in range(samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, duration * (k + 0.5) / samples * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            peak = gray if peak is None else np.maximum(peak, gray)
    finally:
        cap.release()
    if peak is None:
        return None
    rows = np.where(peak.max(axis=1) > limit)[0]
    cols = np.where(peak.max(axis=0) > limit)[0]
    if not len(rows) or not len(cols):
        return None
    y, x = int(rows[0]), int(cols[0])
    h, w = editor._even(rows[-1] + 1 - y), editor._even(cols[-1] + 1 - x)
    return (min(w, peak.shape[1] - x), min(h, peak.shape[0] - y), x, y)


def ensure_yunet(models_dir):
    path = os.path.join(models_dir, YUNET_FILE)
    if not os.path.isfile(path):
        os.makedirs(models_dir, exist_ok=True)
        tmp = path + ".part"
        urllib.request.urlretrieve(YUNET_URL, tmp)
        os.replace(tmp, path)
    return path


def track_faces(src, start, end, yunet_path, should_cancel, bars=None):
    """[(t relative to start, face centre x in 0..1 or None)] every TRACK_STEP seconds."""
    import cv2
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    det, size, out = None, None, []
    every = max(1, round(fps * TRACK_STEP))
    n, prev = 0, None
    try:
        while True:
            if not cap.grab():
                break
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if t > end:
                break
            n += 1
            if (n - 1) % every:
                continue
            if should_cancel():
                raise Cancelled()
            ok, frame = cap.retrieve()
            if not ok:
                continue
            if bars:
                bw, bh, bx, by = bars
                frame = frame[by:by + bh, bx:bx + bw]
            h, w = frame.shape[:2]
            k = 640 / w
            small = cv2.resize(frame, (640, max(2, int(h * k))))
            if det is None or size != small.shape[:2]:
                size = small.shape[:2]
                det = cv2.FaceDetectorYN.create(yunet_path, "", (small.shape[1], small.shape[0]), 0.8)
            _, faces = det.detect(small)
            x = None
            if faces is not None and len(faces):
                def weight(f):  # big faces first, and stay on the same person when two are alike
                    cx = (f[0] + f[2] / 2) / small.shape[1]
                    return f[2] * f[3] * f[14] * (1.0 if prev is None else 1.5 - min(abs(cx - prev), 0.5))
                f = max(faces, key=weight)
                x = prev = float((f[0] + f[2] / 2) / small.shape[1])
            out.append((max(0.0, t - start), x))
    finally:
        cap.release()
    return out


def camera_path(samples, cuts, crop_frac, deadzone=0.07, follow=0.25):
    """Face samples -> smooth crop centre keyframes [(t, x)]: hold still while the face stays in the dead zone,
    glide when it leaves, and jump at scene cuts."""
    lo, hi = crop_frac / 2, 1 - crop_frac / 2
    # a face in only a few frames is a false detection (a wheel, a sponge...): keep the centre crop
    if not samples or sum(x is not None for _, x in samples) < max(3, 0.25 * len(samples)):
        return [(0.0, 0.5)]
    filled, last = [], next(x for _, x in samples if x is not None)
    for t, x in samples:  # hold the last known face through missed detections
        last = x if x is not None else last
        filled.append((t, last))
    # median of 3 removes single-frame detection jumps
    xs = [x for _, x in filled]
    med = [sorted(xs[max(0, i - 1):i + 2])[len(xs[max(0, i - 1):i + 2]) // 2] for i in range(len(xs))]
    keys, cam, moving = [], None, False
    cut_iter = sorted(cuts)
    for (t, _), target in zip(filled, med):
        target = min(max(target, lo), hi)
        cut = next((c for c in cut_iter if keys and keys[-1][0] < c <= t), None)
        if cam is None or cut is not None:
            if cut is not None and keys:
                keys.append((max(cut - 0.01, keys[-1][0]), cam))  # hold until the cut, then jump
            cam = target
            keys.append((t if cut is None else cut, cam))
            continue
        # start gliding when the face leaves the dead zone, keep going until it is centred again
        moving = abs(target - cam) > (0.01 if moving else deadzone)
        if moving:
            cam += (target - cam) * follow
        keys.append((t, cam))
    return _simplify(keys, 0.004)


def _simplify(points, eps):
    """Ramer-Douglas-Peucker on (t, x) keyframes: keeps the ffmpeg expression short."""
    if len(points) < 3:
        return points
    (t0, x0), (t1, x1) = points[0], points[-1]
    worst, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        t, x = points[i]
        expect = x0 + (x1 - x0) * ((t - t0) / (t1 - t0) if t1 > t0 else 0)
        if abs(x - expect) > worst:
            worst, idx = abs(x - expect), i
    if worst <= eps:
        return [points[0], points[-1]]
    return _simplify(points[:idx + 1], eps)[:-1] + _simplify(points[idx:], eps)


def _x_expr(keys, crop_w, width):
    """Piecewise-linear crop x(t) as a balanced if() tree (depth log2(n))."""
    px = [(t, min(max(x * width - crop_w / 2, 0), width - crop_w)) for t, x in keys]
    if len(px) == 1:
        return f"{px[0][1]:.1f}"

    def seg(k):
        (ta, xa), (tb, xb) = px[k], px[k + 1]
        if tb - ta < 1e-3 or abs(xb - xa) < 0.5:
            return f"{xb:.1f}"
        return f"({xa:.1f}+({xb - xa:.1f})*(t-{ta:.3f})/{tb - ta:.3f})"

    def tree(a, b):  # segments a..b-1
        if b - a == 1:
            return seg(a)
        m = (a + b) // 2
        return f"if(lt(t,{px[m][0]:.3f}),{tree(a, m)},{tree(m, b)})"

    body = tree(0, len(px) - 1)
    return f"if(lt(t,{px[0][0]:.3f}),{px[0][1]:.1f},if(gte(t,{px[-1][0]:.3f}),{px[-1][1]:.1f},{body}))"


# ---------- 5. captions ----------
def _ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


SUB_POSITIONS = {"bottom": (2, 420), "middle": (5, 0), "top": (8, 300)}  # ASS alignment, vertical margin


def write_ass(path, segments, parts, language, position="bottom", label=""):
    """Short TikTok-style captions (a few words at a time) from the word timestamps; each word turns yellow as
    it is spoken (ASS karaoke: PrimaryColour = spoken, SecondaryColour = not yet).
    `parts`: [(start, end)] of the source played one after another - caption times follow the output timeline."""
    cjk = language in SUB_FONTS
    limit = 12 if cjk else 24
    events = []  # each: ([(word start, word end, text)] in output time, end of its part in output time)
    offset = 0.0
    for start, end in parts:
        part_end = offset + end - start

        def out(t, start=start, offset=offset):
            return t - start + offset

        for s in segments:
            if s["end"] <= start or s["start"] >= end:
                continue
            words = s["words"] or [{"start": s["start"], "end": s["end"], "word": s["text"]}]
            cur = []
            for w in words:
                if w["end"] <= start or w["start"] >= end:
                    continue
                piece = w["word"].strip() if cjk else w["word"]
                text = "".join(p for _, _, p in cur)
                if cur and len((text + piece).strip()) > limit:
                    events.append((cur, part_end))
                    cur = []
                cur.append((out(max(w["start"], start)), out(min(w["end"], end)), piece))
                if re.search(r"[.!?。！？、,，]$", piece.strip()) and len("".join(p for _, _, p in cur).strip()) >= limit // 2:
                    events.append((cur, part_end))
                    cur = []
            if cur:
                events.append((cur, part_end))
        offset = part_end
    font = SUB_FONTS.get(language, "Arial")
    align, margin = SUB_POSITIONS.get(position, SUB_POSITIONS["bottom"])
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}", "WrapStyle: 0", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
             "MarginR, MarginV, Encoding",
             f"Style: Default,{font},76,&H0000E5FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,{align},80,80,"
             f"{margin},1",
             # "Part 2/3" badge: white on a dark box, top centre, for the first seconds
             f"Style: Label,{font},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,"
             "190,1", "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if label:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(3.5)},Label,,0,0,0,,{{\\fad(150,300)}}{label}")
    for k, (ev, part_end) in enumerate(events):
        a, b = ev[0][0], ev[-1][1]
        nxt = events[k + 1][0][0][0] if k + 1 < len(events) else b
        b = min(max(b, min(nxt, b + 0.4)), part_end)  # keep a caption up through short pauses, never past its shot
        texts = []
        for i, (ws, we, piece) in enumerate(ev):
            nxt_word = ev[i + 1][0] if i + 1 < len(ev) else we
            cs = max(1, round((max(nxt_word, ws) - ws) * 100))
            piece = piece.lstrip() if i == 0 else piece
            texts.append(f"{{\\k{cs}}}" + piece.replace("{", "(").replace("}", ")").replace("\n", " "))
        lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,{''.join(texts).strip()}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(events) + bool(label)


# ---------- 6. render ----------
def _video_chain(i, W, H, keys, mode, bars, fps):
    """Filters turning input i into a 1080x1920 picture labelled [v{i}]."""
    pre = f"[{i}:v]" + (f"crop={bars[0]}:{bars[1]}:{bars[2]}:{bars[3]}," if bars else "")
    tail = f"setsar=1,fps={fps},setpts=PTS-STARTPTS"
    if mode == "fit":  # the whole frame over a blurred, zoomed copy of itself
        return (f"{pre}split[b{i}][f{i}];[b{i}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},gblur=sigma=40[bg{i}];[f{i}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,{tail}[v{i}]")
    if W / H > OUT_W / OUT_H:  # wider than 9:16: move a full-height window
        crop_w = editor._even(H * OUT_W / OUT_H)
        crop = f"crop=w={crop_w}:h={H}:x='{_x_expr(keys, crop_w, W)}':y=0"
    else:
        crop = f"crop=w={W}:h={min(editor._even(W * OUT_H / OUT_W), H)}:x=0:y=(ih-oh)/2"
    return f"{pre}{crop},scale={OUT_W}:{OUT_H}:flags=lanczos,{tail}[v{i}]"


def render(ffmpeg, src, dst, info, parts, keys_list, subs_file, on_progress, should_cancel, mode="face", bars=None):
    """`parts`: [(start, end)] of the source, joined in order (one part = one continuous clip);
    `keys_list`: the crop path of each part (face mode)."""
    W, H = (bars[0], bars[1]) if bars else (info["width"], info["height"])
    src, dst = os.path.abspath(src), os.path.abspath(dst)  # ffmpeg runs in the temp folder
    workdir = tempfile.mkdtemp(prefix="grabber-smartcut-")
    try:
        fps = round(min(info.get("fps") or 30, 60), 3)
        label, enc_args, pix = editor._encoder_args(ffmpeg, OUT_W, OUT_H, fps)
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
        graph, fade = [], 0.03
        for i, (start, end) in enumerate(parts):
            d = end - start
            cmd += ["-ss", f"{start:.3f}", "-t", f"{d:.3f}", "-i", src]
            graph.append(_video_chain(i, W, H, keys_list[i] if i < len(keys_list) else [(0.0, 0.5)], mode, bars, fps)
                         .replace(f"[v{i}]", f",trim=duration={d:.3f}[v{i}]"))
            audio = f"[{i}:a:0]aresample=48000," if info.get("hasAudio") else f"anullsrc=r=48000:cl=stereo,"
            graph.append(f"{audio}aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={d:.3f},"
                         f"asetpts=PTS-STARTPTS" + (f",afade=t=in:d={fade},afade=t=out:st={d - fade:.3f}:d={fade}"
                                                    if len(parts) > 1 else "") + f"[a{i}]")
        n = len(parts)
        graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
        if subs_file:
            shutil.copy(subs_file, os.path.join(workdir, "subs.ass"))
        graph.append(f"[vc]{'ass=subs.ass,' if subs_file else ''}format={pix}[v]")
        with open(os.path.join(workdir, "graph.txt"), "w", encoding="utf-8") as f:
            f.write(";\n".join(graph))
        total = sum(e - s for s, e in parts)
        cmd += ["-/filter_complex", "graph.txt", "-map", "[v]", "-map", "[ac]", *enc_args,
                "-c:a", "aac", "-b:a", "192k", *editor.STRIP_ARGS, "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", dst]
        editor._run_ffmpeg(cmd, workdir, total, dst, on_progress, should_cancel)
        return label
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- the whole run ----------
def analyze(ffmpeg, src, info, opts, data_dir, progress, should_cancel, log):
    """Transcript (cached per file + Whisper model), loudness and scene cuts."""
    cache_dir = os.path.join(data_dir, "smartcut_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, _video_key(src, opts["whisper"], opts.get("language") or "auto") + ".json")
    if os.path.isfile(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                data = json.load(f)
            log("Using the transcript and scene cuts from the last run on this video")
            if "motion" not in data:  # cache from before motion was measured
                progress("Measuring the action in each scene…", 0.55)
                data["cuts"], data["motion"] = scene_scan(ffmpeg, src, should_cancel)
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            return data
        except (OSError, ValueError):
            pass
    cuts_box, motion_box, cut_err = [], [], []

    def cuts_worker():
        try:
            cuts, motion = scene_scan(ffmpeg, src, should_cancel)
            cuts_box.extend(cuts)
            motion_box.extend(motion)
        except Exception as ex:  # noqa: BLE001 - reported below; cuts are optional
            cut_err.append(ex)

    worker = threading.Thread(target=cuts_worker, daemon=True)
    worker.start()
    tmp = tempfile.mkdtemp(prefix="grabber-smartcut-")
    try:
        wav = os.path.join(tmp, "audio.wav")
        if info.get("hasAudio"):
            progress("Reading the audio…", 0.01)
            extract_audio(ffmpeg, src, wav)
            loud = loudness(wav)
        if info.get("hasAudio") and opts["whisper"] == "off":
            segments, lang = [], ""
            log("Speech model is off: picking moments from the scenes and the sound only, without captions")
        elif info.get("hasAudio"):
            progress(f"Loading Whisper {opts['whisper']} (downloaded on first use)…", 0.03)
            segments, lang = transcribe(wav, opts["whisper"], os.path.join(data_dir, "models"), opts.get("language"),
                                        lambda f: progress(f"Transcribing… {round(f * 100)}%", 0.03 + 0.5 * f),
                                        should_cancel, log)
        else:
            loud, segments, lang = [], [], ""
            log("This video has no audio: picking moments from the scene cuts only")
        progress("Detecting scene changes…", 0.55)
        while worker.is_alive():
            worker.join(0.3)
            if should_cancel():
                raise Cancelled()
        if cut_err and not isinstance(cut_err[0], Cancelled):
            log(f"Scene detection failed: {cut_err[0]}")
        data = {"segments": segments, "language": lang, "loudness": loud, "cuts": cuts_box, "motion": motion_box}
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
