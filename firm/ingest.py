"""Turn a YouTube strategy video into a testable, scoreable strategy.

Pipeline
    URL -> transcript -> rule extraction -> spec -> backtest+optimize
        -> robustness score -> verdict (adopt / ignore) -> EA export

Extraction uses the LLM when a key is available and falls back to a
deterministic keyword/number parser that reads indicator settings straight out
of the transcript ("21 EMA", "RSI above 50", "2:1 risk reward"). The result is
always a spec that composite.py can execute - never free-form code.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .strategies.composite import RULE_TYPES

YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str | None:
    m = YT_ID.search(url or "")
    if m:
        return m.group(1)
    s = (url or "").strip()
    return s if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) else None


# ----------------------------------------------------------------------
# transcript
# ----------------------------------------------------------------------
def _oembed_title(vid: str, timeout: float = 15.0) -> str:
    """Video title via oEmbed. Never raises."""
    try:
        r = httpx.get("https://www.youtube.com/oembed",
                      params={"url": f"https://www.youtube.com/watch?v={vid}",
                              "format": "json"}, timeout=timeout)
        if r.status_code == 200:
            return r.json().get("title", "")
    except Exception:
        pass
    return ""


def fetch_transcript(url: str, timeout: float = 25.0) -> dict:
    """Best-effort transcript + title. Never raises."""
    vid = video_id(url)
    if not vid:
        return {"ok": False, "error": "could not parse a YouTube video id from that URL"}

    # 1) youtube_transcript_api if installed.
    #    v1.x replaced the static get_transcript() with an instance .fetch();
    #    support both so either version works.
    langs = ["en", "en-US", "en-GB"]
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        text = ""
        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):       # v0.x
                parts = YouTubeTranscriptApi.get_transcript(vid, languages=langs)
                text = " ".join(p["text"] for p in parts)
            else:                                                      # v1.x
                fetched = YouTubeTranscriptApi().fetch(vid, languages=langs)
                text = " ".join(s.text for s in fetched)
        except Exception:
            text = ""
        if text.strip():
            text = re.sub(r"\s+", " ", text).strip()
            return {"ok": True, "video_id": vid, "text": text,
                    "title": _oembed_title(vid, timeout),
                    "source": "youtube_transcript_api"}
    except Exception:
        pass

    # 2) timedtext endpoint + oEmbed for the title
    title = _oembed_title(vid, timeout)
    for lang in ("en", "en-US", "en-GB"):
        try:
            r = httpx.get("https://video.google.com/timedtext",
                          params={"lang": lang, "v": vid}, timeout=timeout)
            if r.status_code == 200 and "<text" in r.text:
                text = re.sub(r"<[^>]+>", " ", r.text)
                text = (text.replace("&amp;#39;", "'").replace("&amp;quot;", '"')
                        .replace("&amp;amp;", "&").replace("&#39;", "'"))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return {"ok": True, "video_id": vid, "text": text,
                            "title": title, "source": "timedtext"}
        except Exception:
            continue

    return {"ok": False, "video_id": vid, "title": title,
            "error": ("No public transcript available for this video. Paste the "
                      "strategy description manually, or install "
                      "`pip install youtube-transcript-api` for better coverage."),
            "text": ""}


# ----------------------------------------------------------------------
# heuristic extraction
# ----------------------------------------------------------------------
_NUM = r"(\d{1,3})"


def extract_rules_heuristic(text: str) -> dict:
    """Read indicator settings out of plain speech. Deterministic, free."""
    t = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    entry: list[dict] = []
    filters: list[dict] = []
    notes: list[str] = []

    def nums(pattern: str) -> list[int]:
        return [int(x) for x in re.findall(pattern, t)]

    def _nums_in(hay: str, pattern: str) -> list[int]:
        return [int(x) for x in re.findall(pattern, hay)]

    # moving averages: "21 ema", "ema 21", "50 period moving average"
    # "3 EMA strategy" = a count of EMAs, not a 3-period EMA. Drop small
    # numbers that are immediately followed by a strategy/system noun.
    t_clean = re.sub(rf"\b{_NUM}\s*e\.?m\.?a\s*(?:pullback|strategy|system|"
                     rf"setup|method|cross(?:over)?\s*strategy)\b", " ", t)
    emas = sorted(set(_nums_in(t_clean, rf"{_NUM}\s*(?:period\s*)?e\.?m\.?a\b")
                      + _nums_in(t_clean, rf"\bema\s*{_NUM}")
                      + _nums_in(t_clean, rf"{_NUM}\s*(?:period\s*)?exponential")))
    smas = sorted(set(nums(rf"{_NUM}\s*(?:period\s*)?s\.?m\.?a\b")
                      + nums(rf"\bsma\s*{_NUM}")
                      + nums(rf"{_NUM}\s*(?:day|period)\s*(?:simple\s*)?moving average")))
    emas = [n for n in emas if 2 <= n <= 400]
    smas = [n for n in smas if 2 <= n <= 400]

    if len(emas) >= 3:
        entry.append({"type": "ema_stack", "fast": emas[0], "mid": emas[1], "slow": emas[2]})
        notes.append(f"EMA stack {emas[0]}/{emas[1]}/{emas[2]}")
    elif len(emas) == 2:
        entry.append({"type": "ema_cross", "fast": emas[0], "slow": emas[1]})
        notes.append(f"EMA cross {emas[0]}/{emas[1]}")
    elif len(emas) == 1:
        entry.append({"type": "pullback", "to_period": emas[0], "max_atr": 1.0})
        notes.append(f"pullback to EMA{emas[0]}")
    if len(smas) >= 2:
        entry.append({"type": "sma_cross", "fast": smas[0], "slow": smas[1]})
        notes.append(f"SMA cross {smas[0]}/{smas[1]}")

    # RSI
    if "rsi" in t or "relative strength" in t:
        ob = nums(rf"(?:overbought|above|over)\s*(?:at\s*)?{_NUM}")
        os_ = nums(rf"(?:oversold|below|under)\s*(?:at\s*)?{_NUM}")
        ob = [n for n in ob if 50 <= n <= 95]
        os_ = [n for n in os_ if 5 <= n <= 50]
        if ("oversold" in t or "overbought" in t) and (ob or os_):
            entry.append({"type": "rsi_extreme", "overbought": ob[0] if ob else 70,
                          "oversold": os_[0] if os_ else 30})
            notes.append(f"RSI extremes {os_[0] if os_ else 30}/{ob[0] if ob else 70}")
        else:
            entry.append({"type": "rsi_zone", "min": 40, "max": 80})
            notes.append("RSI momentum zone")

    if "macd" in t:
        entry.append({"type": "macd_cross"})
        notes.append("MACD cross")
    if "bollinger" in t or "band" in t:
        mode = "breakout" if "squeeze" in t or "breakout" in t else "reversion"
        entry.append({"type": "bb_touch", "mode": mode})
        notes.append(f"Bollinger {mode}")
    if any(k in t for k in ("breakout", "break out", "break of structure",
                            "highs", "donchian", "channel")):
        ch = nums(rf"{_NUM}\s*(?:bar|candle|period|day)s?\s*(?:high|low|channel)")
        entry.append({"type": "breakout", "channel": ch[0] if ch else 20})
        notes.append("N-bar breakout")
    if any(k in t for k in ("engulfing", "pin bar", "pinbar", "hammer",
                            "price action", "rejection candle")):
        entry.append({"type": "candle"})
        notes.append("candlestick trigger")
    if "pullback" in t or "retrace" in t or "retest" in t:
        if not any(r["type"] == "pullback" for r in entry):
            entry.append({"type": "pullback", "to_period": emas[1] if len(emas) > 1
                          else (emas[0] if emas else 21), "max_atr": 1.2})
            notes.append("pullback entry")

    # sessions
    if "london" in t:
        filters.append({"type": "session", "from": 7, "to": 16}); notes.append("London session")
    elif "new york" in t or "ny session" in t:
        filters.append({"type": "session", "from": 12, "to": 21}); notes.append("NY session")
    elif "asian" in t or "tokyo" in t:
        filters.append({"type": "session", "from": 0, "to": 9}); notes.append("Asian session")

    # exits
    rr = 2.0
    m = re.search(r"(\d(?:\.\d)?)\s*(?::|to)\s*1\s*(?:risk|reward|r:?r)?", t)
    if m:
        try:
            v = float(m.group(1))
            if 0.5 <= v <= 10:
                rr = v; notes.append(f"{v}:1 reward:risk")
        except ValueError:
            pass
    atr_mult = 2.0
    m2 = re.search(rf"(\d(?:\.\d)?)\s*(?:x|times)?\s*atr", t)
    if m2:
        try:
            v = float(m2.group(1))
            if 0.3 <= v <= 6:
                atr_mult = v; notes.append(f"{v}xATR stop")
        except ValueError:
            pass

    # timeframe
    tf = "H1"
    for pat, val in ((r"\b(?:1|one)[- ]?hour|\bh1\b", "H1"),
                     (r"\b(?:4|four)[- ]?hour|\bh4\b", "H4"),
                     (r"\b15[- ]?min|\bm15\b", "M15"),
                     (r"\b5[- ]?min|\bm5\b", "M5"),
                     (r"\bdaily\b|\bd1\b", "D1")):
        if re.search(pat, t):
            tf = val
            break

    if not entry:
        entry = [{"type": "ema_cross", "fast": 9, "slow": 21},
                 {"type": "rsi_zone", "min": 45, "max": 75}]
        notes.append("no explicit rules found - applied a generic trend template")

    return {"entry": entry, "filters": filters,
            "exit": {"stop": "atr", "atr_mult": atr_mult, "rr": rr},
            "agreement": 0.6, "timeframe": tf, "notes": notes,
            "method": "heuristic"}


EXTRACT_SYSTEM = f"""You convert trading-strategy videos into a machine-testable spec.
Return STRICT JSON only. Available rule types:
{json.dumps(RULE_TYPES, indent=1)}

