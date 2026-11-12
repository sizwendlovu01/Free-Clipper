"""Cut, reframe (face tracking), caption and encode one clip with ffmpeg."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path

from .. import config
from . import captions, media

log = logging.getLogger("freeclipper.render")

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


# ---------------- face tracking ----------------
#
# Samples the clip every 0.1 s for camera cuts and every 0.5 s for faces. The crop follows the
# main face inside each shot (gentle pans past a dead zone) and jumps instantly at camera cuts.

YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
_yunet_lock = threading.Lock()


def _yunet_model() -> Path | None:
    """OpenCV's YuNet face detector (233 KB, handles angled faces). Downloaded once; None if offline."""
    path = config.DATA_DIR / "models" / "face_detection_yunet_2023mar.onnx"
    with _yunet_lock:
        if path.exists() and path.stat().st_size > 100_000:
            return path
        try:
            import httpx

            path.parent.mkdir(parents=True, exist_ok=True)
            resp = httpx.get(YUNET_URL, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            if len(resp.content) < 100_000:
                raise ValueError("unexpected model size")
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(path)
            return path
        except Exception as exc:
            log.warning("YuNet face model unavailable (%s); using Haar cascades", exc)
            return None


class _FaceDetector:
    def __init__(self):
        import cv2

        self.cv2 = cv2
        model = _yunet_model()
        self.yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 20) if model else None
        if not self.yunet:
            base = cv2.data.haarcascades
            self.frontal = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
            self.profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")

    def detect(self, small) -> list[tuple[float, float]]:
        """Faces in a downscaled BGR frame as (normalized center x, normalized area)."""
        cv2 = self.cv2
        h, w = small.shape[:2]
        if self.yunet:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(small)
            if faces is None:
                return []
            return [((f[0] + f[2] / 2) / w, f[2] * f[3] / (w * h)) for f in faces]
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        found = [((x + fw / 2) / w, fw * fh / (w * h))
                 for x, y, fw, fh in self.frontal.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))]
        if not found:
            found = [((x + fw / 2) / w, fw * fh / (w * h))
                     for x, y, fw, fh in self.profile.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))]
        if not found:
            flipped = cv2.flip(gray, 1)
            found = [(1 - (x + fw / 2) / w, fw * fh / (w * h))
                     for x, y, fw, fh in self.profile.detectMultiScale(flipped, 1.1, 5, minSize=(24, 24))]
        return found


