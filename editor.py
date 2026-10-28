"""Video editing on top of ffmpeg: crop / zoom / rotate / flip / trim / color / speed / audio, export, metadata removal.

Edit spec (all coordinates in the *rotated* frame, i.e. after rotation is applied):
    {
      "rotate": 0 | 90 | 180 | 270,          # clockwise
      "flipH": bool, "flipV": bool,
      "crop": {"x", "y", "w", "h"},          # pixels; the output frame
      "zoom": float,                         # 1 = none, >1 zoom in, <1 zoom out (video shrinks inside the frame)
      "panX": 0..1, "panY": 0..1,            # zoom in: focus point; zoom out: position of the video
      "background": "black" | "blur" | "#rrggbb",   # fill for zoom out
      "zoomAnimate": bool, "zoomEnd": float, "zoomEase": bool,   # zoom from `zoom` to `zoomEnd` over the clip
      "start": seconds, "end": seconds,      # trim
      "color": {brightness, contrast, saturation, temperature, tint, gamma, fade, vignette, sharpen},  # -100..100
      "speed": 0.25..4, "volume": 0..2, "mute": bool, "fadeIn": s, "fadeOut": s,   # mute = original audio only
      "music": {"path", "volume": 0..2, "offset": s, "loop": bool},   # background music, cut to the output length
      "text": {"content", "font": file, "size": % of height, "color", "opacity", "outline", "outlineColor",
               "box": bool, "boxColor", "boxOpacity", "x": 0..1, "y": 0..1},
      "logo": {"path", "size": % of width, "opacity", "x": 0..1, "y": 0..1},
      "stripMetadata": bool,
    }
"""
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".mkv": "video/x-matroska",
               ".webm": "video/webm", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac",
               ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection"}
STRIP_ARGS = ["-map_metadata", "-1", "-map_chapters", "-1",
              "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
              "-metadata:s:v", "encoder=", "-metadata:s:a", "encoder="]  # bitexact still leaves "Lavc <codec>"

