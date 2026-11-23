#!/usr/bin/env python3
"""PreToolUse gate for the ACP->OpenAI bridge.

Deny the agent's built-in action tools ONLY for sessions whose client declared
its own tools (marker written by the bridge, keyed by conversationId). Without
client tools the agent keeps its own abilities, so a plain chat request can
still read/list files.

The reason string is fed back to the model: phrase it as the instruction you
want, not as a bare refusal.
"""
import json
import os
import sys
import time

MODE_DIR = os.environ.get("AGY_CWD", "/data/work") + "/.bridge-mode"
REASON = ("Built-in tools are disabled for this request. Do not call them. "
          "Fulfil the request by emitting the caller's function as a "
          "tool_call block in OpenAI function-call shape (arguments as a JSON "
          "string), or answer in plain text if no function is needed.")

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
name = ((payload.get("toolCall") or {}).get("name") or "").strip()
conv = str(payload.get("conversationId") or "")
# conversationId comes from the hook payload unvalidated; reject anything that
# is not a plain identifier so it can never traverse out of MODE_DIR.
import re as _re
if not _re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conv):
    conv = ""
conditional = bool(conv) and os.path.isfile(os.path.join(MODE_DIR, conv + ".json"))
decision = "deny" if conditional else "allow"
try:
    with open("/var/log/agy-bridge-hooks.log", "a") as fh:
        fh.write(f"{time.strftime('%H:%M:%S')} {decision} {name} conv={conv[:8]}\n")
except Exception:
    pass
out = {"decision": decision}
if conditional:
    out["reason"] = REASON
print(json.dumps(out))