def _face_track(src: Path, start: float, end: float, fps: float, src_w: int, crop_w: int) -> str | None:
    """Return an ffmpeg expression for the crop x position that follows the main face, or None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    detector = _FaceDetector()
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        return None
    cut_every = max(1, int(round(fps * 0.1)))
    face_every = max(cut_every, int(round(fps * 0.5)))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    samples: list[tuple[float, float | None, bool]] = []
    total_frames = int((end - start) * fps) + 1
    prev_thumb = None
    last_choice: float | None = None
    try:
        for frame_idx in range(total_frames):
            if not cap.grab():
                break
            if frame_idx % cut_every:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                break
            thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (32, 18), interpolation=cv2.INTER_AREA)
            cut = prev_thumb is not None and float(np.mean(cv2.absdiff(thumb, prev_thumb))) > 28
            prev_thumb = thumb
            if not (cut or frame_idx % face_every == 0):
                continue
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (480, max(1, int(h * 480 / w))), interpolation=cv2.INTER_AREA)
            faces = [f for f in detector.detect(small) if f[1] > 0.002]
            if cut:
                last_choice = None
            choice = None
            if faces:
                biggest = max(faces, key=lambda f: f[1])
                choice = biggest[0]
                if last_choice is not None:  # stay on the same person unless they became much smaller
                    near = min(faces, key=lambda f: abs(f[0] - last_choice))
                    if near[1] >= 0.5 * biggest[1]:
                        choice = near[0]
                last_choice = choice
            samples.append((frame_idx / fps, choice, cut))
    finally:
        cap.release()
    return crop_expression(samples, src_w, crop_w)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def crop_expression(samples: list[tuple], src_w: int, crop_w: int) -> str | None:
    """samples: (time, normalized face center x or None[, camera cut here]) -> crop-x expression."""
    samples = [(s[0], s[1], bool(s[2]) if len(s) > 2 else False) for s in samples]
    if sum(1 for s in samples if s[1] is not None) < 2:
        return None
    # Split into shots at camera cuts.
    shots: list[list[tuple[float, float | None]]] = []
    for t, c, cut in samples:
        if cut or not shots:
            shots.append([])
        shots[-1].append((t, c))
    max_x = src_w - crop_w

    def to_x(center: float) -> float:
        return max(0.0, min(max_x, center * src_w - crop_w / 2))

    points: list[tuple[float, float]] = []
    prev_center = 0.5
    dead_zone = crop_w * 0.18 / src_w
    for shot in shots:
        known = [c for _, c in shot if c is not None]
        if not known:
            filled = [(t, prev_center) for t, _ in shot]
        else:
            # Fill gaps inside the shot from the nearest detection, then median-filter blips away.
            raw, last = [], known[0]
            for t, c in shot:
                last = c if c is not None else last
                raw.append((t, last))
            filled = [(t, _median([c for _, c in raw[max(0, i - 2): i + 3]])) for i, (t, _) in enumerate(raw)]
        t0, cur = filled[0]
        if points:
            points.append((max(points[-1][0], t0 - 0.02), points[-1][1]))  # hold until the cut...
        points.append((t0, to_x(cur)))                                    # ...then jump
        for t, c in filled[1:]:
            if abs(c - cur) > dead_zone and t - points[-1][0] > 0.6 and len(points) < 150:
                points.append((t, to_x(cur)))
                points.append((t + 0.35, to_x(c)))
                cur = c
        prev_center = cur
    points[0] = (0.0, points[0][1])
    if all(abs(x - points[0][1]) < 0.5 for _, x in points):
        return f"{points[0][1]:.1f}"
    expr = f"{points[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(points, points[1:]))):
        if t1 <= t0:
            continue
        seg = f"{x0:.1f}" if abs(x1 - x0) < 0.5 else f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{t1 - t0:.3f}"
        expr = f"if(lt(t\\,{t1:.3f})\\,{seg}\\,{expr})"
    return expr


# ---------------- filter graph ----------------

def _video_chain(src: Path, info: dict, start: float, end: float, out_w: int, out_h: int,
                 mode: str, accent: str) -> str:
    """Builds the filter graph up to the [v0] label."""
    if not info["has_video"]:
        color = (accent or "#7c5cff").lstrip("#")
        return (f"color=c=0x0f0f14:s={out_w}x{out_h}:r=30[bg];"
                f"[0:a]showwaves=s={out_w}x{out_h // 4}:mode=cline:scale=sqrt:colors=0x{color}:rate=30[wv];"
                f"[bg][wv]overlay=0:(H-h)/2:shortest=1,setsar=1[v0]")
    w, h = info["width"], info["height"]
    target = out_w / out_h
    source = w / h if h else target
    if abs(source - target) / target < 0.04:
        mode = "fit_black"
    if mode in ("auto", "face", "center"):
        if source > target:
            crop_w, crop_h = _even(h * target), _even(h)
        else:
            crop_w, crop_h = _even(w), _even(w / target)
        x_expr = f"{(w - crop_w) / 2:.1f}"
        y_expr = f"{(h - crop_h) / 2:.1f}"
        if mode in ("auto", "face") and source > target:
            tracked = _face_track(src, start, end, info["fps"], w, crop_w)
            if tracked:
                x_expr = tracked
        return (f"[0:v]crop={crop_w}:{crop_h}:{x_expr}:{y_expr},"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v0]")
    if mode == "fit_blur":
        return (f"[0:v]split[a][b];"
                # Blur at quarter size, then upscale: same look, a fraction of the CPU.
                f"[a]scale={_even(out_w / 4)}:{_even(out_h / 4)}:force_original_aspect_ratio=increase,"
                f"crop={_even(out_w / 4)}:{_even(out_h / 4)},boxblur=8:2,eq=brightness=-0.08,"
                f"scale={out_w}:{out_h}:flags=bilinear[bg];"
                f"[b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    return (f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v0]")


def _watermark_xy(position: str, margin: int) -> str:
    return {
        "top-left": f"{margin}:{margin}",
        "top-right": f"W-w-{margin}:{margin}",
        "bottom-left": f"{margin}:H-h-{margin}",
        "bottom-right": f"W-w-{margin}:H-h-{margin}",
    }.get(position, f"W-w-{margin}:{margin}")


def render_clip(job_dir: Path, src: Path, info: dict, clip: dict, transcript: dict, options: dict,
                style: dict, brand: dict) -> dict:
    out_dir = job_dir / "clips" / clip["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start
    out_w, out_h = ASPECTS.get(options.get("aspect", "9:16"), ASPECTS["9:16"])
    if options.get("aspect") == "original" and info["has_video"]:
        scale = min(1.0, 1920 / max(info["width"], info["height"]))
        out_w, out_h = _even(info["width"] * scale), _even(info["height"] * scale)
    language = transcript.get("language")

    # Captions in open formats, timed relative to the clip.
    words = captions.words_in_range(transcript, start, end)
    sep = captions.joiner(language)
    cues = captions.cues_from_groups(captions.group_words(words, 7, 42), sep)
    (out_dir / "captions.srt").write_text(captions.to_srt(cues), encoding="utf-8")
    (out_dir / "captions.vtt").write_text(captions.to_vtt(cues), encoding="utf-8")
    burn = bool(options.get("captions", True)) and bool(words)
    if burn:
        (out_dir / "captions.ass").write_text(
            captions.to_ass(words, style, out_w, out_h, language), encoding="utf-8")

    graph = _video_chain(src, info, start, end, out_w, out_h, options.get("reframe", "auto"),
                         brand.get("accent", "#7c5cff"))
    label = "v0"
    if burn:
        sub = "subtitles=captions.ass"
        if any(config.FONTS_DIR.iterdir()):
            try:
                rel = os.path.relpath(config.FONTS_DIR, out_dir).replace("\\", "/")
                sub += f":fontsdir='{rel}'"
            except ValueError:
                pass
        graph += f";[v0]{sub}[v1]"
        label = "v1"

    inputs = ["-ss", f"{start:.3f}", "-i", str(src)]
    wm = brand.get("watermark") or {}
    logo = config.BRAND_DIR / "logo.png"
    if wm.get("enabled") and logo.exists():
        inputs += ["-i", str(logo)]
        size = _even(out_w * float(wm.get("size", 0.18)))
        opacity = max(0.05, min(1.0, float(wm.get("opacity", 0.8))))
        graph += (f";[1:v]format=rgba,scale={size}:-1,colorchannelmixer=aa={opacity:.2f}[wm];"
                  f"[{label}][wm]overlay={_watermark_xy(wm.get('position', 'top-right'), int(out_w * 0.04))}[v2]")
        label = "v2"

    tmp = out_dir / "clip.tmp.mp4"
    cmd = [config.FFMPEG, "-y", "-v", "error", *inputs, "-t", f"{duration:.3f}",
           "-filter_complex", graph, "-map", f"[{label}]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        if options.get("loudnorm", True):
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", str(config.X264_CRF),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", tmp.name]
    media.run(cmd, cwd=out_dir)
    # Never report success for a broken file: the output must be a real, full-length video.
    check = media.probe(tmp)
    if not check["has_video"] or check["duration"] < min(1.0, duration * 0.9) or \
            abs(check["duration"] - duration) > max(1.5, duration * 0.1):
        raise media.MediaError(f"Rendered file is incomplete ({check['duration']:.1f}s of {duration:.1f}s).")
    shutil.move(str(tmp), str(out_dir / "clip.mp4"))
    media.thumbnail(out_dir / "clip.mp4", out_dir / "thumb.jpg", min(1.0, duration / 2))

    meta = {
        "id": clip["id"], "title": clip.get("title", ""), "hook": clip.get("hook", ""),
        "reason": clip.get("reason", ""), "score": clip.get("score", 0),
        "hashtags": clip.get("hashtags", []), "source_start": start, "source_end": end,
        "duration": round(duration, 3), "width": out_w, "height": out_h, "language": language,
        "transcript": sep.join(w["word"] for w in words),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