# tried in order; the first one that can encode a test clip wins
ENCODERS = [
    ("h264_nvenc", "NVIDIA NVENC", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"], "yuv420p"),
    ("h264_qsv", "Intel Quick Sync", ["-c:v", "h264_qsv", "-global_quality", "22"], "nv12"),
    ("h264_amf", "AMD AMF", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"], "yuv420p"),
    ("h264_mf", "Windows Media Foundation", ["-c:v", "h264_mf"], "yuv420p"),
    ("libopenh264", "OpenH264 (software)", ["-c:v", "libopenh264"], "yuv420p"),
]
BITRATE_ENCODERS = {"h264_mf", "libopenh264"}  # no quality mode: give them a bitrate


class Cancelled(Exception):
    pass


def find_tool(name, bundled_dir):
    path = os.path.join(bundled_dir, name + ".exe")
    return path if os.path.isfile(path) else (shutil.which(name) or name)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW, **kw)


def probe(ffprobe, path):
    r = _run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    if r.returncode != 0:
        raise RuntimeError(f"Cannot read video: {r.stderr.strip()[-200:]}")
    data = json.loads(r.stdout)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("This file has no video stream")
    audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    num, _, den = (video.get("avg_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(num or 0) else 30.0
    tags = dict(data["format"].get("tags") or {})
    for s in data["streams"]:
        for k, v in (s.get("tags") or {}).items():
            tags[f"{s.get('codec_type')}:{k}"] = v
    return {
        "width": int(video["width"]), "height": int(video["height"]),
        "duration": float(data["format"].get("duration") or video.get("duration") or 0),
        "fps": round(fps, 3), "vcodec": video.get("codec_name"), "acodec": audio.get("codec_name") if audio else None,
        "hasAudio": bool(audio), "size": int(data["format"].get("size") or 0), "tags": tags,
    }


def probe_audio(ffprobe, path):
    r = _run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-select_streams", "a:0", "-of", "json", path])
    if r.returncode != 0:
        raise RuntimeError(f"Cannot read audio: {r.stderr.strip()[-200:]}")
    data = json.loads(r.stdout)
    if not data.get("streams"):
        raise RuntimeError("This file has no audio stream")
    return {"duration": float(data["format"].get("duration") or 0), "acodec": data["streams"][0].get("codec_name")}


_encoder_cache = {}


def detect_encoder(ffmpeg):
    if ffmpeg in _encoder_cache:
        return _encoder_cache[ffmpeg]
    for name, label, args, pix in ENCODERS:
        cmd = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=0.3",
               "-vf", f"format={pix}", *args, *(["-b:v", "1M"] if name in BITRATE_ENCODERS else []), "-f", "null", "-"]
        try:
            if _run(cmd, timeout=30).returncode == 0:
                _encoder_cache[ffmpeg] = (name, label, args, pix)
                return _encoder_cache[ffmpeg]
        except subprocess.TimeoutExpired:
            continue
    raise RuntimeError("No working H.264 encoder found in ffmpeg")


def _even(v):
    return max(2, int(round(v / 2)) * 2)


MAX_LONG, MAX_SHORT = 3840, 2160  # H.264 encoders (NVENC/QSV/AMF/openh264) stop around 4K


def _encodable(w, h):
    """Even output size, scaled down (aspect kept) to fit 4K so sources like 4320x5760 still encode."""
    k = min(1.0, MAX_LONG / max(w, h), MAX_SHORT / min(w, h))
    return _even(w * k), _even(h * k)


def rotated_size(info, rotate):
    w, h = info["width"], info["height"]
    return (h, w) if rotate in (90, 270) else (w, h)


def _zoom_expr(z0, z1, length, ease):
    """ffmpeg expression for the zoom factor at time t (source time inside the trim)."""
    p = f"clip(t/{length:.4f},0,1)"
    if ease:
        p = f"({p}*{p}*(3-2*{p}))"
    return f"({z0:.5f}+({z1 - z0:.5f})*{p})"


def build_geometry(info, spec, length):
    """Rotate/flip -> crop -> (animated) zoom. Returns (filter_complex, "[vgeo]", (out_w, out_h))."""
    rot = int(spec.get("rotate") or 0) % 360
    rw, rh = rotated_size(info, rot)
    pre = {0: [], 90: ["transpose=1"], 180: ["transpose=1", "transpose=1"], 270: ["transpose=2"]}[rot]
    if spec.get("flipH"):
        pre.append("hflip")
    if spec.get("flipV"):
        pre.append("vflip")

    c = spec.get("crop") or {"x": 0, "y": 0, "w": rw, "h": rh}
    cw, ch = min(int(c["w"]), rw), min(int(c["h"]), rh)
    cx, cy = max(0, min(int(c["x"]), rw - cw)), max(0, min(int(c["y"]), rh - ch))
    ow, oh = _encodable(cw, ch)
    z0 = float(spec.get("zoom") or 1)
    z1 = float(spec.get("zoomEnd") or z0) if spec.get("zoomAnimate") else z0
    px, py = float(spec.get("panX", 0.5)), float(spec.get("panY", 0.5))
    head = ",".join(pre) + "," if pre else ""
    base = f"[0:v]{head}crop={cw}:{ch}:{cx}:{cy}"
    bg = spec.get("background") or "black"
    color = bg if re.fullmatch(r"#[0-9a-fA-F]{6}", bg) else "#000000"
    animated = abs(z1 - z0) > 1e-3

    if animated and min(z0, z1) < 1 < max(z0, z1):
        raise ValueError("Animated zoom must stay on one side of 100% (both above or both below).")

    if not animated:
        if z0 >= 1:
            # zoom in = crop a smaller window around the focus point, then scale back to the frame size
            sw, sh = cw / z0, ch / z0
            sx = cx + min(max(px * cw - sw / 2, 0), cw - sw)
            sy = cy + min(max(py * ch - sh / 2, 0), ch - sh)
            chain = f"[0:v]{head}crop={int(sw)}:{int(sh)}:{int(sx)}:{int(sy)},scale={ow}:{oh}:flags=lanczos,setsar=1[vgeo]"
            return chain, "[vgeo]", (ow, oh)
        # zoom out = the cropped frame shrinks inside the output frame, background fills the rest
        fw, fh = _even(ow * z0), _even(oh * z0)
        fx, fy = int((ow - fw) * px), int((oh - fh) * py)
        if bg == "blur":
            chain = (f"{base},split[a][b];[a]scale={ow}:{oh},gblur=sigma={max(10, ow // 25)}[bg];"
                     f"[b]scale={fw}:{fh}:flags=lanczos[fg];[bg][fg]overlay={fx}:{fy},setsar=1[vgeo]")
        else:
            chain = f"{base},scale={fw}:{fh}:flags=lanczos,pad={ow}:{oh}:{fx}:{fy}:color={color.replace('#', '0x')},setsar=1[vgeo]"
        return chain, "[vgeo]", (ow, oh)

    z = _zoom_expr(z0, z1, length, spec.get("zoomEase", True))
    if min(z0, z1) >= 1:
        # animated zoom in: scale the frame up per frame, keep an output-sized window around the focus point
        chain = (f"{base},scale=w='trunc({ow}*{z}/2)*2':h='trunc({oh}*{z}/2)*2':eval=frame:flags=bicubic,"
                 f"crop={ow}:{oh}:x='clip({px:.4f}*iw-{ow}/2,0,iw-{ow})':y='clip({py:.4f}*ih-{oh}/2,0,ih-{oh})',setsar=1[vgeo]")
        return chain, "[vgeo]", (ow, oh)

    # animated zoom out: background fixed, foreground scaled per frame and positioned by pan
    if bg == "blur":
        bg_chain = f"[a]scale={ow}:{oh},gblur=sigma={max(10, ow // 25)}[bg]"
    else:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        bg_chain = f"[a]scale={ow}:{oh},format=rgb24,lutrgb=r={r}:g={g}:b={b}[bg]"
    chain = (f"{base},split[a][b];{bg_chain};"
             f"[b]scale=w='trunc({ow}*{z}/2)*2':h='trunc({oh}*{z}/2)*2':eval=frame:flags=bicubic[fg];"
             f"[bg][fg]overlay=x='(W-w)*{px:.4f}':y='(H-h)*{py:.4f}':eval=frame,setsar=1[vgeo]")
    return chain, "[vgeo]", (ow, oh)


# ---------- color grading ----------
LUT_KEYS = ("brightness", "contrast", "saturation", "temperature", "tint", "gamma", "fade")  # baked into the 3D LUT
COLOR_KEYS = LUT_KEYS + ("vignette", "sharpen")


def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def color_transform(color):
    """Affine RGB matrix (4x4, rows r,g,b,1; offsets in 0..1) + gamma exponent.
    Must stay identical to colorTransform() in web/editor.js so the preview matches the export."""
    v = {k: float((color or {}).get(k) or 0) / 100 for k in LUT_KEYS}
    ident = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    b = 1 + v["brightness"]
    bright = [[b, 0, 0, 0], [0, b, 0, 0], [0, 0, b, 0], [0, 0, 0, 1]]
    c = 1 + v["contrast"]
    o = 0.5 * (1 - c)
    contrast = [[c, 0, 0, o], [0, c, 0, o], [0, 0, c, o], [0, 0, 0, 1]]
    s = 1 + v["saturation"]
    sat = [[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s, 0],
           [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s, 0],
           [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s, 0],
           [0, 0, 0, 1]]
    kt, kg = 0.12 * v["temperature"], 0.10 * v["tint"]
    temp = [[1 + kt, 0, 0, 0], [0, 1 - kg, 0, 0], [0, 0, 1 - kt, 0], [0, 0, 0, 1]]
    f = 0.25 * max(0.0, v["fade"])
    fade = [[1 - f, 0, 0, f], [0, 1 - f, 0, f], [0, 0, 1 - f, f], [0, 0, 0, 1]]
    m = ident
    for step in (bright, contrast, sat, temp, fade):  # applied in this order
        m = _matmul(step, m)
    gamma = 2 ** (-v["gamma"])
    return m, gamma


def write_lut(path, color, size=33):
    m, gamma = color_transform(color)
    rows = [f"LUT_3D_SIZE {size}"]
    n = size - 1
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                rgb = (ri / n, gi / n, bi / n)
                out = []
                for row in m[:3]:
                    x = row[0] * rgb[0] + row[1] * rgb[1] + row[2] * rgb[2] + row[3]
                    x = min(max(x, 0.0), 1.0) ** gamma
                    out.append(f"{x:.6f}")
                rows.append(" ".join(out))
    with open(path, "w", encoding="ascii") as fh:
        fh.write("\n".join(rows) + "\n")


def _atempo_chain(speed):
    parts = []
    while speed > 2.0:
        parts.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        parts.append(0.5)
        speed /= 0.5
    parts.append(speed)
    return ",".join(f"atempo={p:.5f}" for p in parts)


def _rgba(hex_color, opacity):
    color = hex_color if re.fullmatch(r"#[0-9a-fA-F]{6}", hex_color or "") else "#ffffff"
    return f"0x{color[1:]}@{min(max(float(opacity), 0), 1):.3f}"


def build_graph(info, spec, length, lut_name=None, pix_fmt="yuv420p", text_files=None, logo_index=None):
    """Full filter graph: geometry -> color -> text/logo -> speed -> fades -> pixel format (+ audio).
    Returns (graph, vlabel, alabel, size, out_len)."""
    graph, vlabel, (ow, oh) = build_geometry(info, spec, length)
    color = spec.get("color") or {}
    speed = min(max(float(spec.get("speed") or 1), 0.25), 4.0)
    out_len = length / speed
    fade_in = min(max(float(spec.get("fadeIn") or 0), 0), out_len / 2)
    fade_out = min(max(float(spec.get("fadeOut") or 0), 0), out_len / 2)

    look = []
    if lut_name:
        look.append(f"lut3d=file={lut_name}:interp=tetrahedral")
    sharpen = float(color.get("sharpen") or 0) / 100
    if sharpen > 0:
        look.append(f"unsharp=5:5:{1.5 * sharpen:.3f}:5:5:0")
    vignette = float(color.get("vignette") or 0) / 100
    if vignette > 0:
        look.append(f"vignette=angle={0.15 + 0.7 * vignette:.4f}")
    text = spec.get("text") or {}
    if text_files and (text.get("content") or "").strip():
        fs = max(8, round(oh * float(text.get("size") or 6) / 100))
        op = float(text.get("opacity", 1))
        dt = [f"fontfile={text_files['font']}", f"textfile={text_files['text']}", "expansion=none",
              f"fontsize={fs}", f"fontcolor={_rgba(text.get('color'), op)}", "line_spacing=" + str(round(fs * 0.15)),
              f"x=(w-text_w)*{float(text.get('x', 0.5)):.4f}", f"y=(h-text_h)*{float(text.get('y', 0.9)):.4f}"]
        outline = float(text.get("outline") or 0)
        if outline > 0:
            dt += [f"borderw={max(1, round(fs * outline / 100))}", f"bordercolor={_rgba(text.get('outlineColor'), op)}"]
        if text.get("box"):
            dt += ["box=1", f"boxcolor={_rgba(text.get('boxColor'), float(text.get('boxOpacity', 0.5)) * op)}",
                   f"boxborderw={round(fs * 0.35)}"]
        look.append("drawtext=" + ":".join(dt))
    if look:
        graph += f";{vlabel}{','.join(look)}[vlook]"
        vlabel = "[vlook]"
    logo = spec.get("logo") or {}
    if logo_index is not None and logo.get("path"):
        lw = _even(ow * float(logo.get("size") or 20) / 100)
        graph += (f";[{logo_index}:v]scale={lw}:-2,format=rgba,colorchannelmixer=aa={float(logo.get('opacity', 1)):.3f}[logo];"
                  f"{vlabel}[logo]overlay=x=(W-w)*{float(logo.get('x', 0.95)):.4f}:y=(H-h)*{float(logo.get('y', 0.05)):.4f}[vlogo]")
        vlabel = "[vlogo]"

    timing = []
    if abs(speed - 1) > 1e-3:
        timing.append(f"setpts=PTS/{speed:.5f}")
    if fade_in > 0:
        timing.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        timing.append(f"fade=t=out:st={out_len - fade_out:.3f}:d={fade_out:.3f}")
    timing.append(f"format={pix_fmt}")  # pixel format the encoder accepts
    graph += f";{vlabel}{','.join(timing)}[vout]"
    vlabel = "[vout]"

    # audio: original track (speed-matched) and/or background music (input #1), mixed, then faded
    tracks = []
    if info["hasAudio"] and not spec.get("mute"):
        achain = [_atempo_chain(speed)] if abs(speed - 1) > 1e-3 else []
        volume = float(spec.get("volume", 1))
        if abs(volume - 1) > 1e-3:
            achain.append(f"volume={max(volume, 0):.3f}")
        graph += f";[0:a:0]{','.join(achain) or 'anull'}[aorig]"
        tracks.append("[aorig]")
    music = spec.get("music") or {}
    if music.get("path"):
        mvol = max(float(music.get("volume", 1)), 0)
        graph += f";[1:a:0]atrim=duration={out_len:.3f},asetpts=PTS-STARTPTS,volume={mvol:.3f}[amusic]"
        tracks.append("[amusic]")

    alabel = None
    if tracks:
        fades = []
        if fade_in > 0:
            fades.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            fades.append(f"afade=t=out:st={out_len - fade_out:.3f}:d={fade_out:.3f}")
        mix = f"amix=inputs=2:duration=longest:normalize=0," if len(tracks) == 2 else ""
        # pad with silence + trim so the audio track always spans the whole video (e.g. short music, no loop)
        graph += f";{''.join(tracks)}{mix}apad,atrim=duration={out_len:.3f}{''.join(',' + f for f in fades)}[aout]"
        alabel = "[aout]"
    return graph, vlabel, alabel, (ow, oh), out_len


def _encoder_args(ffmpeg, ow, oh, fps):
    name, label, enc_args, pix = detect_encoder(ffmpeg)
    args = list(enc_args)
    if name in BITRATE_ENCODERS:
        kbps = int(min(max(ow * oh * (fps or 30) * 0.12 / 1000, 2500), 20000))
        args += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
    return label, args, pix


def _run_ffmpeg(cmd, workdir, out_len, dst, on_progress, should_cancel):
    proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    stderr_lines = []
    threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                raise Cancelled()
            if line.startswith("out_time_us=") and on_progress:
                us = line.split("=", 1)[1].strip()
                if us.isdigit():
                    on_progress(min(int(us) / 1e6 / out_len, 1.0))
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        _remove(dst)
        raise
    if proc.returncode != 0:
        _remove(dst)
        raise RuntimeError("ffmpeg failed: " + "".join(stderr_lines).strip()[-300:])


def export(ffmpeg, src, dst, info, spec, on_progress=None, should_cancel=None):
    start = max(0.0, float(spec.get("start") or 0))
    end = float(spec.get("end") or info["duration"]) or info["duration"]
    length = max(0.1, end - start)

    # files referenced from the graph are put in a temp dir and used by relative name (no Windows path escaping)
    workdir = tempfile.mkdtemp(prefix="grabber-edit-")
    try:
        lut_name = None
        if any(float((spec.get("color") or {}).get(k) or 0) for k in LUT_KEYS):
            lut_name = "grade.cube"
            write_lut(os.path.join(workdir, lut_name), spec.get("color"))
        text_files = None
        text = spec.get("text") or {}
        if (text.get("content") or "").strip() and text.get("font") and os.path.isfile(text["font"]):
            font_name = "font" + os.path.splitext(text["font"])[1].lower()
            shutil.copyfile(text["font"], os.path.join(workdir, font_name))
            # newline="\n": a CRLF would be drawn as two line breaks
            with open(os.path.join(workdir, "text.txt"), "w", encoding="utf-8", newline="\n") as f:
                f.write(text["content"].replace("\r", "").strip())
            text_files = {"font": font_name, "text": "text.txt"}

        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src]
        music = spec.get("music") or {}
        if music.get("path"):  # input #1
            if music.get("loop", True):
                cmd += ["-stream_loop", "-1"]
            cmd += ["-ss", f"{max(float(music.get('offset') or 0), 0):.3f}", "-i", music["path"]]
        logo_index = None
        logo = spec.get("logo") or {}
        if logo.get("path") and os.path.isfile(logo["path"]):
            logo_index = 2 if music.get("path") else 1
            cmd += ["-i", logo["path"]]

        geo_size = build_geometry(info, spec, length)[2]
        label, enc_args, pix = _encoder_args(ffmpeg, *geo_size, info["fps"])
        graph, vlabel, alabel, (ow, oh), out_len = build_graph(info, spec, length, lut_name, pix, text_files, logo_index)
        cmd += ["-filter_complex", graph, "-map", vlabel, *enc_args]
        cmd += ["-map", alabel, "-c:a", "aac", "-b:a", "192k"] if alabel else ["-an"]
        cmd += [*(STRIP_ARGS if spec.get("stripMetadata", True) else ["-map_metadata", "0"]),
                "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", dst]
        _run_ffmpeg(cmd, workdir, out_len, dst, on_progress, should_cancel)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {"path": dst, "encoder": label, "width": ow, "height": oh}


# ---------- presets (an edit saved independently of one video) ----------
RATIO_VALUES = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 4 / 5, "4:3": 4 / 3, "3:4": 3 / 4}
PRESET_KEYS = ("rotate", "flipH", "flipV", "zoom", "panX", "panY", "zoomAnimate", "zoomEnd", "zoomEase", "background",
               "color", "speed", "volume", "mute", "fadeIn", "fadeOut", "music", "text", "logo", "stripMetadata")


