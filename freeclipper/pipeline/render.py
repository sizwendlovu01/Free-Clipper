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

# Every clip renders at 720p (the short side is 720 px): sharp on phones, small files, fast to encode.
ASPECTS = {"9:16": (720, 1280), "1:1": (720, 720), "4:5": (720, 900), "16:9": (1280, 720)}


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


# ---------------- face tracking ----------------
#
# Samples the clip every 0.1 s for camera cuts and every 0.5 s for faces. The crop follows the
# main face inside each shot (gentle pans past a dead zone) and jumps instantly at camera cuts.

YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
_yunet_lock = threading.Lock()


def _yunet_model() -> Path | None:
    """OpenCV's YuNet face detector (233 KB, handles angled faces). Baked into the Docker image;
    otherwise downloaded once into MODEL_DIR and checksum-verified. None if unavailable."""
    import hashlib

    with _yunet_lock:
        for candidate in (config.YUNET_MODEL, config.MODEL_DIR / "face_detection_yunet_2023mar.onnx"):
            if candidate.exists() and candidate.stat().st_size > 100_000:
                return candidate
        path = config.MODEL_DIR / "face_detection_yunet_2023mar.onnx"
        try:
            import httpx

            path.parent.mkdir(parents=True, exist_ok=True)
            resp = httpx.get(YUNET_URL, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            if hashlib.sha256(resp.content).hexdigest() != YUNET_SHA256:
                raise ValueError("checksum mismatch")
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

    def boxes(self, small) -> list[tuple[float, float, float, float]]:
        """Faces as normalized (x, y, width, height) boxes."""
        cv2 = self.cv2
        h, w = small.shape[:2]
        if self.yunet:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(small)
            return [] if faces is None else [(f[0] / w, f[1] / h, f[2] / w, f[3] / h) for f in faces]
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        return [(x / w, y / h, fw / w, fh / h)
                for x, y, fw, fh in self.frontal.detectMultiScale(gray, 1.1, 5, minSize=(20, 20))]

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


_SAMPLE_FPS = 10  # frames analysed per second: cut detection on every one, faces every 5th (0.5 s)
_SAMPLE_W = 480


def _face_track(src: Path, start: float, end: float, fps: float, src_w: int, crop_w: int,
                src_h: int = 0) -> str | None:
    """Return an ffmpeg expression for the crop x position that follows the main face, or None.

    ffmpeg decodes the clip (multi-threaded, deblocking skipped: invisible at this size) and hands over
    only 10 small frames per second, about half the CPU of decoding every full frame in Python."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    if not src_h:
        return _face_track_cv2(src, start, end, fps, src_w, crop_w)
    w, h = _SAMPLE_W, _even(src_h * _SAMPLE_W / src_w)
    cmd = [config.FFMPEG, "-v", "error", *media.thread_args(), "-skip_loop_filter", "all", "-flags2", "+fast",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}", "-an", "-sn",
           "-vf", f"fps={_SAMPLE_FPS},scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    detector = _FaceDetector()
    frame_bytes = w * h * 3
    face_every = _SAMPLE_FPS // 2
    samples: list[tuple[float, float | None, bool]] = []
    prev_thumb = None
    last_choice: float | None = None
    with media.stream(cmd) as proc:
        idx = 0
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (32, 18), interpolation=cv2.INTER_AREA)
            cut = prev_thumb is not None and float(np.mean(cv2.absdiff(thumb, prev_thumb))) > 28
            prev_thumb = thumb
            if cut or idx % face_every == 0:
                faces = [f for f in detector.detect(frame) if f[1] > 0.002]
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
                samples.append((idx / _SAMPLE_FPS, choice, cut))
            idx += 1
    if not samples:  # ffmpeg couldn't produce frames: fall back to OpenCV's own decoder
        return _face_track_cv2(src, start, end, fps, src_w, crop_w)
    return crop_expression(samples, src_w, crop_w)


def _face_track_cv2(src: Path, start: float, end: float, fps: float, src_w: int, crop_w: int) -> str | None:
    """Fallback tracker: decodes every frame with OpenCV."""
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
    # The path is the starting position plus one term per move: 0 before the move, a straight ramp during
    # it, the full change after. Terms are added as a balanced tree: ffmpeg refuses expressions nested
    # about 100 deep, which a chain of if()s (or of +s) reaches on long, busy clips.
    # Positions in whole tenths of a pixel, so the changes add up exactly (no rounding drift).
    tenths = [round(x * 10) for _, x in points]
    terms = [f"{tenths[0] / 10:.1f}"]
    for i, ((t0, _), (t1, _)) in enumerate(zip(points, points[1:])):
        d = (tenths[i + 1] - tenths[i]) / 10
        if t1 <= t0 or not d:
            if t1 <= t0:
                tenths[i + 1] = tenths[i]  # a zero-length step can't move: keep the sum in step
            continue
        terms.append(f"if(lt(t\\,{t0:.3f})\\,0\\,if(lt(t\\,{t1:.3f})\\,{d:.1f}*(t-{t0:.3f})/{t1 - t0:.3f}\\,{d:.1f}))")
    return _balanced_sum(terms)


def _balanced_sum(terms: list[str]) -> str:
    """a+b+c+... grouped as ((a+b)+(c+d))+... so the expression is only log2(n) levels deep."""
    while len(terms) > 1:
        terms = [f"({terms[i]}+{terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


# ---------------- filter graph ----------------

def _source_in(info: dict) -> str:
    """The video input, slowed to MAX_OUTPUT_FPS first when the source is faster (a 60 fps stream renders
    at 30 fps: half the frames to filter and encode; TikTok, Shorts and Reels play 30 fps anyway)."""
    cap = config.MAX_OUTPUT_FPS
    if cap and float(info.get("fps") or 0) > cap + 0.5:
        return f"[0:v]fps={cap},"
    return "[0:v]"


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
            tracked = _face_track(src, start, end, info["fps"], w, crop_w, h)
            if tracked:
                x_expr = tracked
        return (f"{_source_in(info)}crop={crop_w}:{crop_h}:{x_expr}:{y_expr},"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v0]")
    if mode == "fit_blur":
        return (f"{_source_in(info)}split[a][b];"
                # Blur at quarter size, then upscale: same look, a fraction of the CPU.
                f"[a]scale={_even(out_w / 4)}:{_even(out_h / 4)}:force_original_aspect_ratio=increase,"
                f"crop={_even(out_w / 4)}:{_even(out_h / 4)},boxblur=8:2,eq=brightness=-0.08,"
                f"scale={out_w}:{out_h}:flags=bilinear[bg];"
                f"[b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    return (f"{_source_in(info)}scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v0]")


# ---------------- streamer layout (gameplay + webcam box) ----------------
#
# Stream clips as streamer clip channels post them: the gameplay fills the frame and the streamer's webcam
# (found by looking for their face) sits in a corner box on top. The caption sits against the webcam box.

STREAMER_CAM_WIDTH = 0.42  # the webcam box's share of the frame width
STREAMER_CAM_MAX_ASPECT = 1.2  # widest the webcam box gets (width / height); lower = taller box


def _find_webcam(src: Path, start: float, end: float, src_w: int, src_h: int) -> tuple | None:
    """Where the streamer's webcam face is, as a normalized (cx, cy, width, height) box.

    Games show faces too, often bigger than the webcam's. The webcam face is the one that stays in the same
    place all clip long, usually near an edge or corner, so faces seen in ~1 frame per second are grouped
    by position and the steadiest, most frequent group wins. None if no face shows up reliably."""
    try:
        import numpy as np
    except ImportError:
        return None
    w = _SAMPLE_W * 2  # webcams are small; sample at 960 px so small faces are still found
    h = _even(src_h * w / src_w)
    duration = end - start
    fps = max(0.5, min(1.0, 30 / max(1.0, duration)))
    # Long clips: decode keyframes only (streams have one every ~2 s) instead of every frame, and take at
    # most `fps` of them per second. Many times faster; short clips keep every frame so enough are seen.
    keyframes = ["-skip_frame", "nokey"] if duration >= 40 else []
    pick = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{1 / fps:.3f})'"
            if keyframes else f"fps={fps:.3f}")
    cmd = [config.FFMPEG, "-v", "error", *media.thread_args(), "-skip_loop_filter", "all", "-flags2", "+fast",
           *keyframes, "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}", "-an", "-sn",
           "-vf", f"{pick},scale={w}:{h}:flags=area", "-fps_mode", "vfr",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    detector = _FaceDetector()
    groups: list[dict] = []  # faces grouped by where they sit in the frame
    frames = 0
    bars: list[tuple[int, int]] = []  # black letterbox rows at the top and bottom of each frame
    grays: list = []  # small grayscale copies, to find the webcam overlay's edges
    with media.stream(cmd) as proc:
        while True:
            buf = proc.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            frames += 1
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            rows = frame[:, int(w * 0.2):int(w * 0.6)].mean(axis=(1, 2))  # middle of the gameplay
            dark = rows < 16
            top = int(np.argmin(dark)) if not dark.all() else 0
            bottom = int(np.argmin(dark[::-1])) if not dark.all() else 0
            bars.append((top, bottom))
            grays.append(frame[::2, ::2].mean(axis=2).astype(np.float32))
            for x, y, bw, bh in detector.boxes(frame):
                if bw * bh < 0.0004:
                    continue
                c = (x + bw / 2, y + bh / 2)
                g = next((g for g in groups if abs(g["cx"] - c[0]) < 0.06 and abs(g["cy"] - c[1]) < 0.08
                          and frames not in g["frames"]), None)
                if g is None:
                    g = {"cx": c[0], "cy": c[1], "frames": set(), "boxes": []}
                    groups.append(g)
                g["frames"].add(frames)
                g["boxes"].append((c[0], c[1], bw, bh))
                n = len(g["boxes"])
                g["cx"] += (c[0] - g["cx"]) / n
                g["cy"] += (c[1] - g["cy"]) / n
    need = max(2, frames // 3)

    def score(g: dict) -> float:
        xs = [b[0] for b in g["boxes"]]
        ys = [b[1] for b in g["boxes"]]
        spread = (max(xs) - min(xs)) + (max(ys) - min(ys))
        edge = max(abs(g["cx"] - 0.5), abs(g["cy"] - 0.5)) > 0.25  # overlays sit near edges and corners
        return len(g["frames"]) / max(1, frames) * (1.5 if edge else 1.0) / (1 + 8 * spread)

    steady = [g for g in groups if len(g["frames"]) >= need]
    if not steady:
        return None
    best = max(steady, key=score)
    boxes = sorted(best["boxes"], key=lambda b: b[2] * b[3])
    cx, cy, bw, bh = boxes[len(boxes) // 2]
    tops, bottoms = sorted(b[0] for b in bars), sorted(b[1] for b in bars)
    bar_top = min(tops[len(tops) // 2], int(h * 0.15)) / h  # never trust more than 15% as "bar"
    bar_bottom = min(bottoms[len(bottoms) // 2], int(h * 0.15)) / h
    ox0, oy0, ox1, oy1 = _overlay_bounds(np.stack(grays), cx, cy, bw, bh)
    return cx, cy, bw, bh, bar_top, bar_bottom, ox0, oy0, ox1, oy1


def _overlay_bounds(frames, cx: float, cy: float, fw: float, fh: float) -> tuple[float, float, float, float]:
    """The webcam overlay's rectangle (normalized x0, y0, x1, y1). Overlays sit in a corner with fixed edges
    while the game behind them changes, so the edge between overlay and game is the strongest line that
    stays put across the sampled frames. Falls back to a generous box around the face."""
    import numpy as np

    n, H, W = frames.shape
    fx0, fx1 = (cx - fw / 2) * W, (cx + fw / 2) * W
    fy0, fy1 = (cy - fh / 2) * H, (cy + fh / 2) * H

    def edge(axis: int, lo: int, hi: int, band: tuple[int, int]) -> int | None:
        """Strongest steady edge between positions lo..hi (columns if axis=1, rows if axis=0)."""
        lo, hi = max(1, lo), min((W if axis == 1 else H) - 1, hi)
        if hi - lo < 4:
            return None
        b0, b1 = max(0, band[0]), min(H if axis == 1 else W, band[1])
        if b1 - b0 < 4:
            return None
        if axis == 1:
            d = np.abs(np.diff(frames[:, b0:b1, lo - 1:hi + 1], axis=2)).mean(axis=1)  # (n, cols)
        else:
            d = np.abs(np.diff(frames[:, lo - 1:hi + 1, b0:b1], axis=1)).mean(axis=2)  # (n, rows)
        profile = np.median(d, axis=0)  # steady across frames
        k = int(np.argmax(profile))
        if profile[k] < 6 or profile[k] < 3 * float(np.median(profile)):
            return None
        return lo + k

    right_side, top_side = cx > 0.5, cy < 0.5
    row_band = (int(fy0 - fh * H), int(fy1 + fh * H))
    if right_side:
        x0 = edge(1, int(W * 0.3), int(fx0) - 2, row_band)
        x0, x1 = (x0 if x0 is not None else max(0.0, fx0 - 3 * fw * W)), W
    else:
        x1 = edge(1, int(fx1) + 2, int(W * 0.7), row_band)
        x0, x1 = 0, (x1 if x1 is not None else min(W, fx1 + 3 * fw * W))
    col_band = (int(x0) + 2, int(x1) - 2)
    if top_side:
        y1 = edge(0, int(fy1) + 2, int(H * 0.75), col_band)
        y0, y1 = 0, (y1 if y1 is not None else min(H, fy1 + 1.5 * fh * H))
    else:
        y0 = edge(0, int(H * 0.25), int(fy0) - 2, col_band)
        y0, y1 = (y0 if y0 is not None else max(0.0, fy0 - 1.5 * fh * H)), H
    return x0 / W, y0 / H, x1 / W, y1 / H


def _streamer_chain(src: Path, info: dict, start: float, end: float, out_w: int, out_h: int,
                    cam_top: bool) -> tuple[str, int] | None:
    """Picture-in-picture stream layout, as streamer clip channels post them: the gameplay fills the whole
    frame and the streamer's webcam sits on top of it as a box flush with the left edge, in the top-left
    or bottom-left corner. Returns the filter graph and the y where the caption's top should go: just under
    a top-left webcam, just above a bottom-left one. None when there's no webcam to show."""
    w, h = info["width"], info["height"]
    if not info["has_video"] or w <= h:
        return None  # already vertical or square: nothing to place
    cam = _find_webcam(src, start, end, w, h)
    if not cam:
        return None
    cx, cy = cam[0] * w, cam[1] * h
    bar_top, bar_bottom = int(cam[4] * h), int(cam[5] * h)
    ox0, oy0, ox1, oy1 = cam[6] * w, cam[7] * h, cam[8] * w, cam[9] * h
    # Webcam box: the whole webcam feed, about two fifths of the frame's width, keeping the feed's shape
    # (within reason) so nobody in the camera is cut off.
    ov_w, ov_h = max(16.0, ox1 - ox0), max(16.0, oy1 - oy0)
    aspect = min(STREAMER_CAM_MAX_ASPECT, max(0.8, ov_w / ov_h))
    box_w = _even(out_w * STREAMER_CAM_WIDTH)
    box_h = _even(box_w / aspect)
    crop_w, crop_h = ov_w, ov_w / aspect
    if crop_h > ov_h:  # feed is taller or wider than the box allows: trim around the face
        crop_h, crop_w = ov_h, ov_h * aspect
    crop_w, crop_h = _even(crop_w), _even(crop_h)
    x0 = min(max(ox0, cx - crop_w / 2), ox1 - crop_w)
    y0 = min(max(oy0, cy - crop_h * 0.42), oy1 - crop_h)
    x0, y0 = max(0.0, min(x0, w - crop_w)), max(0.0, min(y0, h - crop_h))
    # Gameplay: most of the picture, not zoomed in. The full gameplay height (letterbox bars removed) and the
    # full width except the side where the stream's own webcam overlay sits (it would show the streamer
    # twice), fitted to the frame width. The space around it gets a soft, darkened blur of the game.
    content_h = h - bar_top - bar_bottom
    if cx > w / 2:
        gx0, gx1 = 0.0, ox0 - 4
    else:
        gx0, gx1 = ox1 + 4, float(w)
    if gx1 - gx0 < w * 0.55:  # overlay too wide to leave out: keep the full width
        gx0, gx1 = 0.0, float(w)
    g_w, g_h = _even(gx1 - gx0), _even(content_h)
    gx, gy = gx0, float(bar_top)
    fit_w = out_w
    fit_h = _even(out_w * g_h / g_w)
    if fit_h > out_h:
        fit_h, fit_w = out_h, _even(out_h * g_w / g_h)
    fx, fy = (out_w - fit_w) // 2, (out_h - fit_h) // 2
    box_y = 0 if cam_top else out_h - box_h
    small_w, small_h = _even(out_w / 4), _even(out_h / 4)
    graph = (f"{_source_in(info)}split=3[b][g][c];"
             f"[b]crop={g_w}:{g_h}:{gx:.1f}:{gy:.1f},scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
             f"crop={small_w}:{small_h},boxblur=10:2,eq=brightness=-0.12,scale={out_w}:{out_h}:flags=bilinear[bg];"
             f"[g]crop={g_w}:{g_h}:{gx:.1f}:{gy:.1f},scale={fit_w}:{fit_h}:flags=lanczos,setsar=1[game];"
             f"[c]crop={crop_w}:{crop_h}:{x0:.1f}:{y0:.1f},scale={box_w}:{box_h}:flags=lanczos,setsar=1[cam];"
             f"[bg][game]overlay={fx}:{fy}[base];[base][cam]overlay=0:{box_y},setsar=1[v0]")
    return graph, (box_h if cam_top else out_h - box_h)


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
        scale = min(1.0, 720 / min(info["width"], info["height"]))  # 720p, never upscaled
        out_w, out_h = _even(info["width"] * scale), _even(info["height"] * scale)
    language = transcript.get("language")

    # Captions in open formats, timed relative to the clip.
    words = captions.words_in_range(transcript, start, end)
    sep = captions.joiner(language)
    cues = captions.cues_from_groups(captions.group_words(words, 7, 42), sep)
    (out_dir / "captions.srt").write_text(captions.to_srt(cues), encoding="utf-8")
    (out_dir / "captions.vtt").write_text(captions.to_vtt(cues), encoding="utf-8")
    burn = bool(options.get("captions", True)) and bool(words)
    reframe = options.get("reframe", "auto")
    streamer = None
    if reframe in ("streamer", "streamer_bottom") and out_h >= out_w:  # 9:16, 4:5 and 1:1
        streamer = _streamer_chain(src, info, start, end, out_w, out_h, cam_top=(reframe == "streamer"))
        if streamer:
            # Captions sit against the webcam box: across its bottom edge when it's top-left (like the
            # "Lacy" reference), just above it when it's bottom-left (like the "StableRonaldo" one).
            edge = streamer[1]
            font_px = int(style.get("font_size", 80) * min(out_w, out_h) / 1080)
            top = edge - int(font_px * 0.65) if reframe == "streamer" else edge - int(font_px * 1.45)
            style = {**style, "position": "top", "margin_v": max(0, top) * 1920 / out_h}
        else:
            reframe = "auto"  # no webcam found in this clip: follow the main face instead
    if burn:
        (out_dir / "captions.ass").write_text(
            captions.to_ass(words, style, out_w, out_h, language), encoding="utf-8")

    graph = streamer[0] if streamer else _video_chain(src, info, start, end, out_w, out_h, reframe,
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

    # Work file on the container's large temporary disk; only the finished clip goes to the storage volume.
    tmp = config.TEMP_DIR / f"render-{clip['id']}-{os.getpid()}-{threading.get_ident()}.mp4"
    lean = ["-filter_threads", "1"] if config.LOW_MEMORY else []
    cmd = [config.FFMPEG, "-y", "-v", "error", *media.thread_args(), *inputs, "-t", f"{duration:.3f}", *lean,
           "-filter_complex", graph, "-map", f"[{label}]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        if options.get("loudnorm", True):
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", str(config.X264_CRF),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", *media.thread_args()]
    if config.LOW_MEMORY:
        cmd += ["-x264-params", "rc-lookahead=10:sync-lookahead=0"]
    cmd.append(str(tmp))
    try:
        # A clip should encode in a few times its length even on a small server; stop runaway renders.
        media.run(cmd, cwd=out_dir, timeout=min(config.JOB_TIMEOUT_SECONDS, max(300.0, duration * 30)))
        # Never report success for a broken file: the output must be a real, full-length video.
        check = media.probe(tmp)
        if not check["has_video"] or check["duration"] < min(1.0, duration * 0.9) or \
                abs(check["duration"] - duration) > max(1.5, duration * 0.1):
            raise media.MediaError(
                f"Rendered file is incomplete ({check['duration']:.1f}s of {duration:.1f}s).")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
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
