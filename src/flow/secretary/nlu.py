"""Deterministic language understanding for the browser demo.

This is a stand-in for the language model that runs inside the hosted voice agent.
It is rule-based on purpose: predictable, free, and testable. It understands the
common phrasings of a booking call; anything else falls back to a callback.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

NUM_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDINALS = {
    "first": 0,
    "1st": 0,
    "one": 0,
    "second": 1,
    "2nd": 1,
    "two": 1,
    "third": 2,
    "3rd": 2,
    "three": 2,
    "last": -1,
}
HOUR_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "noon": 12,
}

_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|correct|right|perfect|sounds good|that works|"
    r"ok|okay|please do|go ahead|confirm|great|absolutely)\b",
    re.I,
)
_NO = re.compile(r"\b(no|nope|nah|not really|don't|do not|wrong|incorrect)\b", re.I)
_DONE = re.compile(
    r"\b(that's all|that is all|that's it|nothing else|no thanks|no thank you|"
    r"bye|goodbye|all set|i'm good|im good|i am good)\b",
    re.I,
)
_HUMAN = re.compile(
    r"\b(human|real person|a person|someone real|speak to (someone|a|the)|"
    r"talk to (someone|a|the)|representative|operator|manager|owner)\b",
    re.I,
)
_BOOK = re.compile(
    r"\b(book|booking|appointment|schedule|come (out|by|over)|send someone|"
    r"someone (to|come)|visit|available|availability|opening|quote|estimate|"
    r"fix|repair|install|replace|look at|service call|technician|tech|"
    r"plumber|electrician|hvac guy|someone out)\b",
    re.I,
)
_CANCEL = re.compile(r"\b(cancel)\b", re.I)
_RESCHEDULE = re.compile(
    r"\b(reschedule|move my|change my (appointment|booking)|"
    r"different time for my)\b",
    re.I,
)
_QUESTION = re.compile(
    r"(\?|\b(what|how|do you|are you|is there|can you tell|when are|"
    r"where|which|price|cost|charge|fee|hours|open)\b)",
    re.I,
)
_OTHER_TIMES = re.compile(
    r"\b(other|another|different|later|none of|neither|any other|"
    r"something else|next week)\b",
    re.I,
)
_EARLIEST = re.compile(
    r"\b(earliest|soonest|first available|asap|as soon as|whenever|"
    r"any time|anytime)\b",
    re.I,
)
_NOT_NAME = {
    "looking",
    "calling",
    "wondering",
    "having",
    "trying",
    "interested",
    "not",
    "just",
    "here",
    "good",
    "fine",
    "okay",
    "ok",
    "the",
    "a",
    "an",
    "yes",
    "no",
    "sure",
    "hi",
    "hello",
    "hey",
    "sorry",
    "going",
    "need",
    "needing",
    "with",
    "from",
    "about",
    "my",
    "your",
    "available",
    "free",
}


# Everyday words that are never a caller's name (on top of _NOT_NAME).
_COMMON = {
    "thanks",
    "thank",
    "bye",
    "goodbye",
    "ok",
    "okay",
    "yes",
    "yeah",
    "no",
    "nope",
    "what",
    "how",
    "when",
    "where",
    "why",
    "who",
    "do",
    "does",
    "can",
    "could",
    "would",
    "will",
    "is",
    "are",
    "you",
    "guys",
    "weekends",
    "weekend",
    "today",
    "tomorrow",
    "price",
    "cost",
    "much",
    "charge",
    "fix",
    "leak",
    "leaky",
    "tap",
    "furnace",
    "heat",
    "heating",
    "water",
    "heater",
    "noise",
    "weird",
    "broken",
    "urgent",
    "emergency",
    "help",
    "someone",
    "somebody",
    "it",
    "that",
    "this",
    "there",
    "they",
    "we",
    "i",
    "me",
    "us",
    "for",
    "to",
    "of",
    "in",
    "on",
    "at",
    "be",
    "been",
    "still",
    "again",
    "too",
    "also",
    "really",
    "very",
    "pretty",
    "kind",
    "basement",
    "kitchen",
    "sink",
    "toilet",
    "drain",
    "pipe",
    "pipes",
    "ac",
    "air",
    "booked",
    "book",
    "appointment",
    "visit",
    "quote",
    "estimate",
    "morning",
    "afternoon",
    "evening",
    # timing and choice words a caller says while being asked something else
    "first",
    "second",
    "third",
    "last",
    "next",
    "week",
    "earliest",
    "soonest",
    "asap",
    "anytime",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "noon",
    "fine",
    "alright",
    "right",
    "cold",
    "hot",
    "freezing",
    "wet",
    "dead",
    "out",
    "off",
    "down",
    "loud",
    "bad",
    "worse",
    "number",
    "phone",
    "name",
    "please",
    "maybe",
    "nothing",
    "hmm",
    "pardon",
    "huh",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
}


def is_yes(t: str) -> bool:
    return bool(_YES.search(t)) and not re.search(r"\b(no|not)\b", t, re.I)


def is_no(t: str) -> bool:
    return bool(_NO.search(t)) and not _YES.search(t)


def is_done(t: str) -> bool:
    return bool(_DONE.search(t))


def wants_human(t: str) -> bool:
    return bool(_HUMAN.search(t))


def wants_booking(t: str) -> bool:
    return bool(_BOOK.search(t))


def wants_cancel(t: str) -> bool:
    return bool(_CANCEL.search(t)) and not _RESCHEDULE.search(t)


def wants_reschedule(t: str) -> bool:
    return bool(_RESCHEDULE.search(t))


def is_question(t: str) -> bool:
    return bool(_QUESTION.search(t))


def wants_other_times(t: str) -> bool:
    return bool(_OTHER_TIMES.search(t))


def wants_earliest(t: str) -> bool:
    return bool(_EARLIEST.search(t))


def is_emergency(t: str, keywords: list[str]) -> bool:
    low = t.lower()
    return any(k in low for k in keywords)


def extract_phone(t: str) -> str | None:
    words = re.findall(r"[a-z]+|\d+", t.lower())
    digits = "".join(NUM_WORDS.get(w, w if w.isdigit() else "") for w in words)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


_STRONG_NAME = re.compile(
    r"\b(?:my name is|my name's|name is|the name is|you can call me|call me)\s+"
    r"([a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+)?)",
    re.I,
)
# "it's ..." / "i'm ..." usually introduce a name only when we just asked for one:
# "it's making a weird noise" and "i'm calling about" are not names.
_WEAK_NAME = re.compile(
    r"\b(?:this is|it's|it is|i'm|i am)\s+([a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+)?)", re.I
)
_FILLER = {"um", "uh", "yeah", "sure", "it's", "its", "and", "please", "oh", "well", "so"}
# Words a caller often says before giving their name: "fine. Priya", "hi yes, Priya Sharma".
_LEAD_IN = re.compile(
    r"^\s*(?:(?:hi|hello|hey|yes|yeah|yep|sure|ok|okay|fine|alright|well|um|uh|oh|so|right|"
    r"of course|no problem|thanks|thank you)\b[\s,.!]*)+",
    re.I,
)
_CLAUSE = re.compile(r"[,.;!?]| and | but ", re.I)


# Adverbs a caller says where a name could go. A blanket "-ly" rule rejected Emily,
# Kelly and Holly, so they are listed instead.
_ADVERBS = {
    "really", "actually", "basically", "probably", "honestly", "literally", "recently",
    "currently", "finally", "quickly", "usually", "definitely", "only", "early", "likely",
    "totally", "exactly", "apparently", "suddenly", "constantly", "possibly", "hopefully",
    "seriously", "obviously", "completely", "absolutely", "certainly", "unfortunately",
}  # fmt: skip
# Real names ending in "-ing" that the "-ing means a verb" rule would otherwise drop.
_ING_NAMES = {"sterling", "manning", "fleming", "browning", "harding", "golding", "channing"}


def _looks_like_word(w: str) -> bool:
    lw = w.lower()
    if lw in _NOT_NAME or lw in _COMMON or lw in _ADVERBS:
        return True
    # "making", "leaking", "calling" are verbs; short ones (Ming, Jing, King) are names.
    return lw.endswith("ing") and len(lw) >= 6 and lw not in _ING_NAMES


_INTRO_NAME = re.compile(
    r"\b(?:this is|it's|it is)\s+([a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+)?)\s+"
    r"(?:calling|speaking|here)\b",
    re.I,
)


def _clean_name(candidate: str) -> str | None:
    candidate = re.sub(r"[^a-zA-Z'\-\s]", " ", candidate)
    words = [w for w in candidate.split() if w.lower() not in _FILLER]
    if not words or len(words) > 3 or _looks_like_word(words[0]):
        return None
    words = [w for w in words if not _looks_like_word(w)][:2]
    if not words or any(len(w) < 2 and not w.isupper() for w in words):
        return None
    return " ".join(_cap(w) for w in words)


def _cap(word: str) -> str:
    """Priya, Al-Rashid, O'Brien."""
    return re.sub(r"(^|[-'])([a-z])", lambda m: m.group(1) + m.group(2).upper(), word.lower())


