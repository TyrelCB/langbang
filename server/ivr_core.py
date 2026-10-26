"""IVR turn logic shared by LangBang's IVR (server/ivr.py) and the
standalone IVR node (server/ivr_node.py). No langchain, no FastAPI: the node
imports this on a box that has neither.

  Sentences       cut streamed model text into speakable pieces
  window()        the slice of a call's history sent to the model
  THINKING_OFF    request switches that turn model "thinking" off, per backend
"""
import re

import openai

PINNED_MSGS = 4  # a call's first two exchanges (who's calling, and why) always stay


def window(msgs: list, cfg: dict) -> list:
    """The call's history sent to the model: all of it while it fits
    ivr.max_history messages, then the opening exchanges (PINNED_MSGS — the
    caller's name and reason usually come first) plus the most recent ones.
    Spoken exchanges are short (~25 tokens a message), so the default 160
    holds a 30+ minute call in ~4k tokens. Returns the trimmed list; callers
    keep it as the call's new history.

    Trimmed in chunks, not one exchange per turn: dropping the oldest
    unpinned exchange changes the prompt right after the opening, so the
    backend's prefix cache misses and re-prefills the whole history — every
    turn (measured on the Mac mini: turns 81+ of a call went 1.3 s → 6.5 s).
    Cutting a quarter of the window at once pays that once per ~20 turns."""
    cap = max(PINNED_MSGS + 2, int(cfg.get("max_history") or 160))
    cap -= cap % 2  # whole exchanges: the kept tail must start on a caller turn
    if len(msgs) > cap:
        slack = max(2, cap // 4)
        slack -= slack % 2
        keep = max(2, cap - slack - PINNED_MSGS)
        msgs = msgs[:PINNED_MSGS] + msgs[-keep:]
    return msgs


# thinking OFF whatever the thread/CONFIG thinking setting says (Qwen
# hybrids think by default when the switch isn't sent — minutes of dead air
# on a phone line). Backends disagree on the switch: sglang/vLLM read
# chat_template_kwargs; Ollama's /v1 ignores that (and its own "think":false
# once tools are in the request) and honours only reasoning_effort "none",
# which vLLM 400s on. So try both, then each alone-ish, then nothing, and
# remember per backend what it accepted — the 400 round trip is paid once.
_TPL_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
THINKING_OFF = ({**_TPL_OFF, "reasoning_effort": "none"}, _TPL_OFF, None)


def variants(accepts: dict, base_url: str) -> list:
    """(index, extra_body) to try for this backend, from the one it last accepted."""
    i = accepts.get(base_url, 0)
    return [(j, THINKING_OFF[j]) for j in range(i, len(THINKING_OFF))]


def rejected(e: Exception, eb: dict | None) -> bool:
    """A 400 about one of the thinking switches (→ try the next variant)."""
    return eb is not None and isinstance(e, openai.BadRequestError) and \
        any(k in str(e) for k in eb)


class Sentences:
    """Cut streamed model text into speakable sentences. A sentence ends at
    . ! ? (plus closing quotes/brackets) followed by whitespace, or at a
    newline; "3.5" and "e.g." mid-word never split because the next char
    isn't whitespace. Fragments shorter than `min_chars` wait for more text
    so Pocket isn't handed "Sure." and "Okay." as separate chunks.

    Until the first piece is out, a clause ending in , ; : also counts once
    it is `first_clause` chars long: the caller hears audio as soon as there
    is a natural pause to cut at, not after a long first sentence."""
    _END = re.compile(r"[.!?]+[\"')\]]*\s+|\n+")
    _CLAUSE = re.compile(r"[.!?,;:]+[\"')\]]*\s+|\n+")
    _ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|etc|vs|Mr|Mrs|Ms|Dr|St|Jr|Sr|Inc|Co|No)|\b[A-Z])\.$")

    def __init__(self, min_chars: int = 12, first_clause: int = 40):
        self.buf, self.min, self.first, self.started = "", min_chars, first_clause, False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out, start = [], 0
        if not self.started:
            for m in self._CLAUSE.finditer(self.buf):
                piece = self.buf[:m.end()].strip()
                if self._ABBREV.search(piece):
                    continue
                ends_sentence = bool(self._END.fullmatch(m.group()))
                if len(piece) >= (self.min if ends_sentence else self.first):
                    out.append(piece)
                    start = m.end()
                    self.started = True
                    break
        for m in self._END.finditer(self.buf, start):
            piece = self.buf[start:m.end()].strip()
            if len(piece) >= self.min and not self._ABBREV.search(piece):
                out.append(piece)
                start = m.end()
        self.buf = self.buf[start:]
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        self.started = True
        return [rest] if rest else []
