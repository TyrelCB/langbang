"""Regression cases for server/ttsnorm.py — run: .venv/bin/python tools/ttsnorm_check.py

Expected strings are what Pocket TTS should be handed. The first three are
the 2026-10-06 Pocket/Higgs/Qwen comparison prompts; their normalized forms
round-tripped through Pocket (javert) → faster-whisper small intact, while
the raw forms came back garbled (or, for the ISO one, cut off after 4 s)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.ttsnorm import normalize  # noqa: E402
from server.voice import speakable  # noqa: E402

CASES = [
    ("Refreshed for Thu, Oct 1, 2026 (13:11 UTC) — Week 4 kicks off tonight.",
     "Refreshed for Thursday, October first, twenty twenty-six, thirteen eleven U T C, Week four kicks off tonight."),
    ("The next check-in is Friday, November 13, 2026 at 9:05 a.m. Eastern.",
     "The next check-in is Friday, November thirteenth, twenty twenty-six at nine oh five A M Eastern."),
    ("Archive window: 2026-12-31 23:59 UTC. Reopen at 00:01 on January 1, 2027.",
     "Archive window: December thirty-first, twenty twenty-six, at twenty-three fifty-nine U T C. Reopen at zero zero oh one on January first, twenty twenty-seven."),
    ("Deployed 2026-10-04T12:28:49Z.", "Deployed October fourth, twenty twenty-six, at twelve twenty-eight and forty-nine seconds U T C."),
    ("Costs $1,234.56, up 12.5% since 2024.", "Costs one thousand two hundred thirty-four dollars and fifty-six cents, up twelve point five percent since twenty twenty-four."),
    ("Ships 10/1/2026, item #3.", "Ships October first, twenty twenty-six, item number three."),
    ("Meet on the 3rd of March, 2027 at 2pm PST.", "Meet on the third of March, twenty twenty-seven at two P M P S T."),
    ("Sun Mar 7 at 14:30.", "Sunday March seventh at fourteen thirty."),
    ("40-60 s per image at 1024x1024.", "forty to sixty seconds per image at one thousand twenty-four by one thousand twenty-four."),
    ("113.3k vs 228k, about 2.05x low.", "one hundred thirteen point three thousand versus two hundred twenty-eight thousand, about two point zero five times low."),
    ("128 GB; 1 GB; 1 hr; -5°C.", "one hundred twenty-eight gigabytes; one gigabyte; one hour; minus five degrees Celsius."),
    ("pages 10-20, score 3-1.", "pages ten to twenty, score three-one."),
    ("call 555-1234 or (303) 555-0199.", "call five five five, one two three four or three zero three, five five five, zero one nine nine."),
    ("ids 007 and 1,2,3; it returned 200.", "ids zero zero seven and one, two, three; it returned two hundred."),
    # left alone: glued to letters, versions, shouted words, spelled-anyway initialisms
    ("Qwen3.8 on a GB10, v1.2.3, H100.", "Qwen3.8 on a GB10, v1.2.3, H100."),
    ("Use the STOP button, then NEW CHAT. The API runs on the GPU.", "Use the STOP button, then NEW CHAT. The API runs on the GPU."),
    ("The Sun was out; I sat on Mar Vista.", "The Sun was out; I sat on Mar Vista."),
    # 2026-10-06: emoji + "·"-joined scores sent Pocket off the rails (one 300-token "sentence")
    ("🏈 NFL — Week 4 final. Colts 30 Commanders 13 (London) · 49ers 24 Broncos 14 · Panthers 32 Lions 26.",
     "N F L, Week four final. Colts thirty, Commanders thirteen, London. forty-niners twenty-four, Broncos fourteen. Panthers thirty-two, Lions twenty-six."),
    ("✅ Done • 3 items ⚠️ check | next", "Done. three items, check. next"),
    # 2026-10-06: TNA fund write-up — table rows, signs, finance shorthand
    ("2021     +33%     +15%     −12pp", "twenty twenty-one, plus thirty-three percent, plus fifteen percent, minus twelve percentage points."),
    ("52-week range     $37.03 – $77.24", "fifty-two-week range, thirty-seven dollars and three cents to seventy-seven dollars and twenty-four cents."),
    ("Distribution ~$0.18 ttm (0.3%), up 4.5%/yr since inception (17.8y).",
     "Distribution about eighteen cents trailing twelve months, zero point three percent, up four point five percent per year since inception, seventeen point eight years."),
    ("captured 2010–12 and 2019–21; splits (2013, 2017); inception Nov 5 2008",
     "captured twenty ten to twenty twelve and twenty nineteen to twenty twenty-one; splits, twenty thirteen, twenty seventeen, inception November fifth, two thousand eight"),
    ("| Year | TNA | IWM |\n|---|---|---|\n| 2024 | +8% | +11% |", "Year, T N A, I W M. twenty twenty-four, plus eight percent, plus eleven percent."),
    ("It ran 24/7 w/ 99.9% uptime ±0.1%, ≥ 3 nodes.", "It ran twenty-four seven with ninety-nine point nine percent uptime plus or minus zero point one percent, at least three nodes."),
]

bad = 0
for src, want in CASES:
    got = normalize(speakable(src)) if "\n" in src or "  " in src else normalize(src)
    if got != want:
        bad += 1
        print(f"FAIL {src!r}\n  want {want!r}\n  got  {got!r}")
print(f"{len(CASES) - bad}/{len(CASES)} ok")
sys.exit(1 if bad else 0)
