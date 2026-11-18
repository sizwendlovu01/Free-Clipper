"""Scroll-stopping titles, hashtags and a short "why" for clips, without an AI service.

Used by the built-in highlight finder (Claude and Ollama write their own titles). The clip's punchiest
line becomes the title: questions stay questions, statements become a quote, filler words are removed
and one power word is emphasised. Hashtags are the clip's most distinctive topic words.
"""
from __future__ import annotations

import math
import re
from collections import Counter

POWER_WORDS = {
    "secret", "secrets", "never", "nobody", "truth", "worst", "best", "biggest", "mistake", "mistakes", "stop",
    "crazy", "insane", "wrong", "always", "everyone", "only", "first", "last", "real", "really", "actually",
    "shocking", "hidden", "free", "rich", "money", "million", "broke", "love", "hate", "fear", "afraid",
    "dangerous", "warning", "why", "how", "what", "imagine", "forget", "lie", "lies", "lied", "fail", "failed",
    "success", "win", "won", "lost", "lose", "changed", "change", "life", "dead", "die", "kill", "proof",
    "nobody's", "don't", "can't", "won't", "must", "need", "simple", "easy", "hard", "fast", "instantly",
}
# Words worth capitalising for emphasis (strong and unambiguous).
EMPHASIS = {"never", "nobody", "secret", "truth", "worst", "biggest", "stop", "mistake", "crazy", "insane",
            "wrong", "always", "everyone", "only", "real", "hidden", "warning", "lied", "lies", "proof"}
FILLER_LEAD = re.compile(
    r"^(?:(?:so|and|but|um+|uh+|er+|like|well|okay|ok|yeah|yes|no|right|basically|literally|actually|"
    r"you know|i mean|listen|look|anyway|now|then|oh)\b[\s,.-]*)+", re.IGNORECASE)
FILLER_INLINE = re.compile(r"\b(?:um+|uh+|er+|erm|hmm+)\b[,]?\s*", re.IGNORECASE)
STOPWORDS = set("""a about above after again against all am an and any are aren't as at be because been before being
below between both but by can could did didn't do does doesn't doing don't down during each few for from further
had has have having he her here hers herself him himself his how i if in into is isn't it it's its itself just let
me more most my myself no nor not now of off on once only or other our ours ourselves out over own same she should
so some such than that that's the their theirs them themselves then there there's these they they're this those
through to too under until up very was we we're were what when where which while who whom why will with would you
you're your yours yourself yourselves gonna wanna gotta yeah okay like really thing things something stuff know
think going get got make made said say says want just also even still back much many lot lots kind sort mean
people person time times right well good great little big come came take took give gave look see seen tell told
way ways year years day days going went doing done ever every everything anything nothing someone somebody one two
three four five first last next guys guy actually basically literally""".split())
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!。？！])\s+", re.sub(r"\s+", " ", text).strip())
    out = []
    for p in parts:
        p = p.strip()
        # Very long run-on sentences: also consider their comma-separated pieces.
        if len(p.split()) > 18:
            out.extend(x.strip() for x in re.split(r"(?<=[,;:])\s+", p) if len(x.split()) >= 4)
        out.append(p)
    return [p for p in out if p]


def _clean(sentence: str) -> str:
    s = FILLER_INLINE.sub("", sentence)
    s = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", s, flags=re.IGNORECASE)  # "the the" -> "the"
    for _ in range(3):
        s = FILLER_LEAD.sub("", s).strip(" ,.-")
    return s.strip()


def _shorten(s: str, max_words: int = 12) -> str:
    words = s.split()
    if len(words) <= max_words:
        return s
    cut = " ".join(words[:max_words])
    # Prefer ending at a natural break inside the limit.
    m = re.search(r"^(.{20,}?)[,;:]\s", cut)
    return (m.group(1) if m else cut).rstrip(",;:-") + "…"


def _score(sentence: str, position: float, english: bool) -> float:
    words = [w.lower().strip("'’") for w in _WORD.findall(sentence)]
    n = len(words)
    if n < 3:
        return -10.0
    score = 0.0
    if sentence.rstrip().endswith("?"):
        score += 3.0
    if sentence.rstrip().endswith("!"):
        score += 1.0
    if re.search(r"\d", sentence):
        score += 1.5
    if english:
        score += 1.5 * sum(1 for w in words if w in POWER_WORDS)
        if any(w in ("you", "your", "you're") for w in words):
            score += 1.0
    if 5 <= n <= 12:
        score += 2.0
    elif 13 <= n <= 18:
        score += 0.5
    elif n > 22:
        score -= 2.0
    score += 1.0 * (1 - position)  # earlier lines hook viewers sooner
    return score


