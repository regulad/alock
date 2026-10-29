"""Entry point for the hooks: `hook.py SessionStart` or `hook.py PreToolUse`,
with the event's JSON on stdin and the hook's JSON result on stdout."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alock import hooks  # noqa: E402

HANDLERS = {"SessionStart": hooks.session_start, "PreToolUse": hooks.pre_tool_use}

if __name__ == "__main__":
    event = json.load(sys.stdin)
    result = HANDLERS[sys.argv[1]](event)
    if result:
        json.dump(result, sys.stdout)
