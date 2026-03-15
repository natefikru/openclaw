#!/opt/homebrew/bin/python3.14
"""
OpenClaw iMessage Bridge
Polls ~/Library/Messages/chat.db for new messages and responds via
AppleScript when the trigger word is detected.

Usage:
  export OPENAI_API_KEY="sk-..."
  python3 imessage_bridge.py

Helper:
  python3 imessage_bridge.py --list-chats
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from chat_memory import ChatMemory

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Monitored chats: list of dicts.
# guid               -- from chat.db
# handle             -- phone/email for 1-on-1 sends; ignored for group chats (osascript uses guid)
# announce           -- if True, send the startup message when the bridge launches; default False
# context            -- per-chat description prepended to every agent message
# auto_summarize_urls -- if True, bare URLs with no trigger word get auto-fetched and summarized
MONITORED_CHATS = [
    {
        "guid": "any;-;+16128670966",
        "handle": "+16128670966",
        "announce": False,
        "context": "1-on-1 with Chad Green, close friend from Minneapolis now in NYC. Topics: music, events, nightlife, film photography, creative projects, video editing. Vibe is casual and real.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;+;chat643476086489734015",
        "handle": "chat643476086489734015",
        "announce": False,
        "context": "Group chat called 'Artificial Nigga Intelligence' -- close friend group with shared interest in tech, AI, music, and culture. Humorous tone, very casual.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;+;chat386131053609522809",
        "handle": "chat386131053609522809",
        "announce": False,
        "context": "Group chat called 'Brethren' -- close friend group. Casual vibes.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;-;+17637322236",
        "handle": "+17637322236",
        "announce": False,
        "context": "Personal test chat -- Nate testing the bridge.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;+;chat684554202854870884",
        "handle": "chat684554202854870884",
        "announce": False,
        "context": "Group chat called 'Superself' -- close group with Hassan and Iris. Casual vibes.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;-;+16467505928",
        "handle": "+16467505928",
        "announce": False,
        "context": "1-on-1 with Hassan, close friend in NYC.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;-;+16517577453",
        "handle": "+16517577453",
        "announce": False,
        "context": "1-on-1 with Sam, close friend.",
        "auto_summarize_urls": True,
    },
    {
        "guid": "any;-;+447397867986",
        "handle": "+447397867986",
        "announce": False,
        "context": "1-on-1 with Iris, close friend based in London.",
        "auto_summarize_urls": True,
    },
]

# Seconds between polls of chat.db
POLL_INTERVAL = 2

# Trigger word (case-insensitive)
TRIGGER_WORD = "claw"

# Prefix prepended to every outbound message
BOT_PREFIX = "[OpenClaw]"

# Cooldown in seconds after sending a reply before resuming polls
REPLY_COOLDOWN = 3

# Default number of recent messages passed to the model as context.
# Can be overridden per-request by including a range command in the trigger message:
#   "claw last 50 messages ..."
#   "claw last 2 hours ..."
#   "claw last 24h ..."
#   "claw yesterday ..."
#   "claw this week ..."
CONTEXT_MESSAGES = 25

# OpenAI model to use
OPENAI_MODEL = "gpt-5-mini"

# OpenAI model for web search queries
OPENAI_SEARCH_MODEL = "gpt-4o-search-preview"

# OpenAI API key -- set via environment variable (do not hardcode here)
# export OPENAI_API_KEY="sk-..."
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# OpenClaw integration (Phase 2)
OPENCLAW_DIR = os.path.expanduser("~/go/src/din/natefikru/openclaw")
NODE_BIN = "node"
OPENCLAW_MODEL = "openai/gpt-5"

# System prompt sent to the model on every request
_SYSTEM_PROMPT_BASE = (
    "You are OpenClaw, a helpful AI assistant embedded in a private iMessage conversation. "
    "Someone mentioned 'claw' to get your attention. "
    "Answer the most recent question or request directly. "
    "Never end your reply with a follow-up question or offer to do more -- just answer and stop. "
    "Lines prefixed with [OpenClaw] in the conversation history are your own prior replies -- use them to avoid repeating yourself. "
    "Use the conversation history only if it is directly relevant to what was just asked -- do not reference or summarize previous topics unprompted. "
    "Do not mention that you are an AI unless directly asked. "
    "Keep replies concise -- 1-2 short paragraphs maximum, under 80 words. This is a chat, not an essay. "
    "Hard limit: never exceed 80 words total. For news, weather, or search results, pick 2-3 highlights only -- no bullet lists, no multi-section breakdowns, no forecasts. One short paragraph. "
    "Never use emojis under any circumstances. "
    "Never ask a follow-up question. Never offer to do more or suggest next steps. Answer, then stop. "
    "Never draft or send messages to third parties on someone's behalf -- if asked to text or message someone, decline. "
    "When summarizing articles or web pages, do not include citation links or source URLs in your response -- just the plain text summary."
)

# Load IDENTITY.md from the same directory as this script and append to the system prompt.
_IDENTITY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "IDENTITY.md")
try:
    with open(_IDENTITY_PATH) as _f:
        _identity = _f.read().strip()
    SYSTEM_PROMPT = f"{_SYSTEM_PROMPT_BASE}\n\nPersona:\n{_identity}"
except FileNotFoundError:
    _identity = ""
    SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE

# Path to the iMessage SQLite database
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# File that persists the last processed ROWID across restarts
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_rowid")

# Memory storage directory
MEMORY_DIR = os.path.expanduser("~/.openclaw/memory")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentResponse
# ---------------------------------------------------------------------------

AgentResponse = namedtuple("AgentResponse", ["text", "media_urls"])


# ---------------------------------------------------------------------------
# Phase 2: OpenClaw warmup state
# ---------------------------------------------------------------------------

_openclaw_warmed_up = False


def _openclaw_env() -> dict:
    """Build a subprocess env with pnpm and standard system tools on PATH.

    The process may inherit a stripped PATH (e.g. from Claude Code) that lacks
    /usr/bin. We always ensure /usr/bin:/bin are present, then prepend the bin
    dir of the newest asdf nodejs install that ships pnpm so the OpenClaw build
    script can find it.
    """
    env = {**os.environ}
    existing = env.get("PATH", "")
    extra: list[str] = []

    # Always include standard system tool dirs (for sed, dirname, uname, etc.)
    for sysdir in ("/usr/local/bin", "/usr/bin", "/bin"):
        if sysdir not in existing.split(os.pathsep):
            extra.append(sysdir)

    # Find pnpm from asdf nodejs installs (newest version first)
    asdf_nodejs = os.path.expanduser("~/.asdf/installs/nodejs")
    if os.path.isdir(asdf_nodejs):
        for ver in sorted(os.listdir(asdf_nodejs), reverse=True):
            bin_dir = os.path.join(asdf_nodejs, ver, "bin")
            if os.path.isfile(os.path.join(bin_dir, "pnpm")):
                extra.insert(0, bin_dir)
                break

    if extra:
        env["PATH"] = os.pathsep.join(extra) + os.pathsep + existing
    return env


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_path(guid: str) -> str:
    safe = guid.replace(";", "_").replace("+", "").replace(" ", "_")
    return os.path.join(os.path.dirname(STATE_FILE), f".last_rowid_{safe}")


def load_last_rowid(guid: str) -> int:
    try:
        with open(_state_path(guid)) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_last_rowid(guid: str, rowid: int) -> None:
    with open(_state_path(guid), "w") as f:
        f.write(str(rowid))


# ---------------------------------------------------------------------------
# attributedBody text extraction
# ---------------------------------------------------------------------------

_ATTRIBUTED_BODY_SKIP = {
    b"streamtyped", b"NSAttributedString", b"NSMutableAttributedString",
    b"NSObject", b"NSString", b"NSMutableString", b"NSDictionary",
    b"NSMutableDictionary", b"NSArray", b"NSNumber", b"NSValue",
    b"__kIMMessagePartAttributeName", b"__kIMBaseWritingDirectionAttributeName",
    b"__kIMFilenameAttributeName", b"__kIMMessagePartAttributeName",
    b"root", b"$null",
}
_ATTRIBUTED_PRINTABLE_RE = re.compile(rb'[\x20-\x7e]{3,}')


def _text_from_attributed_body(blob) -> str:
    """Extract plain text from iMessage attributedBody binary blob."""
    try:
        raw = bytes(blob)
        candidates = _ATTRIBUTED_PRINTABLE_RE.findall(raw)
        for c in candidates:
            if c not in _ATTRIBUTED_BODY_SKIP and not c.startswith(b"NS") and not c.startswith(b"__k"):
                text = c.decode("utf-8", errors="replace").strip()
                # Strip leading NSArchiver artifact bytes (e.g. "+=" or "+#") that
                # precede the real message text in the binary blob.
                text = re.sub(r'^[^a-zA-Z0-9\[\(\"\'\-\!]+', '', text)
                if text:
                    return text
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    uri = f"file:{CHAT_DB}?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def get_new_messages(conn: sqlite3.Connection, guid: str, last_rowid: int) -> list[dict]:
    """Return all messages (including own) in the given chat newer than last_rowid."""
    query = """
        SELECT
            m.ROWID,
            m.text,
            m.attributedBody,
            m.is_from_me,
            m.date,
            COALESCE(h.id, 'unknown') AS sender
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE c.guid = ?
          AND m.ROWID > ?
          AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
        ORDER BY m.ROWID ASC
    """
    cur = conn.execute(query, (guid, last_rowid))
    cols = [d[0] for d in cur.description]
    rows = []
    for row in cur.fetchall():
        msg = dict(zip(cols, row))
        # Skip own messages with no plain text field -- these are bot replies stored
        # only in attributedBody. Partial extraction can produce "claw..." fragments
        # that falsely pass should_respond and cause duplicate responses.
        if msg.get("is_from_me") and not msg.get("text"):
            continue
        if not msg.get("text") and msg.get("attributedBody"):
            msg["text"] = _text_from_attributed_body(msg["attributedBody"])
        msg.pop("attributedBody", None)
        if msg.get("text"):
            rows.append(msg)
    return rows


def get_context_window(
    conn: sqlite3.Connection,
    guid: str,
    n: int = CONTEXT_MESSAGES,
    since_apple_ts: int | None = None,
) -> list[dict]:
    """Return recent messages in chronological order for a given chat.

    If since_apple_ts is provided, returns all messages after that timestamp
    (up to 500 as a safety cap). Otherwise returns the last n messages.
    """
    if since_apple_ts is not None:
        query = """
            SELECT m.text, m.is_from_me, COALESCE(h.id, 'unknown') AS sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE c.guid = ?
              AND m.text IS NOT NULL
              AND m.date >= ?
            ORDER BY m.ROWID ASC
            LIMIT 500
        """
        cur = conn.execute(query, (guid, since_apple_ts))
    else:
        query = """
            SELECT m.text, m.is_from_me, COALESCE(h.id, 'unknown') AS sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE c.guid = ?
              AND m.text IS NOT NULL
            ORDER BY m.ROWID DESC
            LIMIT ?
        """
        cur = conn.execute(query, (guid, n))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return list(reversed(rows)) if since_apple_ts is None else rows


def get_current_tip(conn: sqlite3.Connection, guid: str) -> int:
    cur = conn.execute(
        """
        SELECT COALESCE(MAX(m.ROWID), 0)
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE c.guid = ?
        """,
        (guid,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def list_chats(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT guid, chat_identifier, COALESCE(display_name, '') FROM chat ORDER BY ROWID"
    )
    rows = cur.fetchall()
    if not rows:
        print("No chats found in chat.db.")
        return
    for guid, identifier, name in rows:
        print(f"  GUID:       {guid}")
        print(f"  Identifier: {identifier}")
        print(f"  Name:       {name or '(unnamed)'}")
        print()


# ---------------------------------------------------------------------------
# Context range parsing
# ---------------------------------------------------------------------------

# Apple epoch offset in seconds (Jan 1 2001 UTC)
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_APPLE_EPOCH_UNIX = 978307200  # same as above in Unix seconds

def _to_apple_ts(dt: datetime) -> int:
    """Convert a datetime to Apple nanosecond timestamp."""
    delta = dt.astimezone(timezone.utc) - _APPLE_EPOCH
    return int(delta.total_seconds() * 1e9)

def parse_context_range(text: str) -> tuple[int | None, int | None]:
    """Parse an optional range command out of the trigger message.

    Supported patterns (case-insensitive):
      last <N> messages   -> (n=N, since=None)
      last <N>m / mins    -> (n=None, since=now-N minutes)
      last <N>h / hours   -> (n=None, since=now-N hours)
      last <N>d / days    -> (n=None, since=now-N days)
      yesterday           -> (n=None, since=start of yesterday)
      today               -> (n=None, since=start of today)
      this week           -> (n=None, since=7 days ago)

    Returns (n, since_apple_ts). Both None means use the default.
    """
    t = text.lower()
    now = datetime.now(tz=timezone.utc)

    # "last N messages"
    m = re.search(r'last\s+(\d+)\s+messages?', t)
    if m:
        return int(m.group(1)), None

    # "last Nh" / "last N hours"
    m = re.search(r'last\s+(\d+)\s*h(?:ours?)?(?:\s|$)', t)
    if m:
        return None, _to_apple_ts(now - timedelta(hours=int(m.group(1))))

    # "last Nd" / "last N days"
    m = re.search(r'last\s+(\d+)\s*d(?:ays?)?(?:\s|$)', t)
    if m:
        return None, _to_apple_ts(now - timedelta(days=int(m.group(1))))

    # "last Nm" / "last N mins"
    m = re.search(r'last\s+(\d+)\s*m(?:in(?:utes?)?)?(?:\s|$)', t)
    if m:
        return None, _to_apple_ts(now - timedelta(minutes=int(m.group(1))))

    # "yesterday"
    if 'yesterday' in t:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return None, _to_apple_ts(start)

    # "today"
    if 'today' in t:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return None, _to_apple_ts(start)

    # "this week"
    if 'this week' in t or 'past week' in t:
        return None, _to_apple_ts(now - timedelta(days=7))

    return None, None


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

def should_respond(message: dict) -> bool:
    text = (message.get("text") or "").lower()
    return TRIGGER_WORD.lower() in text


def is_followup(conn: sqlite3.Connection, guid: str, current_rowid: int) -> bool:
    """True if the most recent prior message in the chat was from OpenClaw.

    This lets replies like 'yes', 'go deeper', 'tell me more' continue the
    conversation without needing the trigger word again.
    """
    cur = conn.execute(
        """
        SELECT m.text, m.attributedBody FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE c.guid = ? AND m.ROWID < ?
          AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
        ORDER BY m.ROWID DESC LIMIT 4
        """,
        (guid, current_rowid),
    )
    for text, attributed_body in cur.fetchall():
        if not text and attributed_body:
            text = _text_from_attributed_body(attributed_body)
        text = (text or "").strip()
        if not text:
            continue
        return text.startswith(BOT_PREFIX)
    return False


# ---------------------------------------------------------------------------
# Phase 1: Contact name resolution
# ---------------------------------------------------------------------------

def build_contact_cache() -> dict[str, str]:
    """Query Contacts.app via osascript; returns {digits: display_name}."""
    script = (
        'tell application "Contacts"\n'
        '    set output to ""\n'
        '    repeat with p in people\n'
        '        set pname to name of p\n'
        '        repeat with ph in phones of p\n'
        '            set pval to value of ph\n'
        '            set output to output & pval & "|" & pname & "\n"\n'
        '        end repeat\n'
        '    end repeat\n'
        '    return output\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("Contact cache: osascript error: %s", result.stderr.strip())
            return {}
        cache: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            phone, name = line.split("|", 1)
            digits = re.sub(r"\D", "", phone.strip())
            if digits:
                cache[digits] = name.strip()
        log.info("Contact cache built: %d entries", len(cache))
        return cache
    except Exception as exc:
        log.warning("Contact cache build failed: %s", exc)
        return {}


def resolve_sender(handle: str, cache: dict[str, str]) -> str:
    """Return a contact display name for the handle, or a formatted number."""
    if not handle or handle == "unknown":
        return handle
    digits = re.sub(r"\D", "", handle)
    if digits in cache:
        return cache[digits]
    # Try last 10 digits to handle +1 country code mismatches
    if len(digits) > 10 and digits[-10:] in cache:
        return cache[digits[-10:]]
    # Format as a readable phone number if possible
    if len(digits) == 11 and digits[0] == "1":
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return handle


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def format_context(messages: list[dict], name_cache: dict[str, str] | None = None) -> str:
    lines = []
    for msg in messages:
        if msg.get("is_from_me"):
            sender = "[me]"
        else:
            raw = msg["sender"]
            display = resolve_sender(raw, name_cache) if name_cache else raw
            sender = f"[{display}]"
        text = (msg.get("text") or "").strip()
        if text:
            lines.append(f"{sender}: {text}")
    return "\n".join(lines)


def _apple_ts_to_date_str(ts: int) -> str:
    """Convert Apple nanosecond timestamp to YYYY-MM-DD string."""
    try:
        unix_ts = ts / 1e9 + _APPLE_EPOCH_UNIX
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def format_context_with_memory(
    recent_msgs: list[dict],
    relevant_chunks: list[dict],
    name_cache: dict[str, str] | None = None,
) -> str:
    parts: list[str] = []
    if relevant_chunks:
        parts.append("--- Relevant history ---")
        for chunk in relevant_chunks:
            date_str = _apple_ts_to_date_str(chunk.get("timestamp", 0))
            text = chunk.get("text", "").strip()
            if date_str:
                parts.append(f"[{date_str}]")
            if text:
                parts.append(text)
        parts.append("")
    parts.append("--- Recent conversation (last 25) ---")
    parts.append(format_context(recent_msgs, name_cache))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 3: Search query heuristic
# ---------------------------------------------------------------------------

_SEARCH_PATTERNS = re.compile(
    r'\b(find|where is|where are|what\'s open|what is open|best .* near|'
    r'how much|what time does|who is|who are|look up|search for|current|'
    r'latest|today\'s|right now|near me|open now|hours for|'
    r'news|headlines|top stories|what happened|what\'s going on|'
    r'weather|forecast|temperature|score|standings|stock price|'
    r'fetch and|summarize this link|summarize this url)\b',
    re.IGNORECASE,
)


def _looks_like_search_query(text: str) -> bool:
    return bool(_SEARCH_PATTERNS.search(text))


# Matches markdown citations like ([domain](https://...)) or ([text](url))
_CITATION_RE = re.compile(r'\(\[[^\]]*\]\(https?://[^)]+\)\)')
# Matches residual inline markdown links [text](url) -- keep the display text
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(https?://[^)]+\)')
# Matches bare URLs left over after citation stripping
_BARE_URL_RE = re.compile(r'https?://\S+')
# Matches emoji unicode ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001f926-\U0001f937"
    "\U00010000-\U0010ffff"
    "\u2640-\u2642"
    "\u2600-\u2B55"
    "\u200d"
    "\u23cf"
    "\u23e9"
    "\u231a"
    "\ufe0f"
    "\u3030"
    "]+",
    flags=re.UNICODE,
)


def _clean_response(text: str) -> str:
    """Strip citation links, markdown link syntax, bare URLs, and emojis from response text."""
    text = _CITATION_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _BARE_URL_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    # Collapse multiple spaces/newlines left by removals
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Phase 2: OpenClaw integration
# ---------------------------------------------------------------------------

def warmup_openclaw() -> None:
    """Trigger TypeScript build before the first real user query."""
    global _openclaw_warmed_up
    log.info("Warming up OpenClaw (triggering TypeScript build)...")
    try:
        result = subprocess.run(
            [NODE_BIN, "scripts/run-node.mjs", "--version"],
            capture_output=True,
            text=True,
            cwd=OPENCLAW_DIR,
            timeout=120,
            env=_openclaw_env(),
        )
        if result.returncode == 0:
            log.info("OpenClaw warmup complete")
        else:
            log.warning("OpenClaw warmup exited %d: %s", result.returncode, result.stderr.strip()[:200])
        _openclaw_warmed_up = True
    except subprocess.TimeoutExpired:
        log.warning("OpenClaw warmup timed out (build may still be in progress)")
        _openclaw_warmed_up = True
    except Exception as exc:
        log.warning("OpenClaw warmup failed: %s", exc)


def ask_openclaw(context: str, session_args: list[str] | None = None) -> AgentResponse:
    """Run the OpenClaw agent and return an AgentResponse, or empty response on any error."""
    timeout = 60 if _openclaw_warmed_up else 180
    try:
        log.info("Calling OpenClaw agent (timeout=%ds)", timeout)
        cmd = [NODE_BIN, "scripts/run-node.mjs", "agent", "--local", "--message", context]
        if session_args:
            cmd.extend(session_args)
        cmd.append("--json")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=OPENCLAW_DIR,
            timeout=timeout,
            env=_openclaw_env(),
        )
        if result.returncode != 0:
            log.warning("OpenClaw exited %d: %s", result.returncode, result.stderr.strip()[:300])
            return AgentResponse("", [])
        # Find the JSON object in stdout (may have build output before it)
        stdout = result.stdout
        json_start = stdout.find("{")
        if json_start == -1:
            log.warning("OpenClaw: no JSON found in stdout")
            return AgentResponse("", [])
        data = json.loads(stdout[json_start:])
        text = ""
        media_urls: list[str] = []
        try:
            payload = data["result"]["payloads"][0]
            text = (payload.get("text") or "").strip()
            # Collect media URLs from singular and array fields
            if payload.get("mediaUrls"):
                media_urls = list(payload["mediaUrls"])
            elif payload.get("mediaUrl"):
                media_urls = [payload["mediaUrl"]]
        except (KeyError, IndexError, TypeError):
            pass
        if not text:
            text = (data.get("summary") or "").strip()
        return AgentResponse(_clean_response(text), media_urls)
    except subprocess.TimeoutExpired:
        log.warning("OpenClaw agent timed out after %ds", timeout)
        return AgentResponse("", [])
    except json.JSONDecodeError as exc:
        log.warning("OpenClaw JSON parse error: %s", exc)
        return AgentResponse("", [])
    except Exception as exc:
        log.warning("OpenClaw call failed: %s", exc)
        return AgentResponse("", [])


# ---------------------------------------------------------------------------
# OpenAI direct (fallback)
# ---------------------------------------------------------------------------

def _ask_openai_direct(context: str, force_model: str | None = None) -> AgentResponse:
    """Call OpenAI chat completions directly and return an AgentResponse."""
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY is not set. Export it before running.")
        return AgentResponse("", [])

    model = force_model or OPENAI_MODEL

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"].strip()
            return AgentResponse(_clean_response(text), [])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code == 429 and attempt == 0 and model == OPENAI_SEARCH_MODEL:
                log.warning("Search model rate limited, retrying with base model")
                model = OPENAI_MODEL
                payload = json.dumps({
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": context},
                    ],
                }).encode()
                req = urllib.request.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=payload,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                continue
            log.error("OpenAI API error %d: %s", exc.code, body)
            return AgentResponse("", [])
        except Exception as exc:
            log.error("OpenAI request failed: %s", exc)
            return AgentResponse("", [])
    return AgentResponse("", [])


def ask_openai(context: str, session_args: list[str] | None = None) -> AgentResponse:
    """Call OpenAI directly."""
    return _ask_openai_direct(context)


# ---------------------------------------------------------------------------
# Session continuity
# ---------------------------------------------------------------------------

def _session_args(chat: dict, msg: dict) -> list[str]:
    """Return CLI args for OpenClaw session continuity.

    1-on-1 (;-; in guid): ["--to", sender_e164]
    group   (;+; in guid): ["--session-id", "imsg-" + guid_alphanum]
    """
    guid = chat["guid"]
    if ";-;" in guid:
        return ["--to", msg["sender"]]
    safe_id = "imsg-" + re.sub(r"[^a-z0-9]", "", guid.lower())
    return ["--session-id", safe_id]


# ---------------------------------------------------------------------------
# Per-chat context injection
# ---------------------------------------------------------------------------

def _build_agent_message(conversation_context: str, chat_context: str, trigger_msg: str = "") -> str:
    """Prepend identity and per-chat context to the conversation block sent to the agent."""
    parts: list[str] = []
    if _identity:
        parts.append(f"[Persona]\n{_identity}")
    if chat_context:
        parts.append(f"[Chat context: {chat_context}]")
    parts.append(f"--- Conversation ---\n{conversation_context}")
    if trigger_msg:
        parts.append(f"--- Message to respond to ---\n{trigger_msg.strip()}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Send via AppleScript
# ---------------------------------------------------------------------------

def _escape_for_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _is_group_chat(guid: str) -> bool:
    return ";+;" in guid


def send_reply(text: str, guid: str, handle: str) -> None:
    prefixed = f"{BOT_PREFIX} {text.strip()}"
    escaped_text = _escape_for_applescript(prefixed)

    if _is_group_chat(guid):
        escaped_guid = _escape_for_applescript(guid)
        script = (
            'tell application "Messages"\n'
            '  set targetService to 1st service whose service type = iMessage\n'
            f'  set targetChat to chat id "{escaped_guid}" of targetService\n'
            f'  send "{escaped_text}" to targetChat\n'
            'end tell'
        )
    else:
        escaped_handle = _escape_for_applescript(handle)
        script = (
            'tell application "Messages"\n'
            '  set targetService to 1st service whose service type = iMessage\n'
            f'  set targetBuddy to buddy "{escaped_handle}" of targetService\n'
            f'  send "{escaped_text}" to targetBuddy\n'
            'end tell'
        )

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.error("osascript error (exit %d): %s", result.returncode, result.stderr.strip())
        else:
            log.info("Sent reply to %s (%d chars)", handle, len(prefixed))
    except subprocess.TimeoutExpired:
        log.error("osascript timed out")
    except Exception as exc:
        log.error("osascript failed: %s", exc)


def send_media_reply(media_urls: list[str], guid: str, handle: str) -> None:
    """Download each media URL and send it via osascript."""
    for url in media_urls:
        tmp_path = None
        try:
            # Derive extension from URL path, default to .jpg
            url_path = url.split("?")[0]
            ext = os.path.splitext(url_path)[1] or ".jpg"
            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            urllib.request.urlretrieve(url, tmp_path)
            escaped_path = _escape_for_applescript(tmp_path)
            if _is_group_chat(guid):
                escaped_guid = _escape_for_applescript(guid)
                script = (
                    'tell application "Messages"\n'
                    '  set targetService to 1st service whose service type = iMessage\n'
                    f'  set targetChat to chat id "{escaped_guid}" of targetService\n'
                    f'  send POSIX file "{escaped_path}" to targetChat\n'
                    'end tell'
                )
            else:
                escaped_handle = _escape_for_applescript(handle)
                script = (
                    'tell application "Messages"\n'
                    '  set targetService to 1st service whose service type = iMessage\n'
                    f'  set targetBuddy to buddy "{escaped_handle}" of targetService\n'
                    f'  send POSIX file "{escaped_path}" to targetBuddy\n'
                    'end tell'
                )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                log.error("osascript media error (exit %d): %s", result.returncode, result.stderr.strip())
            else:
                log.info("Sent media to %s (%s)", handle, url[:80])
        except Exception as exc:
            log.error("send_media_reply failed for %s: %s", url[:80], exc)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# URL auto-summarization
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")

# Domains where a summary would be redundant -- the link itself is the content.
_SKIP_SUMMARIZE_DOMAINS = {
    "spotify.com", "open.spotify.com",
    "music.apple.com",
    "instagram.com", "www.instagram.com",
    "x.com", "twitter.com",
    "tiktok.com", "vm.tiktok.com",
    "youtube.com", "youtu.be", "www.youtube.com",
    "soundcloud.com",
    "pinterest.com",
    "snapchat.com",
    "facebook.com", "fb.com",
    "threads.net",
}


def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def _url_domain(url: str) -> str:
    try:
        host = url.split("//", 1)[1].split("/")[0].lower()
        return host.removeprefix("www.")
    except Exception:
        return ""


_JINA_BASE = "https://r.jina.ai/"
# Max characters of article content to include in the summarize prompt
_JINA_MAX_CHARS = 2500


def _load_cloudflare_config() -> dict:
    path = os.path.expanduser("~/.cloudflare.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


_CF_CONFIG = _load_cloudflare_config()


def _fetch_url_content(url: str) -> str:
    """Fetch clean article text with fallback chain: Jina -> curl -> Cloudflare Browser Rendering."""
    # Tier 1: Jina Reader
    try:
        req = urllib.request.Request(
            _JINA_BASE + url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode(errors="replace")
        if content.strip():
            return content[:_JINA_MAX_CHARS].strip()
    except Exception as exc:
        log.debug("Jina fetch failed for %s: %s", url[:80], exc)

    # Tier 2: curl with browser User-Agent
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "15",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
             url],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout[:_JINA_MAX_CHARS].strip()
    except Exception as exc:
        log.debug("curl fetch failed for %s: %s", url[:80], exc)

    # Tier 3: Cloudflare Browser Rendering
    account_id = _CF_CONFIG.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = _CF_CONFIG.get("CLOUDFLARE_BR_TOKEN") or os.environ.get("CLOUDFLARE_BR_TOKEN")
    if account_id and token:
        try:
            crawl_req = urllib.request.Request(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering/crawl",
                data=json.dumps({"url": url, "limit": 1, "formats": ["markdown"]}).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(crawl_req, timeout=30) as resp:
                crawl_data = json.loads(resp.read().decode())
            job_id = (crawl_data.get("result") or {}).get("id") or crawl_data.get("id")
            if job_id:
                time.sleep(3)
                result_req = urllib.request.Request(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering/crawl/{job_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(result_req, timeout=15) as resp:
                    result_data = json.loads(resp.read().decode())
                pages = (result_data.get("result") or {}).get("pages") or []
                if pages:
                    markdown = pages[0].get("markdown") or ""
                    if markdown.strip():
                        return markdown[:_JINA_MAX_CHARS].strip()
        except Exception as exc:
            log.warning("Cloudflare fetch failed for %s: %s", url[:80], exc)

    return ""


def should_auto_summarize(msg: dict, chat: dict, conn: sqlite3.Connection | None = None) -> bool:
    """True when the chat has auto_summarize_urls enabled, no trigger word, a summarizable URL is present,
    and the immediately preceding user message did not contain the trigger word."""
    if not chat.get("auto_summarize_urls"):
        return False
    if should_respond(msg):
        return False
    urls = _extract_urls(msg.get("text") or "")
    if not any(_url_domain(u) not in _SKIP_SUMMARIZE_DOMAINS for u in urls):
        return False
    # If the last non-bot message before this one had the trigger word, the claw
    # path already owns that intent -- skip auto-summarize.
    if conn is not None:
        try:
            cur = conn.execute(
                """
                SELECT m.text, m.attributedBody FROM message m
                JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                JOIN chat c ON cmj.chat_id = c.ROWID
                WHERE c.guid = ? AND m.ROWID < ? AND m.is_from_me = 0
                  AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
                ORDER BY m.ROWID DESC LIMIT 1
                """,
                (chat["guid"], msg["ROWID"]),
            )
            row = cur.fetchone()
            if row:
                text, attributed_body = row
                if not text and attributed_body:
                    text = _text_from_attributed_body(attributed_body)
                if TRIGGER_WORD.lower() in (text or "").lower():
                    return False
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY environment variable is not set.")
        print("Run:  export OPENAI_API_KEY='sk-...'")
        sys.exit(1)

    log.info("OpenClaw iMessage bridge starting")
    log.info("Monitoring %d chat(s) -- trigger: %s -- model: %s",
             len(MONITORED_CHATS), TRIGGER_WORD, OPENAI_MODEL)

    # Phase 1: Build contact name cache (non-blocking -- Contacts.app can be slow)
    name_cache: dict[str, str] = {}
    def _fill_cache():
        name_cache.update(build_contact_cache())
    threading.Thread(target=_fill_cache, daemon=True).start()

    conn = open_db()

    # Per-chat state: {guid: last_rowid}
    state: dict[str, int] = {}
    for chat in MONITORED_CHATS:
        guid = chat["guid"]
        handle = chat["handle"]
        last_rowid = load_last_rowid(guid)
        if last_rowid == 0:
            try:
                last_rowid = get_current_tip(conn, guid)
                save_last_rowid(guid, last_rowid)
                log.info("[%s] first run, starting from ROWID %d", handle, last_rowid)
            except Exception as exc:
                log.warning("[%s] could not determine tip: %s", handle, exc)
        else:
            log.info("[%s] resuming from ROWID %d", handle, last_rowid)
        state[guid] = last_rowid
        if chat["announce"]:
            send_reply("online -- say 'claw' to get my attention", guid, handle)

    # Phase 4: Build or load chat memory for each monitored chat
    memory: dict[str, ChatMemory] = {}
    for chat in MONITORED_CHATS:
        guid = chat["guid"]
        handle = chat["handle"]
        try:
            log.info("[%s] loading chat memory...", handle)
            memory[guid] = ChatMemory.load_or_build(guid, conn, OPENAI_API_KEY, MEMORY_DIR)
            log.info("[%s] memory ready (%d chunks)", handle, len(memory[guid].chunks))
        except Exception as exc:
            log.warning("[%s] chat memory unavailable: %s", handle, exc)
            memory[guid] = ChatMemory(guid, MEMORY_DIR, OPENAI_API_KEY)

    while True:
        try:
            for chat in MONITORED_CHATS:
                guid = chat["guid"]
                handle = chat["handle"]
                messages = get_new_messages(conn, guid, state[guid])

                # Only act on the last actionable message in the batch --
                # skip earlier triggers that piled up while the bridge was busy.
                last_actionable_rowid: int | None = None
                for msg in messages:
                    text = msg.get("text") or ""
                    if text.startswith(BOT_PREFIX):
                        continue
                    if should_respond(msg) or should_auto_summarize(msg, chat, conn):
                        last_actionable_rowid = msg["ROWID"]

                for msg in messages:
                    state[guid] = msg["ROWID"]
                    save_last_rowid(guid, msg["ROWID"])

                    # Phase 4: update memory with every new message
                    try:
                        memory[guid].update([msg])
                    except Exception as exc:
                        log.warning("[%s] memory update failed: %s", handle, exc)

                    # Never respond to OpenClaw's own replies.
                    msg_text = (msg.get("text") or "")
                    if msg_text.startswith(BOT_PREFIX):
                        log.debug("[%s] skipping own reply ROWID %d", handle, msg["ROWID"])
                        continue

                    # Drop messages older than 5 minutes -- Mac may have been asleep
                    if msg.get("date"):
                        msg_unix = msg["date"] / 1e9 + _APPLE_EPOCH_UNIX
                        age_seconds = time.time() - msg_unix
                        if age_seconds > 300:
                            log.info("[%s] dropping stale message ROWID %d (age %.0fs)", handle, msg["ROWID"], age_seconds)
                            continue

                    # Skip earlier triggers -- only respond to the last one in the batch
                    if last_actionable_rowid is not None and msg["ROWID"] < last_actionable_rowid:
                        if should_respond(msg) or should_auto_summarize(msg, chat, conn):
                            log.debug("[%s] skipping stale trigger ROWID %d (last is %d)", handle, msg["ROWID"], last_actionable_rowid)
                            continue

                    sess_args = _session_args(chat, msg)

                    if should_respond(msg) and not should_auto_summarize(msg, chat, conn):
                        log.info("[%s] triggered by ROWID %d from %s",
                                 handle, msg["ROWID"],
                                 resolve_sender(msg["sender"], name_cache))

                        n_override, since_ts = parse_context_range(msg["text"])
                        if since_ts is not None:
                            log.info("[%s] time-range context requested", handle)
                            ctx_msgs = get_context_window(conn, guid, since_apple_ts=since_ts)
                        elif n_override is not None:
                            log.info("[%s] message-count context: %d", handle, n_override)
                            ctx_msgs = get_context_window(conn, guid, n=n_override)
                        else:
                            ctx_msgs = get_context_window(conn, guid)

                        # Phase 4: inject relevant memory
                        try:
                            relevant = memory[guid].search(msg["text"], top_k=5)
                        except Exception as exc:
                            log.warning("[%s] memory search failed: %s", handle, exc)
                            relevant = []

                        if relevant:
                            conversation_ctx = format_context_with_memory(ctx_msgs, relevant, name_cache)
                        else:
                            conversation_ctx = format_context(ctx_msgs, name_cache)

                        trigger_text = msg.get("text") or ""
                        full_msg = _build_agent_message(conversation_ctx, chat["context"], trigger_msg=trigger_text)
                        log.info("[%s] Chat context: %s", handle, chat["context"][:60])
                        # Detect search queries from trigger message only, not the full context
                        if _looks_like_search_query(trigger_text):
                            log.info("Search query detected in trigger, using model: %s", OPENAI_SEARCH_MODEL)
                            response = _ask_openai_direct(full_msg, force_model=OPENAI_SEARCH_MODEL)
                        else:
                            response = _ask_openai_direct(full_msg)

                        if response.text:
                            send_reply(response.text, guid, handle)
                        if response.media_urls:
                            send_media_reply(response.media_urls, guid, handle)
                        if response.text or response.media_urls:
                            time.sleep(REPLY_COOLDOWN)
                        else:
                            log.warning("[%s] empty response for ROWID %d", handle, msg["ROWID"])

                    elif should_auto_summarize(msg, chat, conn):
                        urls = [u for u in _extract_urls(msg.get("text") or "") if _url_domain(u) not in _SKIP_SUMMARIZE_DOMAINS]
                        url = urls[0]
                        log.info("[%s] auto-summarizing URL: %s", handle, url[:80])
                        ctx_msgs = get_context_window(conn, guid)
                        conversation_ctx = format_context(ctx_msgs, name_cache)
                        prompt = (
                            "Someone shared a link. Fetch it and summarize the content in 2-3 sentences. "
                            "If the recent conversation includes a specific question about the link, tailor the summary to answer it. "
                            f"Link: {url}"
                        )
                        full_msg = _build_agent_message(conversation_ctx, chat["context"], trigger_msg=prompt)
                        response = _ask_openai_direct(full_msg, force_model=OPENAI_SEARCH_MODEL)
                        if response.text:
                            send_reply(response.text, guid, handle)
                            time.sleep(REPLY_COOLDOWN)

                    else:
                        log.debug("[%s] skipping ROWID %d (no trigger)", handle, msg["ROWID"])

        except sqlite3.OperationalError as exc:
            log.warning("DB read error (reconnecting): %s", exc)
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(1)
            conn = open_db()
        except Exception as exc:
            log.error("Unexpected error in poll loop: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--list-chats" in sys.argv:
        try:
            c = open_db()
            list_chats(c)
            c.close()
        except Exception as exc:
            print(f"Error reading chat.db: {exc}")
            print("Make sure Ghostty has Full Disk Access in System Settings.")
            sys.exit(1)
        sys.exit(0)

    main()
