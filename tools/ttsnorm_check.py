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
    # 2026-10-08: network-engineering report (switch API probe): interfaces,
    # IPs, status codes, XML, namespaced / hyphen-chain identifiers, VLANs
    ('openconfig-interfaces gives per-port counters (Gi1/0/1: 3.06 MB in / 14.78 MB out, 0 errors).',
     'open config-interfaces gives per-port counters, gig one slash zero slash one: three point zero six megabytes in, fourteen point seven eight megabytes out, zero errors.'),
    ('VLAN 777 LANGBANG_DEMO (201 Created) plus SVI Vlan777 / 10.77.77.1, deleted in reverse order (204/204).',
     'vee-lan seven seventy-seven langbang demo, two oh one, Created, plus S V I vee-lan seven seventy-seven, ten dot seventy-seven dot seventy-seven dot one, deleted in reverse order, two oh four, two oh four.'),
    ('<filter select="/native/hostname"/> returns a silently empty <data/>.',
     'filter, select hostname, returns a silently empty data.'),
    ('Cisco-IOS-XE-acl-oper returns 7 ACLs; ios_actions RPCs; lldp, utd-oper, pnp: all 404 at container level.',
     'Cisco I O S X E A C L opper returns seven A C Ls; I O S actions R P Cs; L L D P, U T D opper, P N P: all four oh four at container level.'),
    ('ping 8.8.8.8 hangs SSH; NTP (10.17.251.250) stuck at stratum 16.',
     'ping eight dot eight dot eight dot eight hangs S S H; N T P, ten dot seventeen dot two fifty-one dot two fifty, stuck at stratum sixteen.'),
    ('hostname c9000v-terraform-netconf (domain mohamed.local), Loopback99 / 99.99.99.99, LAB_TEST_POLICY on Gi1/0/3.',
     'hostname c9000v terraform net conf, domain mohamed dot local, loopback ninety-nine, ninety-nine dot ninety-nine dot ninety-nine dot ninety-nine, lab test policy on gig one slash zero slash three.'),
    ('an ISE/802.1X lab; no ip domain lookup; NETCONF XPath over RESTCONF.',
     'an I S E eight oh two dot one X lab; no I P domain lookup; net conf X path over rest conf.'),
    ('Set Te1/1/4.100 to 192.168.1.0/24; Error 404 on /api/runs; hit localhost:8123.',
     'Set ten gig one slash one slash four dot one hundred to one ninety-two dot one sixty-eight dot one dot zero slash twenty-four; Error four oh four on runs; hit localhost port eight thousand one hundred twenty-three.'),
]

bad = 0
for src, want in CASES:
    got = normalize(speakable(src)) if "\n" in src or "  " in src else normalize(src)
    if got != want:
        bad += 1
        print(f"FAIL {src!r}\n  want {want!r}\n  got  {got!r}")
print(f"{len(CASES) - bad}/{len(CASES)} ok")
sys.exit(1 if bad else 0)
