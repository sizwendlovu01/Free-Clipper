"""Find the most shareable moments in a transcript.

Providers:
  claude    - Anthropic Claude API (needs ANTHROPIC_API_KEY)
  ollama    - any local model served by Ollama (fully offline)
  heuristic - no AI: speech density, loudness and hook phrases
All providers return the same shape, then go through the same boundary snapping.
"""
from __future__ import annotations

import json
import logging
import math
import re
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .. import config

log = logging.getLogger("freeclipper.highlights")

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "score": {"type": "integer"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start", "end", "title", "reason", "score", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior short-form video editor. You pick moments from long videos \
that will perform as standalone TikTok / YouTube Shorts / Reels clips.

A great clip:
- opens with a hook in the first 3 seconds (a bold claim, question, surprise, conflict or emotional beat);
- is self-contained: a viewer with no context understands it;
- starts at the beginning of a sentence and ends on a finished thought or punchline;
- has one clear idea, story, tip or reaction.

Timestamps in the transcript are in seconds: [start-end] text. Use them exactly.
Write titles and hashtags in the same language as the transcript.
score is 0-100: your honest estimate of viral potential relative to the other candidates.
title: a catchy post title, at most 8 words.
reason: one plain sentence, at most 15 words, on why this moment works as a short clip.
hashtags: 2-4 relevant hashtags. Keep every field short."""

# Rough output size per clip, used for progress while a local model is writing.
TOKENS_PER_CLIP = 75


def _format_transcript(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)


def _user_prompt(segments: list[dict], count: int, min_len: int, max_len: int, extra: str) -> str:
    guidance = f"\nExtra instructions from the user: {extra.strip()}" if extra.strip() else ""
    return (
        f"Pick up to {count} non-overlapping clips. Each clip must be between {min_len} and {max_len} "
        f"seconds long. Order them by score, best first.{guidance}\n\n<transcript>\n"
        f"{_format_transcript(segments)}\n</transcript>"
    )


# ---------------- providers ----------------

def _claude(segments: list[dict], count: int, min_len: int, max_len: int, extra: str) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": config.CLAUDE_EFFORT,
            "format": {"type": "json_schema", "schema": CLIP_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_prompt(segments, count, min_len, max_len, extra)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyze this transcript.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens).")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)["clips"]


PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "score": {"type": "integer"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "reason", "score", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}

PICK_SYSTEM = """You are a senior short-form video editor choosing which moments of a long video \
to publish as standalone TikTok / YouTube Shorts / Reels clips.
You get numbered candidate moments. Choose the ones with the strongest hook in the first seconds, \
a clear self-contained idea and a satisfying ending. Skip filler, intros, ads and moments that need context.
For each pick give: id (the candidate number), title (catchy, at most 8 words, same language as the \
transcript), reason (one plain sentence, at most 15 words), score (0-100 viral potential) and 2-4 hashtags.
Keep every field short."""


def _ollama(segments: list[dict], count: int, min_len: int, max_len: int, extra: str,
            progress: Callable[[float], None], audio: Path | None, duration: float) -> list[dict]:
    """Local model as editor over a shortlist.

    On CPU a small model reads ~1k tokens/s but writes only a few tokens/s, and an hour of speech is
    ~25k tokens. So the instant built-in scorer shortlists the strongest candidate moments (already
    cut on sentence boundaries) and the model reads only those, then answers with candidate ids.
    """
    import httpx

    n_cand = min(18, max(8, count * 3))
    candidates = _heuristic(segments, n_cand, min_len, max_len, audio, duration)
    if not candidates:
        return []
    candidates.sort(key=lambda c: c["start"])
    listing = []
    for i, c in enumerate(candidates, 1):
        text = " ".join(s["text"] for s in segments if s["start"] >= c["start"] - 0.01 and s["end"] <= c["end"] + 0.01)
        listing.append(f"#{i} ({c['end'] - c['start']:.0f}s): {text[:1200]}")
    guidance = f"\nExtra instructions from the user: {extra.strip()}" if extra.strip() else ""
    prompt = (f"Choose the best {min(count, len(candidates))} candidates, best first.{guidance}\n\n"
              + "\n\n".join(listing) + "\n\nReply with JSON only.")
    needed = (len(PICK_SYSTEM) + len(prompt)) // 3 + 60 * count * 2 + 1024
    num_ctx = min(config.OLLAMA_NUM_CTX, max(4096, 1 << (needed - 1).bit_length()))
    expected = 55 * min(count, len(candidates))
    parts: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
        with client.stream("POST", f"{config.OLLAMA_URL}/api/chat", json={
            "model": config.OLLAMA_MODEL,
            "stream": True,
            "format": PICK_SCHEMA,
            "options": {"temperature": 0.2, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": PICK_SYSTEM}, {"role": "user", "content": prompt}],
        }) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
            for line in resp.iter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(f"Ollama: {chunk['error']}")
                parts.append(chunk.get("message", {}).get("content", ""))
                progress(min(0.98, len(parts) / expected))
                if chunk.get("done"):
                    break
    picks = json.loads("".join(parts) or "{}").get("picks", [])
    out, seen = [], set()
    for p in picks:
        try:
            idx = int(p.get("id")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(candidates) and idx not in seen:
            seen.add(idx)
            c = candidates[idx]
            out.append({"start": c["start"], "end": c["end"], "title": p.get("title") or c["title"],
                        "reason": p.get("reason") or "", "score": p.get("score", 50),
                        "hashtags": p.get("hashtags") or []})
    return out


HOOK_WORDS = re.compile(
    r"\b(how|why|what|secret|never|always|best|worst|mistake|truth|actually|imagine|crazy|insane|"
    r"wrong|stop|nobody|everyone|biggest|first|only|money|love|hate|problem|story|wait)\b",
    re.IGNORECASE,
)


def _loudness_per_second(audio: Path | None) -> np.ndarray | None:
    if not audio or not audio.exists():
        return None
    try:
        with wave.open(str(audio), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        n = len(samples) // rate
        if n == 0:
            return None
        rms = np.sqrt(np.mean(samples[: n * rate].reshape(n, rate) ** 2, axis=1) + 1e-9)
        db = 20 * np.log10(rms)
        return (db - db.mean()) / (db.std() + 1e-6)
    except Exception:
        log.exception("loudness analysis failed")
        return None


def _select(candidates: list[dict], count: int, avoid: list[dict]) -> list[dict]:
    """Choose up to `count` non-overlapping windows with the highest total score (weighted interval
    scheduling). Unlike picking the single best window repeatedly, this finds the requested number of
    clips whenever the timeline allows it, using shorter windows where needed."""
    import bisect

    cands = [c for c in candidates
             if not any(c["start"] < a["end"] - 0.6 and c["end"] > a["start"] + 0.6 for a in avoid)]
    if not cands or count <= 0:
        return []
    cands.sort(key=lambda c: c["end"])
    low = min(c["raw"] for c in cands)
    weight = [c["raw"] - low + 1.0 for c in cands]  # positive, so an extra clip never lowers the total
    ends = [c["end"] for c in cands]
    neg = float("-inf")
    n = len(cands)
    prev = [bisect.bisect_right(ends, cands[j]["start"] + 0.01, 0, j) for j in range(n)]
    dp = [[0.0] + [neg] * count for _ in range(n + 1)]
    take = [[False] * (count + 1) for _ in range(n + 1)]
    for j in range(1, n + 1):
        p, w = prev[j - 1], weight[j - 1]
        row, before = dp[j], dp[j - 1]
        for m in range(1, count + 1):
            skip = before[m]
            pick = dp[p][m - 1] + w if dp[p][m - 1] != neg else neg
            if pick > skip:
                row[m], take[j][m] = pick, True
            else:
                row[m] = skip
    m = max(k for k in range(count + 1) if dp[n][k] != neg)
    chosen, j = [], n
    while m > 0 and j > 0:
        if take[j][m]:
            chosen.append(cands[j - 1])
            j, m = prev[j - 1], m - 1
        else:
            j -= 1
    return sorted(chosen, key=lambda c: c["raw"], reverse=True)


def _heuristic(segments: list[dict], count: int, min_len: int, max_len: int, audio: Path | None,
               duration: float, avoid: list[dict] | None = None) -> list[dict]:
    loud = _loudness_per_second(audio)
    candidates = []
    if not segments:
        # No speech: fall back to the loudest windows, in a few lengths so the requested count can fit.
        for length in sorted({min_len, (min_len + max_len) // 2, max_len}):
            for s in np.arange(0, max(0.0, duration - length) + 0.01, max(1.0, min_len / 2)):
                e = min(duration, s + length)
                score = float(loud[int(s):int(e)].mean()) if loud is not None and int(e) > int(s) else 0.0
                candidates.append({"start": float(s), "end": float(e), "raw": score, "title": "Highlight",
                                   "hook": "", "reason": "Loud moment (no speech found)."})
    for i, first in enumerate(segments):
        text_parts, words = [], 0
        for j in range(i, len(segments)):
            seg = segments[j]
            dur = seg["end"] - first["start"]
            if dur > max_len:
                break
            text_parts.append(seg["text"])
            words += max(1, len(seg.get("words") or seg["text"].split()))
            if dur < min_len:
                continue
            text = " ".join(text_parts)
            density = words / dur
            hooks_open = len(HOOK_WORDS.findall(first["text"]))
            hooks_all = len(HOOK_WORDS.findall(text))
            punct = text.count("?") + text.count("!")
            ends_clean = 1.0 if seg["text"].rstrip()[-1:] in ".?!。？！" else 0.0
            loud_score = 0.0
            if loud is not None and int(seg["end"]) > int(first["start"]):
                loud_score = float(loud[int(first["start"]):int(seg["end"])].mean())
            raw = (density * 0.6 + hooks_open * 0.8 + hooks_all * 0.15 + punct * 0.25
                   + ends_clean * 0.7 + loud_score * 0.9 + (0.4 if first["text"].rstrip().endswith("?") else 0))
            title = " ".join(first["text"].split()[:9]).rstrip(",.;:")
            candidates.append({"start": first["start"], "end": seg["end"], "raw": raw, "title": title,
                               "hook": first["text"][:140], "reason": "Dense, energetic speech with a clear opening."})
    picked = _select(candidates, count, avoid or [])
    if picked:
        hi = max(p["raw"] for p in picked)
        lo = min(p["raw"] for p in picked)
        for p in picked:
            p["score"] = int(55 + 40 * ((p["raw"] - lo) / (hi - lo) if hi > lo else 1))
            p["hashtags"] = []
    return picked


# ---------------- boundary snapping ----------------

def _snap(clips: list[dict], segments: list[dict], duration: float, min_len: int, max_len: int,
          count: int) -> list[dict]:
    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    word_starts = [w["start"] for s in segments for w in (s.get("words") or [])]
    word_ends = [w["end"] for s in segments for w in (s.get("words") or [])]

    def nearest(values: list[float], target: float, window: float) -> float | None:
        best = min(values, key=lambda v: abs(v - target), default=None)
        return best if best is not None and abs(best - target) <= window else None

    cleaned = []
    for c in clips:
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(s) and math.isfinite(e)) or e <= s:
            continue
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if segments:
            s = next((v for v in (nearest(starts, s, 3.0), nearest(word_starts, s, 1.5)) if v is not None), s)
            e = next((v for v in (nearest(ends, e, 3.0), nearest(word_ends, e, 1.5)) if v is not None), e)
            if e <= s:
                continue
            if e - s > max_len:
                fits = [x for x in ends if s + min_len <= x <= s + max_len]
                e = fits[-1] if fits else (max([x for x in word_ends if x <= s + max_len], default=s + max_len))
            if e - s < min_len:
                longer = [x for x in ends if x >= s + min_len and x <= s + max_len]
                e = longer[0] if longer else min(duration, s + min_len)
        else:
            e = min(e, s + max_len)
            if e - s < min_len:
                e = min(duration, s + min_len)
        s = max(0.0, s - 0.15)
        e = min(duration, e + 0.35)
        if e - s < 1:
            continue
        score = c.get("score", 50)
        try:
            score = int(max(0, min(100, int(score))))
        except (TypeError, ValueError):
            score = 50
        tags = [t if t.startswith("#") else f"#{t}" for t in (c.get("hashtags") or [])
                if isinstance(t, str) and t.strip()][:8]
        # The hook is the clip's actual opening line, taken from the transcript.
        hook = (c.get("hook") or "").strip() or next(
            (seg["text"] for seg in segments if seg["start"] >= s - 0.5), "")
        cleaned.append({
            "start": round(s, 3), "end": round(e, 3),
            "title": (c.get("title") or "Clip").strip()[:120],
            "hook": hook[:300],
            "reason": (c.get("reason") or "").strip()[:500],
            "score": score, "hashtags": [t.replace(" ", "") for t in tags],
        })
    cleaned.sort(key=lambda c: c["score"], reverse=True)
    final: list[dict] = []
    for c in cleaned:
        if not any(_overlaps(c, f) for f in final):
            final.append(c)
        if len(final) >= count:
            break
    return final


def _overlaps(a: dict, b: dict) -> bool:
    """Clips may share only the ~0.5 s of padding added around their edges, never real content."""
    return min(a["end"], b["end"]) - max(a["start"], b["start"]) > 0.6


def find_highlights(transcript: dict, duration: float, count: int, min_len: int, max_len: int,
                    provider: str, audio: Path | None = None, extra: str = "",
                    progress: Callable[[float], None] | None = None) -> tuple[list[dict], str, str]:
    """Returns (clips, provider_used, note)."""
    progress = progress or (lambda _p: None)
    segments = transcript.get("segments", [])
    min_len = max(3, min(min_len, int(duration)))
    max_len = max(min_len + 1, max_len)
    note = ""
    used = provider
    raw: list[dict] = []
    if provider == "claude" and segments and not config.has_claude_credentials():
        note = "Claude is selected but no ANTHROPIC_API_KEY is set (add it to .env); used the built-in finder."
        provider = "heuristic"
    if provider in ("claude", "ollama") and segments:
        try:
            if provider == "claude":
                raw = _claude(segments, count, min_len, max_len, extra)
            else:
                raw = _ollama(segments, count, min_len, max_len, extra, progress, audio, duration)
        except Exception as exc:  # degrade gracefully, never lose the job
            log.exception("%s highlight detection failed", provider)
            note = f"{provider} analysis failed ({type(exc).__name__}: {str(exc)[:200]}); used the built-in heuristic."
            raw = []
    if not raw:
        if provider in ("claude", "ollama") and not segments:
            note = "No speech found; picked the most energetic moments instead."
        used = "heuristic"
        raw = _heuristic(segments, count, min_len, max_len, audio, duration)
    clips = _snap(raw, segments, duration, min_len, max_len, count)
    if len(clips) < count:
        # The AI (or a first pass) returned fewer distinct moments than requested: top up with the next
        # best non-overlapping moments so the user gets the number they asked for whenever the video
        # is long enough.
        need = count - len(clips)
        extra = _snap(_heuristic(segments, need, min_len, max_len, audio, duration, avoid=clips),
                      segments, duration, min_len, max_len, need)
        for c in extra:
            if len(clips) >= count:
                break
            if not any(_overlaps(c, k) for k in clips):
                clips.append(c)
        clips.sort(key=lambda c: c["score"], reverse=True)
    return clips, used, note


def max_clips_possible(duration: float, min_len: int) -> int:
    """Upper bound on non-overlapping clips of at least min_len seconds."""
    return max(1, int(duration // max(1, min_len)))
