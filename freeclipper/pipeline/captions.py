"""Caption generation: SRT, WebVTT and styled ASS (burned in with libass)."""
from __future__ import annotations

# Scripts written without spaces between words.
NO_SPACE_LANGS = {"zh", "ja", "th", "lo", "my", "km", "yue", "bo"}


def joiner(language: str | None) -> str:
    return "" if (language or "").split("-")[0] in NO_SPACE_LANGS else " "


def words_in_range(transcript: dict, start: float, end: float) -> list[dict]:
    """Words overlapping [start, end], re-timed relative to `start`."""
    out = []
    for seg in transcript.get("segments", []):
        if seg["end"] < start or seg["start"] > end:
            continue
        words = seg.get("words") or [{"start": seg["start"], "end": seg["end"], "word": seg["text"]}]
        for w in words:
            mid = (w["start"] + w["end"]) / 2
            if start <= mid <= end:
                out.append({
                    "start": max(0.0, w["start"] - start),
                    "end": max(0.0, min(end, w["end"]) - start),
                    "word": w["word"],
                })
    return out


def group_words(words: list[dict], max_words: int, max_chars: int = 32, gap: float = 0.7) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            too_many = len(cur) >= max_words
            too_long = sum(len(x["word"]) + 1 for x in cur) + len(w["word"]) > max_chars
            paused = w["start"] - cur[-1]["end"] > gap
            sentence_end = cur[-1]["word"][-1:] in ".?!。？！"
            if too_many or too_long or paused or sentence_end:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def cues_from_groups(groups: list[list[dict]], sep: str) -> list[dict]:
    cues = []
    for i, g in enumerate(groups):
        start, end = g[0]["start"], g[-1]["end"]
        if i + 1 < len(groups):
            nxt = groups[i + 1][0]["start"]
            if nxt - end < 0.35:
                end = nxt  # avoid flicker between close cues
        cues.append({"start": start, "end": max(end, start + 0.2), "text": sep.join(w["word"] for w in g),
                     "words": g})
    return cues


def _ts(seconds: float, sep: str) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: list[dict]) -> str:
    return "\n".join(
        f"{i}\n{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}\n{c['text']}\n"
        for i, c in enumerate(cues, 1)
    )


def to_vtt(cues: list[dict]) -> str:
    body = "\n".join(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}\n{c['text']}\n" for c in cues)
    return "WEBVTT\n\n" + body


def segment_cues(transcript: dict) -> list[dict]:
    return [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in transcript.get("segments", [])]


# ---------- ASS ----------

def _ass_color(hex_color: str, opacity: float = 1.0) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    alpha = int(round((1 - max(0.0, min(1.0, opacity))) * 255))
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def to_ass(words: list[dict], style: dict, width: int, height: int, language: str | None) -> str:
    scale = min(width, height) / 1080
    font_size = int(style.get("font_size", 80) * scale)
    outline = float(style.get("outline", 6)) * scale
    shadow = float(style.get("shadow", 2)) * scale
    align = {"bottom": 2, "middle": 5, "top": 8}.get(style.get("position", "bottom"), 2)
    margin_v = int(style.get("margin_v", 250) * height / 1920) if align != 5 else 0
    text_c = _ass_color(style.get("text_color", "#FFFFFF"))
    hl_c = _ass_color(style.get("highlight_color", "#FFE600"))
    if style.get("box"):
        box_c = _ass_color(style.get("box_color", "#000000"), float(style.get("box_opacity", 0.6)))
        border_style, outline_c, back_c, outline = 3, box_c, box_c, max(outline, 8 * scale)
    else:
        border_style, outline_c, back_c = 1, _ass_color(style.get("outline_color", "#000000")), "&H80000000"
    bold = -1 if style.get("bold", True) else 0
    font = (style.get("font") or "Arial").replace(",", " ")
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font},{font_size},{text_c},{text_c},{outline_c},{back_c},{bold},0,0,0,100,100,0,0,"
        f"{border_style},{outline:.1f},{shadow:.1f},{align},{int(70 * scale)},{int(70 * scale)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    sep = joiner(language)
    upper = bool(style.get("uppercase"))
    groups = group_words(words, max(1, int(style.get("words_per_line", 3))),
                         max_chars=max(12, int(42 * width / max(height, 1) + 14)))
    cues = cues_from_groups(groups, sep)
    pop = style.get("animation") == "pop"
    highlight = style.get("highlight_mode", "word") == "word"
    lines = []
    for cue in cues:
        tokens = [_ass_escape(w["word"].upper() if upper else w["word"]) for w in cue["words"]]
        intro = "{\\fscx80\\fscy80\\t(0,90,\\fscx100\\fscy100)}" if pop else ""
        if not highlight:
            lines.append(f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(cue['end'])},Cap,,0,0,0,,"
                         f"{intro}{sep.join(tokens)}")
            continue
        for i, w in enumerate(cue["words"]):
            start = cue["start"] if i == 0 else w["start"]
            end = cue["words"][i + 1]["start"] if i + 1 < len(cue["words"]) else cue["end"]
            if end <= start:
                continue
            parts = [
                f"{{\\c{hl_c}\\fscx108\\fscy108}}{t}{{\\c{text_c}\\fscx100\\fscy100}}" if j == i else t
                for j, t in enumerate(tokens)
            ]
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,"
                         f"{intro if i == 0 else ''}{sep.join(parts)}")
    return header + "\n".join(lines) + "\n"