def resolve_preset(preset, info):
    """Turn a preset (crop stored relative to the frame + aspect ratio) into a spec for one video."""
    spec = {k: preset[k] for k in PRESET_KEYS if k in preset}
    rot = int(preset.get("rotate") or 0) % 360
    rw, rh = rotated_size(info, rot)
    ratio = preset.get("ratio") or "original"
    rel = preset.get("cropRel") or {"x": 0, "y": 0, "w": 1, "h": 1}
    if ratio == "free":
        crop = {"x": rel["x"] * rw, "y": rel["y"] * rh, "w": rel["w"] * rw, "h": rel["h"] * rh}
    else:
        r = RATIO_VALUES.get(ratio, rw / rh)
        w, h = (rh * r, rh) if rw / rh > r else (rw, rw / r)
        # keep the preset's crop centre, clamped inside this video's frame
        cx = (rel["x"] + rel["w"] / 2) * rw
        cy = (rel["y"] + rel["h"] / 2) * rh
        crop = {"x": min(max(cx - w / 2, 0), rw - w), "y": min(max(cy - h / 2, 0), rh - h), "w": w, "h": h}
    spec["crop"] = {k: int(round(v)) for k, v in crop.items()}
    spec["ratio"] = ratio
    spec["start"], spec["end"] = 0, info["duration"]
    return spec