# A name the caller volunteers before being asked: "Hi, it's Priya, my furnace ...".
# Without a question to anchor it we need two signals: the word is capitalised (speech
# recognition and typists capitalise names) and the phrase ends right after it.
_VOLUNTEERED = re.compile(
    r"\b(?i:this is|it's|it is|i'm|i am)\s+([A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)?)"
    r"\s*(?:[,.;!?]|$|\s+(?i:and|here|calling|speaking|from)\b)"
)


def extract_name(t: str, expecting: bool = False) -> str | None:
    """The caller's name, or None. ``expecting`` is True when we just asked for it."""
    m = _STRONG_NAME.search(t) or _INTRO_NAME.search(t)
    if m:
        return _clean_name(m.group(1))
    m = _VOLUNTEERED.search(t)
    if m:
        name = _clean_name(m.group(1))
        if name:
            return name
    if not expecting:
        return None
    m = _WEAK_NAME.search(t)
    if m:
        return _clean_name(m.group(1))
    # Asked for a name: try the whole reply, then its first clause ("Priya Sharma, do you
    # work weekends?"), after dropping lead-ins like "fine." or "hi yes,".
    body = _LEAD_IN.sub("", t).strip()
    first = _CLAUSE.split(body, maxsplit=1)[0].strip()
    for cand in (body, first):
        if cand and len(cand.split()) <= 4 and not is_question(cand):
            name = _clean_name(cand)
            if name:
                return name
    return None


