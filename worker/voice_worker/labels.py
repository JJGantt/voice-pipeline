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
    "Someone asked their voice assistant the question below. Write the label for the answer in an inbox "
    "list on a watch: THE QUESTION THEY ASKED, compressed to its key words. Always a question ending in "
    "'?', 2 to 5 words, never more. Label what they ASKED, never what the answer went on to cover. The "
    "answer is given ONLY to turn words like 'it', 'that movie', 'what about Saturday' into the thing "
    "they refer to. Examples, question -> label: "
    "'What else is there on this movie, the Resident Evil movie, worth watching?' -> 'More on Resident Evil?'; "
    "'Is that movie I just saved, what is that available on?' (answer about Chemical Hearts) -> 'Where is Chemical Hearts streaming?'; "
    "'What catalog are you talking about?' -> 'What catalog?'; "
    "'Yeah I don't agree with him though, is there another reviewer, did YMS review it?' (answer about Resident Evil) -> 'Did YMS review Resident Evil?'; "
    "'Why does Safari keep closing itself on my Mac?' -> 'Why does Safari close?'. "
    "Output only the label, no quotes."
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
