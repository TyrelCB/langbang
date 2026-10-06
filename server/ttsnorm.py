"""English text normalization for TTS: numbers, dates, times, units → words.

Pocket TTS reads digits and abbreviations poorly: "Thu, Oct 1, 2026
(13:11 UTC)" came back from Whisper as "Thuoc 1-226, TNWUTC", while the
spelled form "Thursday, October first, twenty twenty-six, at thirteen
eleven U T C" round-tripped intact. Higgs/Qwen cope better but also garble
ISO timestamps ("20261231-2359"). So we say things the way a person reads
them aloud before the text reaches the model.

Pure regex + a small number speller (no dependencies). Ordered passes —
each pass consumes what it recognizes, so later, more generic passes
(bare numbers) never see a date's or clock's digits. Conservative where
English is ambiguous: "Sun"/"Sat"/"Wed"/"Mar" only expand next to a date,
digits glued to letters ("Qwen3.8", "x86", "H100") are left alone.
"""
from __future__ import annotations

import re

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand")]
_ORD_IRREG = {"one": "first", "two": "second", "three": "third", "five": "fifth",
              "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}


def cardinal(n: int) -> str:
    if n < 0:
        return "minus " + cardinal(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("-" + _ONES[o] if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + (" " + cardinal(r) if r else "")
    for v, name in _SCALES:
        if n >= v:
            q, r = divmod(n, v)
            if q >= 1000 and v == 10**12:  # absurdly large: read the digits
                return digits(str(n))
            return cardinal(q) + " " + name + (" " + cardinal(r) if r else "")
    return str(n)


def ordinal(n: int) -> str:
    words = cardinal(n)
    sep = "-" if "-" in words.split(" ")[-1] else " "
    head, _, last = words.rpartition(sep)
    if not head:
        sep = ""
    if last in _ORD_IRREG:
        last = _ORD_IRREG[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + sep + last


def digits(s: str) -> str:
    return " ".join(_ONES[int(c)] if c.isdigit() else c for c in s if c.strip())


def year(n: int) -> str:
    """2026 → twenty twenty-six; 2005 → two thousand five; 1900 → nineteen hundred."""
    if 2000 <= n <= 2009:
        return cardinal(n)
    hi, lo = divmod(n, 100)
    if 1000 <= n <= 9999 and hi % 10 != 0 or 1100 <= n <= 1999 or 2010 <= n <= 2099:
        if lo == 0:
            return cardinal(hi) + " hundred"
        return cardinal(hi) + " " + (("oh " + _ONES[lo]) if lo < 10 else cardinal(lo))
    return cardinal(n)


def decimal(s: str) -> str:
    """'3.14' → three point one four; '1,234.5' → one thousand … point five."""
    s = s.replace(",", "")
    neg = s.startswith("-")
    s = s.lstrip("-+")
    whole, _, frac = s.partition(".")
    out = cardinal(int(whole or 0)) if not (len(whole) > 1 and whole.startswith("0")) else digits(whole)
    if frac:
        out += " point " + digits(frac)
    return ("minus " if neg else "") + out


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
_MON_ABBR = {m[:3].lower(): m for m in MONTHS} | {"sept": "September"}
_DAYS = {"mon": "Monday", "tue": "Tuesday", "tues": "Tuesday", "wed": "Wednesday",
         "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday", "fri": "Friday",
         "sat": "Saturday", "sun": "Sunday"}
_MON_ALT = "|".join(sorted([m for m in MONTHS] + [a.capitalize() for a in _MON_ABBR], key=len, reverse=True))
_DAY_ALT = "|".join(sorted([d.capitalize() for d in _DAYS], key=len, reverse=True))
_FULL_DAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"

# spelled letter by letter (a TTS model sees "UTC" as a word: "utk")
_TZ = {"UTC", "GMT", "EST", "EDT", "CST", "CDT", "MST", "MDT", "PST", "PDT", "AKST",
       "AKDT", "HST", "CET", "CEST", "BST", "IST", "JST", "AEST", "AEDT"}
_TZ_ALT = "|".join(sorted(_TZ, key=len, reverse=True))
# all-caps words a person says as a WORD, not letters
_SAY_AS_WORD = {"NASA", "NATO", "RAM", "ROM", "GIF", "JPEG", "PNG", "LAN", "WAN", "SIM",
                "PIN", "FAQ", "ASAP", "SCUBA", "LASER", "RADAR", "WIFI", "CAPTCHA", "CRUD",
                "OK", "NOAA", "FEMA", "OPEC", "UNICEF", "AIDS", "COVID", "YAML", "JSON",
                "SQL", "GUI", "MCP", "LLM", "TTS", "ASR", "CPU", "GPU", "API", "URL"}
# (the last row are initialisms people DO spell — kept out of the generic
#  letter-spacer because TTS models already say them letter by letter; spacing
#  them only adds pauses)

_CAPS_WORDS = {"AN", "AND", "THE", "FOR", "NOT", "BUT", "ALL", "NEW", "OFF", "ON", "IN", "TO",
               "OF", "IS", "IT", "NO", "YES", "RUN", "GO", "DO", "UP", "OR", "AT", "BY", "WE",
               "BE", "MY", "SO", "ME", "US", "HE", "OUT", "NOW", "GET", "SET", "ADD", "TOP",
               "END", "BIG", "HOT", "OLD", "ONE", "TWO", "SIX", "TEN", "WHY", "HOW", "WHO",
               "ANY", "CAN", "MAX", "MIN", "LOW", "KEY", "LOG", "MAP", "TAB", "RAW", "FIX"}
# ("US" stays a word: "the US" is rarer in replies than "let US know"-style caps)

_UNITS = {  # after a number; longest first in the regex
    "GiB": "gigabytes", "MiB": "megabytes", "KiB": "kilobytes", "TiB": "terabytes",
    "GB": "gigabytes", "MB": "megabytes", "KB": "kilobytes", "TB": "terabytes", "kB": "kilobytes",
    "Gbps": "gigabits per second", "Mbps": "megabits per second",
    "GHz": "gigahertz", "MHz": "megahertz", "kHz": "kilohertz", "Hz": "hertz",
    "ms": "milliseconds", "µs": "microseconds", "us": "microseconds", "ns": "nanoseconds",
    "sec": "seconds", "secs": "seconds", "s": "seconds", "min": "minutes", "mins": "minutes",
    "hr": "hours", "hrs": "hours", "h": "hours",
    "km": "kilometers", "cm": "centimeters", "mm": "millimeters", "m": "meters",
    "kg": "kilograms", "g": "grams", "lb": "pounds", "lbs": "pounds", "oz": "ounces",
    "mph": "miles per hour", "km/h": "kilometers per hour", "ft": "feet", "mi": "miles",
    "°C": "degrees Celsius", "°F": "degrees Fahrenheit", "°": "degrees",
    "W": "watts", "kW": "kilowatts", "V": "volts", "k": "thousand", "K": "thousand",
    "M": "million", "B": "billion", "x": "times", "X": "times", "×": "times",
    "pp": "percentage points", "bps": "basis points", "bp": "basis points",
    "y": "years", "yr": "years", "yrs": "years", "mo": "months", "wk": "weeks",
    "tok/s": "tokens per second", "t/s": "tokens per second",
}
_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
_SINGULAR = {"seconds": "second", "minutes": "minute", "hours": "hour", "meters": "meter",
             "degrees": "degree", "pounds": "pound", "ounces": "ounce", "miles": "mile",
             "watts": "watt", "volts": "volt", "gigabytes": "gigabyte", "megabytes": "megabyte",
             "kilobytes": "kilobyte", "terabytes": "terabyte", "milliseconds": "millisecond",
             "kilometers": "kilometer", "kilograms": "kilogram", "grams": "gram",
             "years": "year", "months": "month", "weeks": "week",
             "percentage points": "percentage point", "basis points": "basis point"}

_ABBREV = [(r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\betc\.", "et cetera"),
           (r"\bvs\.?(?=\s)", "versus"), (r"\bapprox\.(?=\s)", "approximately"),
           (r"\bNo\.\s?(?=\d)", "number "), (r"(?<![\w&])&(?![\w&])", "and"),
           (r"\s*→\s*", " to "), (r"(?<=\s)~(?=\d)", "about "), (r"≈\s*", "about "),
           (r"(?<=\d)\s*–\s*(?=\d)", " to ")]


def _spell_letters(w: str) -> str:
    return " ".join(w)


def _day_word(d: str) -> str:
    return _DAYS.get(d.rstrip(".").lower(), d)


def _mon_word(m: str) -> str:
    m0 = m.rstrip(".")
    return _MON_ABBR.get(m0.lower(), m0) if len(m0) <= 4 else m0


def clock(h: int, mi: int, sec: int | None = None, ampm: str | None = None) -> str:
    """13:11 → thirteen eleven; 09:00 → nine hundred (24h) / nine (12h);
    00:01 → zero oh one; with am/pm → '9:05 a.m.' → nine oh five A M."""
    if ampm:
        hw = cardinal(h if h else 12)
        mw = "" if mi == 0 else (" oh " + _ONES[mi] if mi < 10 else " " + cardinal(mi))
        out = hw + mw + " " + ("A M" if ampm.lower().startswith("a") else "P M")
    else:
        hw = "zero zero" if h == 0 else cardinal(h)  # "zero oh one" lost the hour in ASR round-trips
        if mi == 0:
            out = hw + " hundred"
        else:
            out = hw + (" oh " + _ONES[mi] if mi < 10 else " " + cardinal(mi))
    if sec:
        out += " and " + cardinal(sec) + (" second" if sec == 1 else " seconds")
    return out


def _date_words(y: int | None, mo: int, d: int) -> str:
    out = f"{MONTHS[mo - 1]} {ordinal(d)}"
    return out + (f", {year(y)}" if y is not None else "")


def _ok_date(mo: int, d: int) -> bool:
    return 1 <= mo <= 12 and 1 <= d <= 31


_TIME = r"(?P<h>[01]?\d|2[0-3]):(?P<mi>[0-5]\d)(?::(?P<s>[0-5]\d))?(?:\.\d+)?"
_AMPM = r"(?:\s?(?P<ap>[aApP])\.?\s?[mM]\.?(?![a-zA-Z]))"
_ZONE = rf"(?:\s?(?P<z>Z|{_TZ_ALT}|[+-]\d\d:?\d\d)(?![A-Za-z]))"


def _zone_words(z: str | None) -> str:
    if not z:
        return ""
    if z == "Z":
        return " U T C"
    if z[0] in "+-":
        hh, mm = int(z[1:3]), int(z[-2:])
        return " U T C " + ("plus " if z[0] == "+" else "minus ") + cardinal(hh) + \
            (" " + cardinal(mm) if mm else "")
    return " " + _spell_letters(z)


# emoji, pictographs, dingbats, arrows, box drawing, variation selectors, ZWJ…
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001FC00-\U0001FFFF\u2190-\u21FF\u2300-\u23FF\u2460-\u24FF"
    "\u2500-\u27BF\u2900-\u297F\u2B00-\u2BFF\u3030\u303D\u3297\u3299\uFE00-\uFE0F"
    "\u200B-\u200F\u2060\U000E0000-\U000E007F]+")
_SYMBOLS = [("±", " plus or minus "), ("≥", " at least "), ("≤", " at most "),
            ("≠", " not equal to "), ("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"),
            ("\u2019", "'"), ("\u00a0", " ")]


def _pre(t: str) -> str:
    """Characters Pocket can't say or that wreck its sentence chunker."""
    t = t.replace("→", " → ")  # keep arrows for the "to" rule below
    t = re.sub(r" → ", "\x00", t)
    t = _EMOJI.sub(", ", t)  # an emoji marks a beat (bullet / sign-off), not nothing
    t = t.replace("\x00", " → ")
    for a, b in _SYMBOLS:
        t = t.replace(a, b)
    # list separators read as sentence ends — Pocket only splits an oversized
    # sentence at , ; : so "A 30 B 13 · C 29 D 26 · …" was one 300-token run
    t = t.replace("\u2212", "-")  # Unicode minus (finance tables) → ASCII, signs handled below
    # spaced dash BETWEEN numeric things is a range: "$37.03 – $77.24", "5 - 10%"
    t = re.sub(r"(?<=[\d%])\s+[–—-]{1,2}\s+(?=[~$€£+-]?\d)", " to ", t)
    t = re.sub(r"\s*[·•‣◦|]\s*", ". ", t)
    t = re.sub(r"(?<=\w)\s+[—–-]{1,2}\s+(?=\w)", ", ", t)  # spaced dashes → pause
    t = re.sub(r"\s*—\s*", ", ", t)
    # short parentheticals → commas (not "(303) 555-…" phone area codes)
    t = re.sub(r"\((?!\d{3}\)\s?\d)([^()]{1,240})\)", r", \1,", t)
    t = re.sub(r"\b24/7\b", "twenty-four seven", t)
    t = re.sub(r"\bw/(?=\s)", "with", t)
    # "/yr" "/day" after a number or % → per year / per day
    t = re.sub(r"(?<=[\d%])\s?/\s?(yr|year|y|mo|month|wk|week|day|d|hr|hour|h)\b",
               lambda m: " per " + {"yr": "year", "y": "year", "mo": "month", "wk": "week",
                                    "d": "day", "hr": "hour", "h": "hour"}.get(m[1], m[1]), t)
    t = re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", " ", t)  # ETF/futures, and/or → spaced
    t = re.sub(r"\bttm\b", "trailing twelve months", t, flags=re.I)
    t = re.sub(r"\bYTD\b", "year to date", t)
    t = re.sub(r"\bQoQ\b", "quarter over quarter", t)
    t = re.sub(r"\bYoY\b", "year over year", t)
    t = re.sub(r"~(?=\s?[$€£+-]?\d)", "about ", t)
    # year ranges: 2010–12 → twenty ten to twenty twelve; 2019-2021
    t = re.sub(r"(?<![\d-])((?:19|20)\d\d)[–-]((?:19|20)?\d\d)\b(?!%|\.\d|[–-]\d|[T ]\d\d:)",
               lambda m: f"{year(int(m[1]))} to {year(int(m[2]) if len(m[2]) == 4 else int(m[1][:2] + m[2]))}"
               if int(m[2][-2:]) != int(m[1][-2:]) else m[0], t)
    # sign before a number: "+33%" → plus thirty-three percent; "-5" → minus five
    t = re.sub(r"(?:(?<=^)|(?<=[\s(,:;]))\+(?=\$?\d)", "plus ", t)
    t = re.sub(r"(?:(?<=^)|(?<=[\s(,:;]))-(?=\$?\d)", "minus ", t)
    # paths: speak the file name, not the directory chain (an unbroken
    # 80-char token can't be split into Pocket-sized chunks)
    t = re.sub(r"(?<![\w])(?:~|\.{1,2})?(?:/[\w.@+-]+){2,}/?",
               lambda m: m[0].rstrip("/").rsplit("/", 1)[-1], t)
    t = re.sub(r"(?<=[A-Za-z0-9])_+(?=[A-Za-z0-9])", " ", t)  # snake_case → words
    t = t.replace("*", " ")
    # closing quote AFTER the period ('stopped." Next') hides the sentence end
    # from Pocket's splitter — put the period last
    t = re.sub(r"([.!?])([\"')\]]+)(?=\s|$)", r"\2\1", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"([,.;:])(?:\s*[,.;:])+", lambda m: "." if "." in m[0] else m[1], t)
    return t


def _post(t: str) -> str:
    """Scores and long clauses: give Pocket somewhere to breathe."""
    # "Colts thirty Commanders thirteen" → comma after a score before a Name
    t = re.sub(r"\b((?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:-[a-z]+)?|"
               r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
               r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"
               r" (?=(?!(?:" + "|".join(MONTHS) + r")\b)[A-Z][a-z])", r"\1, ", t)
    # long clauses are fitted by voice.pocket_fit with Pocket's own tokenizer
    return t


def normalize(text: str) -> str:
    t = _pre(text or "")

    # 1. ISO date-time: 2026-12-31T23:59:59Z / 2026-12-31 23:59 UTC
    def iso_dt(m):
        y, mo, d = int(m["y"]), int(m["mo"]), int(m["d"])
        if not _ok_date(mo, d):
            return m[0]
        s = int(m["s"]) if m["s"] else None
        return f"{_date_words(y, mo, d)}, at {clock(int(m['h']), int(m['mi']), s)}{_zone_words(m['z'])}"
    t = re.sub(rf"\b(?P<y>\d{{4}})-(?P<mo>\d\d)-(?P<d>\d\d)[T ]{_TIME}{_ZONE}?", iso_dt, t)

    # 2. ISO date alone
    t = re.sub(r"\b(\d{4})-(\d\d)-(\d\d)\b",
               lambda m: _date_words(int(m[1]), int(m[2]), int(m[3])) if _ok_date(int(m[2]), int(m[3])) else m[0], t)

    # 3. US numeric date 10/1/2026 or 10/01/26
    def us_date(m):
        mo, d, y = int(m[1]), int(m[2]), m[3]
        if not _ok_date(mo, d):
            return m[0]
        yy = int(y) + (2000 if len(y) == 2 else 0)
        return _date_words(yy, mo, d)
    t = re.sub(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\d/])", us_date, t)

    # 4. weekday abbreviations — only right before a date ("Thu, Oct 1")
    t = re.sub(rf"\b({_DAY_ALT})\.?(?=,?\s+(?:{_MON_ALT}\b|\d))",
               lambda m: _day_word(m[1]), t)

    # 5. month (+abbr) day[st|nd|rd|th][, year]  /  day month [year]
    def md(m):
        d = int(m["d"])
        if not 1 <= d <= 31:
            return m[0]
        out = f"{_mon_word(m['mon'])} {ordinal(d)}"
        if m["y"]:
            out += f", {year(int(m['y']))}"  # "Nov 5 2008" too: the comma is the spoken pause
        return out
    t = re.sub(rf"\b(?P<mon>{_MON_ALT})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b"
               rf"(?:(?P<sep>,?\s+)(?P<y>\d{{4}})\b)?", md, t)

    def dm(m):
        d = int(m["d"])
        if not 1 <= d <= 31:
            return m[0]
        out = f"the {ordinal(d)} of {_mon_word(m['mon'])}"
        return out + (f", {year(int(m['y']))}" if m["y"] else "")
    t = re.sub(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{_MON_ALT})\b\.?"
               rf"(?:,?\s+(?P<y>\d{{4}})\b)?", dm, t)
    # bare month abbreviation before a year: "Oct 2026"
    t = re.sub(rf"\b(?P<mon>{'|'.join(a.capitalize() for a in _MON_ABBR)})\.?\s+(?P<y>\d{{4}})\b",
               lambda m: f"{_mon_word(m['mon'])} {year(int(m['y']))}", t)

    # 6. clock times (+am/pm, +zone): 13:11 UTC, 9:05 a.m., 00:01
    def tm(m):
        return clock(int(m["h"]), int(m["mi"]), int(m["s"]) if m["s"] else None, m["ap"]) \
            + _zone_words(m["z"])
    t = re.sub(rf"(?<![\d:]){_TIME}(?![\d:]){_AMPM}?{_ZONE}?", tm, t)
    t = re.sub(r"\b(1[0-2]|[1-9])\s?([aApP])\.?[mM]\.?(?![a-zA-Z])",
               lambda m: cardinal(int(m[1])) + (" A M" if m[2].lower() == "a" else " P M"), t)
    # remaining timezone tokens on their own ("Eastern" is fine already)
    t = re.sub(rf"\b({_TZ_ALT})\b", lambda m: _spell_letters(m[1]), t)

    # 7. years in date-ish context: "in 2026", "since 1999", "2026's", "© 2024"
    t = re.sub(r"\b(in|since|by|of|until|from|before|after|circa|year|during|through)\s+(1[1-9]\d\d|20\d\d)\b(?![.,]\d)",
               lambda m: f"{m[1]} {year(int(m[2]))}", t, flags=re.I)
    t = re.sub(r"\b(1[1-9]\d\d|20\d\d)s\b", lambda m: year(int(m[1])).rsplit(" ", 1)[0] + " " +
               (lambda w: w[:-1] + "ies" if w.endswith("y") else w + "s")(year(int(m[1])).rsplit(" ", 1)[-1]), t)

    # 8. abbreviations / symbols
    for pat, rep in _ABBREV:
        t = re.sub(pat, rep, t)

    # 8b. phone numbers: (555) 123-4567 / 555-123-4567 / 555-1234 → digit groups
    t = re.sub(r"(?<![\w-])(?:\+?1[-. ])?\(?(\d{3})\)?[-. ](\d{3})-(\d{4})(?![\w-])",
               lambda m: ", ".join(digits(g) for g in m.groups()), t)
    t = re.sub(r"(?<![\w-])(\d{3})-(\d{4})(?![\w-])",
               lambda m: f"{digits(m[1])}, {digits(m[2])}", t)

    # 9. money: $1,234.56 / $5 / $2.5M / €3
    def money(m):
        sym, num, scale = m[1], m[2].replace(",", ""), m[3]
        unit = {"$": "dollar", "€": "euro", "£": "pound"}[sym]
        sc = {"k": " thousand", "K": " thousand", "M": " million", "B": " billion",
              "bn": " billion", "m": " million"}.get(scale or "", "")
        if sc:
            return decimal(num) + sc + " " + unit + "s"
        whole, _, cents = num.partition(".")
        w = int(whole or 0)
        if w == 0 and cents and int(cents.ljust(2, "0")[:2]) and unit != "euro":
            c = int(cents.ljust(2, "0")[:2])  # $0.18 → eighteen cents
            return cardinal(c) + (" cent" if c == 1 else " cents") if unit == "dollar" else \
                cardinal(c) + " pence"
        out = cardinal(w) + " " + unit + ("" if w == 1 else "s")
        if cents and int(cents.ljust(2, "0")[:2]):
            c = int(cents.ljust(2, "0")[:2])
            out += " and " + cardinal(c) + (" cent" if c == 1 else " cents")
        return out
    t = re.sub(r"([$€£])(\d[\d,]*(?:\.\d+)?)(k|K|M|B|bn|m)?\b", money, t)

    # 10. percent, ordinals, ranges, numbers with units
    t = re.sub(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s?%", lambda m: decimal(m[1]) + " percent", t)
    t = re.sub(r"(?<![\w.])(\d+)(st|nd|rd|th)\b", lambda m: ordinal(int(m[1])), t)
    # ranges: "40-60 s" → forty to sixty seconds; "3-5 days" → three to five days
    t = re.sub(rf"(?<![\w.,])(\d+)\s?[-–]\s?(\d+)\s?({_UNIT_ALT})(?![\w/])",
               lambda m: f"{cardinal(int(m[1]))} to {cardinal(int(m[2]))} {_UNITS[m[3]]}", t)
    t = re.sub(r"(?<![\w.,])(\d+)\s?[-–]\s?(\d+)(?=\s+[a-z]+s\b)",
               lambda m: f"{cardinal(int(m[1]))} to {cardinal(int(m[2]))}", t)
    # plain ranges "pages 10-20": small, ascending (keeps 555-1234 phone-ish runs out)
    t = re.sub(r"(?<![\w.,-])(\d{1,3})[-–](\d{1,4})(?![\w-]|\.\d)",
               lambda m: f"{cardinal(int(m[1]))} to {cardinal(int(m[2]))}"
               if int(m[1]) < int(m[2]) <= 1000 else m[0], t)
    # dimensions: 1024x1024 → ten twenty-four by ten twenty-four
    t = re.sub(r"(?<![\w.])(\d+)\s?[x×]\s?(\d+)(?!\w|\.\d)",
               lambda m: f"{decimal(m[1])} by {decimal(m[2])}", t)

    def unit(m):
        num, u = m[1], m[2]
        words = _UNITS[u]
        n = float(num.replace(",", ""))
        if n == 1 and words in _SINGULAR:
            words = _SINGULAR[words]
        return decimal(num) + " " + words
    t = re.sub(rf"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s?({_UNIT_ALT})(?![\w/])", unit, t)

    # 11. "#3" → number three
    t = re.sub(r"(?<!\w)#(\d+)\b", lambda m: "number " + cardinal(int(m[1])), t)

    # 11b. bare digit lists "1,2,3" → one, two, three (not 1,234 groupings)
    t = re.sub(r"(?<![\w.,])\d{1,2}(?:,\d{1,2})+(?![\w,]|\.\d)",
               lambda m: ", ".join(cardinal(int(x)) for x in m[0].split(",")), t)

    # 12. remaining standalone numbers (not glued to letters: Qwen3.8, x86, H100)
    def num(m):
        s = m[0]
        bare = s.replace(",", "")
        if "," in s and not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
            return s  # "1,2,3" list — leave the commas to separate them
        if re.fullmatch(r"0\d+", bare) or (len(bare.split(".")[0].lstrip("-")) > 15):
            return digits(bare)  # zero-padded ids / huge digit runs: read the digits
        if re.fullmatch(r"(?:19[5-9]\d|20\d\d)", bare):
            after = t[m.end():m.end() + 20]
            if not re.match(r"\s+[a-z]+s\b", after):  # "2048 tokens" stays a quantity
                return year(int(bare))  # table rows / "(2013, 2017)" are years
        return decimal(bare)
    t = re.sub(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?!\w|\.\d|,\d)", num, t)

    # 13. all-caps initialisms the model would try to say as a word: 2-3
    # letters (not a shouted common word), or 4 letters with no vowel
    def caps(m):
        w = m[1]
        if w in _SAY_AS_WORD or w in _CAPS_WORDS:
            return w
        if len(w) == 4 and re.search(r"[AEIOUY]", w):
            return w  # STOP, SEND, DONE — shouted words, not initialisms
        return _spell_letters(w)
    t = re.sub(r"\b([A-Z]{2,4})\b", caps, t)

    t = re.sub(r"\b([Tt]he) the\b", r"\1", t)  # "on the 3rd of March" → on the the third…
    t = re.sub(r"\b(\d+)(ers|ner)\b",  # 49ers → forty-niners, 76ers → seventy-sixers
               lambda m: (lambda w: (w[:-1] if w.endswith("e") else w) + "ers")(cardinal(int(m[1]))), t)
    t = _post(t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"^[\s,.;:]+", "", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()