def plausible_name(name: str) -> bool:
    """False for values that are clearly not a person's name ("Making", "First", "416...")."""
    words = re.findall(r"[^\s]+", name or "")
    if not words or len(words) > 4 or re.search(r"\d", name):
        return False
    return not any(_looks_like_word(w.strip(".,'")) for w in words)


_REPEAT = re.compile(
    r"^\s*(?:what|pardon|pardon me|sorry|huh|come again|say that again|say again|"
    r"can you repeat(?: that)?|could you repeat(?: that)?|repeat that|what did you say)"
    r"\s*[?.!]*\s*$",
    re.I,
)


def wants_repeat(t: str) -> bool:
    return bool(_REPEAT.match(t))


def extract_day(t: str, today: date) -> date | None:
    low = t.lower()
    if "today" in low:
        return today
    if "tomorrow" in low:
        return today + timedelta(days=1)
    for i, d in enumerate(WEEKDAYS):
        if re.search(rf"\b{d}\b", low):
            delta = (i - today.weekday()) % 7
            if "next" in low and delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    return None


def extract_part(t: str) -> str | None:
    low = t.lower()
    for p in ("morning", "afternoon", "evening"):
        if p in low:
            return p
    return None


def choose_slot(t: str, offered: list[dict], local_starts: list[datetime]) -> int | None:
    """Return the index of the offered slot the caller picked, or None."""
    low = t.lower()
    n = len(offered)
    m = re.search(r"\b(first|1st|second|2nd|third|3rd|last)\b", low) or re.search(
        r"\b(?:option|number|the) (one|two|three|1|2|3)\b(?!\s*(?:o'clock|am|pm|:|p\.m|a\.m))", low
    )
    if m:
        word = m.group(1)
        idx = {"1": 0, "2": 1, "3": 2}.get(word, ORDINALS.get(word))
        idx = n - 1 if idx == -1 else idx
        if idx is not None and 0 <= idx < n:
            return idx
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", low)
    hour = None
    if m:
        hour, ampm = int(m.group(1)), (m.group(3) or "").replace(".", "")
    else:
        for w, h in HOUR_WORDS.items():
            mm = re.search(rf"\b{w}\b\s*(o'clock|am|pm|a\.m\.|p\.m\.)?", low)
            if mm:
                hour, ampm = h, (mm.group(1) or "").replace(".", "")
                break
    if hour is None:
        return None
    candidates = []
    for i, d in enumerate(local_starts):
        h12 = d.hour % 12 or 12
        if h12 != hour:
            continue
        if ampm == "am" and d.hour >= 12 or ampm == "pm" and d.hour < 12:
            continue
        candidates.append(i)
    day_named = next(
        (i for i, d in enumerate(local_starts) if WEEKDAYS[d.weekday()] in low and i in candidates),
        None,
    )
    if day_named is not None:
        return day_named
    return candidates[0] if candidates else None