# ---------- merge several clips into one video ----------
MERGE_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}


def merge(ffmpeg, clips, infos, dst, opts, on_progress=None, should_cancel=None):
    """clips: list of paths (in order); opts: {size: '9:16'|...|'first', fit: 'blur'|'black'|'crop',
    transition: seconds of crossfade (0 = cut), stripMetadata}."""
    if len(clips) < 2:
        raise ValueError("Add at least two clips to merge.")
    if opts.get("size") in MERGE_SIZES:
        W, H = MERGE_SIZES[opts["size"]]
    else:
        W, H = _encodable(infos[0]["width"], infos[0]["height"])
    fit = opts.get("fit") or "blur"
    durations = [max(0.1, i["duration"]) for i in infos]
    t = min(max(float(opts.get("transition") or 0), 0), min(durations) / 2 - 0.05)
    t = t if t >= 0.1 else 0
    fps = 30

    parts = []
    for i, d in enumerate(durations):
        base = f"[{i}:v]trim=duration={d:.3f},setpts=PTS-STARTPTS,fps={fps}"
        if fit == "crop":
            parts.append(f"{base},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v{i}]")
        elif fit == "black":
            parts.append(f"{base},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]")
        else:
            parts.append(f"{base},split[b{i}][f{i}];"
                         f"[b{i}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma={max(10, W // 25)}[bg{i}];"
                         f"[f{i}]scale={W}:{H}:force_original_aspect_ratio=decrease[fg{i}];"
                         f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{i}]")
        if infos[i]["hasAudio"]:
            parts.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
                         f"atrim=duration={d:.3f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f}[a{i}]")

    label, enc_args, pix = _encoder_args(ffmpeg, W, H, fps)
    n = len(clips)
    if t == 0:
        parts.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
        total = sum(durations)
    else:
        vprev, aprev, offset = "[v0]", "[a0]", 0.0
        for i in range(1, n):
            offset += durations[i - 1] - t
            vout, aout = (f"[vx{i}]", f"[ax{i}]") if i < n - 1 else ("[vc]", "[ac]")
            parts.append(f"{vprev}[v{i}]xfade=transition=fade:duration={t:.3f}:offset={offset:.3f}{vout}")
            parts.append(f"{aprev}[a{i}]acrossfade=d={t:.3f}{aout}")
            vprev, aprev = vout, aout
        total = sum(durations) - t * (n - 1)
    parts.append(f"[vc]format={pix}[vout]")

    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
    for c in clips:
        cmd += ["-i", c]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[ac]", *enc_args,
            "-c:a", "aac", "-b:a", "192k",
            *(STRIP_ARGS if opts.get("stripMetadata", True) else ["-map_metadata", "0"]),
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", dst]
    workdir = tempfile.mkdtemp(prefix="grabber-merge-")
    try:
        _run_ffmpeg(cmd, workdir, total, dst, on_progress, should_cancel)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {"path": dst, "encoder": label, "width": W, "height": H, "duration": total}