def viral_title(text: str, language: str | None = None) -> str:
    """The clip's most clickable line, styled as a short-form post title."""
    english = (language or "en").lower().startswith("en")
    sentences = _sentences(text)
    if not sentences:
        return "Watch this"
    ranked = []
    for i, raw in enumerate(sentences):
        cleaned = _clean(raw)
        if cleaned:
            ranked.append((_score(cleaned, i / max(1, len(sentences) - 1), english), i, cleaned))
    if not ranked:
        return "Watch this"
    ranked.sort(key=lambda r: (-r[0], r[1]))
    best = _shorten(ranked[0][2])
    best = best[0].upper() + best[1:]
    question = best.endswith("?")
    best = best.rstrip(".")
    if english:
        emphasized = False
        words = best.split()
        for k, w in enumerate(words):
            core = re.sub(r"[^\w'’]", "", w).lower()
            if not emphasized and core in EMPHASIS and k > 0:
                words[k] = w.replace(re.sub(r"[^\w'’]", "", w), core.upper())
                emphasized = True
        best = " ".join(words)
    if question:
        return best
    return f"“{best}”"


# Power words that are also good topics; every other power word is an adverb, verb or adjective.
TOPIC_POWER = {"money", "love", "life", "success", "secret", "secrets", "truth", "mistake", "mistakes", "fear",
               "proof", "rich", "million", "story"}
NOT_TOPICS = (POWER_WORDS - TOPIC_POWER) | set("""work worked working works touch touched used using use keep kept
start started stop stopped tried trying try feel felt call called calling move moved turn turned help helped
believe looked looking talk talking talked sure maybe probably different another everybody anybody whole half
today tomorrow yesterday later early before after always minute minutes hour hours week weeks month months
literally honestly seriously totally completely exactly pretty quite""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _topic_words(words: list[str], english: bool) -> list[str]:
    return [w for w in words if len(w) >= 4 and not (english and (
        w in STOPWORDS or w in NOT_TOPICS or (w.endswith(("ed", "ly")) and w not in TOPIC_POWER)))]


def hashtags(text: str, corpus: Counter | None = None, language: str | None = None, limit: int = 3) -> list[str]:
    """The video's main topic plus the clip's most distinctive topic words, then the discovery tags."""
    english = (language or "en").lower().startswith("en")
    words = _topic_words([w.lower().strip("'’-") for w in _WORD.findall(text)], english)
    counts = Counter(words)
    picks: list[str] = []
    if corpus:
        # The video's overall topic, if this clip talks about it.
        topic = [w for w, _ in Counter({w: c for w, c in corpus.items()
                                         if w in set(_topic_words([w], english))}).most_common(5)]
        picks += [w for w in topic if w in counts][:1]
        total = sum(corpus.values())
        ranked = sorted(counts, key=lambda w: (-(counts[w] * math.log(1 + total / (1 + corpus.get(w, 0)))), w))
    else:
        ranked = [w for w, _ in counts.most_common()]
    for w in ranked:
        if len(picks) >= limit:
            break
        if all(_stem(w) != _stem(x) for x in picks):
            picks.append(w)
    tags = ["#" + re.sub(r"[^\w]", "", w) for w in picks]
    return [t for t in tags if len(t) > 3] + ["#fyp", "#viral"]


def reason(text: str, start_text: str, loud: float, ends_clean: bool) -> str:
    """One plain sentence on why the clip was picked."""
    bits = []
    if start_text.rstrip().endswith("?"):
        bits.append("opens with a question")
    hooks = sorted({w.lower() for w in _WORD.findall(start_text) if w.lower() in POWER_WORDS and len(w) > 3})
    if hooks:
        bits.append("hooks with " + ", ".join(f"“{h}”" for h in hooks[:2]))
    if loud > 0.6:
        bits.append("high energy")
    if ends_clean:
        bits.append("ends on a complete thought")
    if not bits:
        bits.append("dense, fast-moving speech")
    out = ", ".join(bits)
    return out[0].upper() + out[1:] + "."