Schema:
{{"name":"short name","summary":"2 sentences",
 "entry":[{{"type":"...", ...params}}],
 "filters":[{{"type":"session","from":7,"to":16}}],
 "exit":{{"stop":"atr","atr_mult":2.0,"rr":2.0}},
 "agreement":0.6,"timeframe":"H1",
 "symbols":["EURUSD"],
 "confidence":0.0,
 "notes":["what the video actually specified vs what you inferred"]}}

Only use listed rule types. Use the numbers the speaker states. If the video is
vague, infer sensible defaults and say so in notes. Never invent performance claims."""


def extract_rules_llm(text: str, llm, title: str = "") -> dict | None:
    if not llm or not llm.available:
        return None
    body = (text or "")[:14000]
    r = llm.ask("research", "claude-sonnet-4-5", EXTRACT_SYSTEM,
                f"Video title: {title}\n\nTranscript:\n{body}\n\n"
                f"Extract the strategy as JSON.", max_tokens=1600, temperature=0.2)
    data = r.json()
    if isinstance(data, dict) and data.get("entry"):
        data["method"] = f"llm:{r.model}"
        data["llm_cost"] = round(r.usd, 5)
        return data
    return None


def extract(text: str, llm=None, title: str = "") -> dict:
    spec = extract_rules_llm(text, llm, title)
    if spec:
        return spec
    spec = extract_rules_heuristic(text)
    spec.setdefault("name", title or "Extracted strategy")
    spec.setdefault("summary", "Rules parsed from the transcript without an LLM.")
    spec.setdefault("confidence", 0.45)
    return spec