def strip_metadata(ffmpeg, path):
    """Rewrite `path` in place without metadata (stream copy: instant, no quality loss)."""
    root, ext = os.path.splitext(path)
    tmp = f"{root}.strip-tmp{ext}"
    r = _run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy",
              *STRIP_ARGS, "-movflags", "+faststart", tmp])
    if r.returncode != 0:
        _remove(tmp)
        raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
    for attempt in range(20):  # the file may still be open by a player for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                _remove(tmp)
                raise RuntimeError("The file is in use by another program") from None
            threading.Event().wait(0.25)


def _remove(path):
    # a just-killed ffmpeg can keep the file locked for a moment on Windows
    for _ in range(20):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            threading.Event().wait(0.1)


class _MediaHandler(http.server.BaseHTTPRequestHandler):
    """Serves registered files only (by random token), with HTTP Range so <video> can seek."""
    files = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        token = self.path.lstrip("/").split("?")[0]
        path = self.files.get(token)
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else end
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(512 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass  # the player dropped the connection while seeking


class MediaServer:
    def __init__(self):
        self._httpd = None
        self._lock = threading.Lock()

    def url_for(self, path):
        with self._lock:
            if not self._httpd:
                self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MediaHandler)
                self._httpd.daemon_threads = True
                threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        token = secrets.token_urlsafe(16)
        _MediaHandler.files[token] = path
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/{token}"


def unique_output(directory, stem, ext=".mp4"):
    name, n = stem, 2
    while os.path.exists(os.path.join(directory, name + ext)):
        name, n = f"{stem} ({n})", n + 1
    return os.path.join(directory, name + ext)
