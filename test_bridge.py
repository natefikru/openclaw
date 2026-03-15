#!/opt/homebrew/bin/python3.14
"""
Generate a random question via AI and inject it into iMessage as if you sent it.
The running bridge daemon detects it and responds automatically.

Usage:
  python3 test_bridge.py
"""

import json
import os
import subprocess
import sys
import urllib.request

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
HANDLE = "+17637322236"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def generate_question() -> str:
    payload = json.dumps({
        "model": "gpt-4o",
        "messages": [{"role": "user", "content":
            "Give me one random, simple factual question a person might ask a chatbot. "
            "Examples: 'what is the capital of Japan', 'how many planets are in the solar system', "
            "'who wrote hamlet'. Return only the question text, no punctuation, all lowercase."}],
        "max_tokens": 30,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    question = data["choices"][0]["message"]["content"].strip().rstrip("?").lower()
    return f"claw {question}"


def send_as_user(text: str) -> None:
    escaped = _escape(text)
    escaped_handle = _escape(HANDLE)
    script = (
        'tell application "Messages"\n'
        '  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{escaped_handle}" of targetService\n'
        f'  send "{escaped}" to targetBuddy\n'
        'end tell'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"[test] osascript error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print(f"[test] injected: {text!r}")
    print("[test] bridge will respond shortly")


if __name__ == "__main__":
    if not OPENAI_API_KEY:
        print("[test] error: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    print("[test] generating question...")
    text = generate_question()
    send_as_user(text)
