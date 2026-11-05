"""The inbox label: a flagged answer's row is named by the question it answers, cut to a few words.

Written by Haiku AFTER the reply is stored, never by the model answering: asked to write it inside its
own marker, that model drifts into leaving it off, and a bare marker in the replayed day is copied by
every later turn. Here the label is a pure function of the question and the answer, so it cannot drift.

The reply row is written first and labelled a few seconds later (`label_later`), so the answer never
waits on the label; until it lands the inbox shows the question's own words.
"""
import shutil
import subprocess
import threading

from .config import CLAUDE_BIN
from .db import Supabase
from .logs import log, log_error

LABEL_MAX = 40
LABEL_TIMEOUT_S = 45

PROMPT = (
    "Someone asked their voice assistant the question below and got the answer below. Write the label "
    "for this answer in an inbox list on a watch. The label is the QUESTION, rewritten as short as it "
    "can be while still saying what was asked: always a question ending in '?', at most 5 words, ideally 3, understandable "
    "with no other context (resolve 'it', 'that one', 'what about Saturday' from the answer). "
    "Examples: \"Who is Paul Rust?\", \"Surf good Sunday?\", \"Resident Evil streaming?\", \"When does "
    "Costco close?\". Output only the label, no quotes."
)


def make_label(question: str, answer: str) -> str | None:
    """Haiku's label for one question and its answer, or None when it could not be had."""
    body = f"{PROMPT}\n\n--- QUESTION ---\n{(question or '').strip()[:4000]}\n\n--- ANSWER ---\n{(answer or '').strip()[:6000]}"
    try:
        # No tools and no MCP servers: the answer can quote anything (a web page, a session's logs), and
        # labelling it needs nothing but the text.
        out = subprocess.run([shutil.which(CLAUDE_BIN) or CLAUDE_BIN, "-p", "--model", "haiku", "--tools", "",
                              "--strict-mcp-config"], input=body, capture_output=True, text=True,
                             timeout=LABEL_TIMEOUT_S)
    except Exception as e:
        log_error(f"inbox label: haiku call failed: {e}")
        return None
    line = " ".join((out.stdout or "").split()).strip().strip("\"'")
    if out.returncode != 0 or not line:
        log_error(f"inbox label: haiku returned nothing (rc={out.returncode}): {(out.stderr or '')[:300]}")
        return None
    return line[:LABEL_MAX]


def label_later(db: Supabase, request_id: str, question: str, answer: str) -> None:
    """Label the flagged reply written for `request_id`, in the background."""
    def run():
        label = make_label(question, answer)
        if not label:
            return
        rows = db.patch("voice_messages", f"request_id=eq.{request_id}&role=eq.assistant&needs_read=is.true",
                        {"label": label}, {"Prefer": "return=representation"})
        if not rows:   # the call failed, or no flagged reply matched: either way the row is unlabelled
            log_error(f"inbox label: could not write {label!r} for request {request_id}")
            return
        log(f"inbox label for {request_id}: {label!r}")
    threading.Thread(target=run, daemon=True).start()
