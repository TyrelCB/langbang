"""Layer 1 — the loop's decisions, no audio, no server (seconds).

Every case is a real utterance from a real call; the call id says where it
came from. A fix for a misfire adds its case here, so it can't come back.
Run through run_suite.py (any test_* function here is collected).
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RIG = os.path.dirname(HERE)
sys.path.insert(0, RIG)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LOOP = _load("ivr_loop", os.path.join(RIG, "ivr_loop.py"))
ANSWER = _load("ivr_answer", os.path.join(RIG, "ivr_answer.py"))


# ---- echo filter -----------------------------------------------------------

ECHO = [
    # (caller transcript, our reply, is echo?, source)
    ("Yeah, tell me about your background and skills.",
     "I apologize if there was a delay. Do you have questions about my background or skills?",
     False, "9e95a05395cc 04:28:02 — real question, was dropped as echo"),
    ("Tell me about your background skills.",
     "I apologize if there was a delay. Do you have questions about my background or skills?",
     False, "9e95a05395cc 04:28:13 — the repeat, dropped again"),
    ("do you have experience with switching and ethernet?",
     "Do you have any questions about my background?",
     False, "79772a3889d1 11:24:37 — only function words shared"),
    ("Do you have questions about my background or skills",
     "I apologize if there was a delay. Do you have questions about my background or skills?",
     True, "our own sentence coming back"),
    ("I know BGP OSPF and EVPN",
     "I know BGP, OSPF, and EVPN-VXLAN for enterprise routing.",
     True, "hyphenated reply word comes back split"),
    ("What's your experience with BGP?",
     "I know BGP, OSPF, and EVPN-VXLAN.",
     False, "one shared word is not an echo"),
]


def test_echo_timing():
    ours = ["I have nineteen years of network and infrastructure experience. "
            "I build generative artificial intelligence systems end to end."]
    quote = "You build generative artificial intelligence end to end now?"
    LOOP.play.ended = 1000.0
    # the caller quoting us back 1.5 s after we stopped: a question, not echo
    # (dropped in the c8d18315086e replay when the window was 1.5 s)
    assert not LOOP.is_echo(quote, 1001.5, ours)
    # the same words starting while we were still talking: echo
    assert LOOP.is_echo(quote, 999.6, ours)
    assert LOOP.is_echo(quote, 1000.3, ours)


def test_echo_filter():
    bad = [f"{src}: same_words={not want}, want {want} — {a!r}"
           for a, b, want, src in ECHO if LOOP.same_words(a, b) != want]
    assert not bad, "\n".join(bad)


# ---- Google Voice's own voice on the line ------------------------------------

GV = [
    ("Plus one, seven, five.", True, "28a38a02a121 — GV caller-ID readout answered as the caller"),
    ("Call from Jenny. To accept, press 1.", True, "GV screening prompt"),
    ("Plus one more thing I wanted to ask about the role.", False, "a caller who says 'plus one'"),
    ("That's one, seven, five.", False, "STT variant — not GV-shaped, left to the handoff fix"),
    ("Hi, this is Sam from Acme Staffing.", False, "a caller intro"),
]


def test_gv_screen_filter():
    bad = [f"{src}: matched={bool(LOOP.GV_SCREEN_RE.search(t))}, want {want} — {t!r}"
           for t, want, src in GV if bool(LOOP.GV_SCREEN_RE.search(t)) != want]
    assert not bad, "\n".join(bad)


# ---- goodbye -----------------------------------------------------------------

BYE = [
    ("Okay, goodbye.", True, "c8d18315086e"),
    ("Goodbye.", True, "9e95a05395cc"),
    ("Okay, thanks. Bye.", True, "7a425e53a2c4 — kept going after it"),
    ("Before you say goodbye, what's your rate expectation for this contract role?", False,
     "mid-sentence goodbye must not end the call"),
    ("It was great talking to you. Do you have any feedback for me?", False, "14c7e7164297"),
]


def test_goodbye():
    bad = [f"{src}: says_goodbye={not want} — {t!r}" for t, want, src in BYE if LOOP.says_goodbye(t) != want]
    assert not bad, "\n".join(bad)


# ---- accept handoff (ivr_answer) ---------------------------------------------

def test_handoff_waits_for_full_press_schedule():
    live = {"answer": False, "panel": True, "live": True}
    # 28a38a02a121: live at press #1 — handing off there answered GV's menu
    for presses in range(len(ANSWER.PRESS_AT)):
        assert not ANSWER.handoff_ready(live, presses), f"handed off after {presses} presses"
    assert ANSWER.handoff_ready(live, len(ANSWER.PRESS_AT))
    # no panel = the call went away: never hand off
    assert not ANSWER.handoff_ready({"answer": False, "panel": False, "live": False}, 4)


# ---- greeting wait (GV connect tone) ------------------------------------------

class FakeDownlink:
    """Frames at FRAME_S each: dB per frame from a list of (seconds, dB) spans."""

    def __init__(self, spans):
        self.dbs = [db for secs, db in spans for _ in range(round(secs / LOOP.FRAME_S))]
        self.i = 0

    def frame(self, timeout):
        if self.i >= len(self.dbs):
            return (b"", -90.0)       # dead-quiet line forever after
        self.i += 1
        return (b"", self.dbs[self.i - 1])

    def threshold(self, over=0.0):
        return -55.0 + over


def _quiet(spans):
    return round(LOOP.wait_line_quiet(FakeDownlink(spans)), 2)


def test_greet_wait_no_tone():
    # 17 of 23 calls: no tone at all — greet at GREET_NO_TONE_S, not 2.5 s
    assert LOOP.GREET_NO_TONE_S <= 1.0, f"GREET_NO_TONE_S={LOOP.GREET_NO_TONE_S}: callers sat through it on most calls"
    assert _quiet([]) == LOOP.GREET_NO_TONE_S


def test_greet_wait_tone_then_quiet():
    # tone heard: greet GREET_QUIET_S after it ends, never over it
    assert _quiet([(0.2, -90), (0.4, -30)]) == round(0.6 + LOOP.GREET_QUIET_S, 2)
    # tone that starts late but inside the no-tone window still holds the greeting
    assert _quiet([(0.8, -90), (0.6, -30)]) == round(1.4 + LOOP.GREET_QUIET_S, 2)


def test_greet_wait_capped():
    assert _quiet([(30, -30)]) == LOOP.GREET_MAX_S


# ---- end of utterance --------------------------------------------------------

def test_end_of_speech_not_shortened():
    # 0.8 (10-16) and 1.0 (10-26) both cut callers off mid-sentence:
    # "Okay, let's...", "It's breaking up a little bit. Is that..."
    assert LOOP.END_S >= 1.2, f"END_S={LOOP.END_S}"


# ---- reply sentence splitting (node) -----------------------------------------
# ivr_core lives in the node and imports its deps (openai): run it in the
# node's venv

NODE = os.path.expanduser("~/projects/ivr-node")
SPLIT_CHECK = r"""
from server.ivr_core import Sentences
s = Sentences()
assert s.feed("Yes. ") == [] and s.feed("I know BGP and OSPF well. More") == \
    ["Yes. I know BGP and OSPF well."], "short first sentence should wait for min_chars"
s = Sentences()
out = s.feed("I use tools, e.g. Python and Ansible, for automation every day. Next")
assert out == ["I use tools, e.g. Python and Ansible, for automation every day."], out
s = Sentences()
assert s.feed("The rate is 3.5 percent. ") == ["The rate is 3.5 percent."]
"""


def test_sentence_splitter():
    import subprocess
    r = subprocess.run([os.path.join(NODE, ".venv", "bin", "python"), "-c", SPLIT_CHECK],
                       cwd=NODE, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1]
