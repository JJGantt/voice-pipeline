#!/usr/bin/env python3
"""Stand-in for the `claude` CLI: records its argv to $FAKE_CLAUDE_LOG and answers one stream-json turn."""
import json
import os
import sys

with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake-session"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Agent did it."}]}}), flush=True)
print(json.dumps({"type": "result", "result": "Agent did it.", "session_id": "fake-session"}), flush=True)
