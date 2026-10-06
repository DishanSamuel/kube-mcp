"""
Slack ChatOps Agent Runner

Production-ready Slack bot using Socket Mode (no public ingress required).
Listens for @mentions and incident channel messages, orchestrates Claude + MCP
gateway to diagnose Kubernetes issues, and posts results back to Slack threads.
"""

import asyncio
import logging
import os
import re
import signal
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # Load .env file before reading any config

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from .mcp_client import MCPClient, MCPClientError
from .llm import run_agent_loop

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("slack-agent")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

# Optional: restrict the bot to specific channels (comma-separated IDs).
# If empty, the bot responds in any channel it's invited to.
INCIDENT_CHANNELS = [
    c.strip()
    for c in os.getenv("INCIDENT_CHANNELS", "").split(",")
    if c.strip()
]

# Maximum Slack message length (Slack hard-caps at ~40 000 chars for text)
SLACK_MAX_LENGTH = 3900

GATEWAY_BINARY_PATH = os.getenv("GATEWAY_BINARY_PATH", "/shared/gateway")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

# Shared async event loop running in a background thread.
_loop: asyncio.AbstractEventLoop | None = None

# Global MCP client – started once on boot, restarted on failure.
_mcp: MCPClient | None = None
_mcp_tools: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# MCP lifecycle helpers
# ---------------------------------------------------------------------------

async def _ensure_mcp() -> tuple[MCPClient, list[dict[str, Any]]]:
    """Return a running MCPClient and its cached tool list, starting one if needed."""
    global _mcp, _mcp_tools

    if _mcp is not None and _mcp.is_running:
        return _mcp, _mcp_tools

    logger.info("(Re)starting MCP gateway subprocess")
    mcp = MCPClient(binary_path=GATEWAY_BINARY_PATH)
    await mcp.start()
    _mcp_tools = await mcp.list_tools()
    _mcp = mcp

    tool_names = [t["name"] for t in _mcp_tools]
    logger.info("MCP tools available: %s", tool_names)

    return _mcp, _mcp_tools


async def _stop_mcp() -> None:
    global _mcp
    if _mcp is not None:
        await _mcp.stop()
        _mcp = None


# ---------------------------------------------------------------------------
# Slack App
# ---------------------------------------------------------------------------

app = App(token=SLACK_BOT_TOKEN)


def _clean_mention(text: str) -> str:
    """Strip the bot's @mention tag from the message text."""
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def _split_message(text: str, limit: int = SLACK_MAX_LENGTH) -> list[str]:
    """
    Split a long message into chunks that fit within Slack's character limit.
    Tries to split on newlines to avoid breaking mid-sentence.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # Find the last newline within the limit
        split_pos = text.rfind("\n", 0, limit)
        if split_pos == -1:
            # No newline found — hard-split at limit
            split_pos = limit

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return chunks


async def _handle_message_async(
    user_text: str,
    say: Any,
    thread_ts: str,
    channel: str,
    client: WebClient,
) -> None:
    """Core async handler: spins up MCP + LLM and posts the result."""
    try:
        mcp, tools = await _ensure_mcp()
    except MCPClientError as exc:
        logger.error("Failed to start MCP gateway: %s", exc)
        say(
            text=f"❌ Failed to connect to the observability gateway:\n```{exc}```",
            thread_ts=thread_ts,
        )
        return

    # Post a "thinking" indicator
    thinking_resp = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text="🔍 _Investigating..._",
    )
    thinking_ts = thinking_resp["ts"]

    try:
        response_text = await run_agent_loop(user_text, mcp, tools)
    except Exception as exc:
        logger.exception("Agent loop failed")
        client.chat_update(
            channel=channel,
            ts=thinking_ts,
            text=f"❌ Investigation failed:\n```{exc}```",
        )
        return

    # Delete the thinking message
    try:
        client.chat_delete(channel=channel, ts=thinking_ts)
    except Exception:
        # If we can't delete (permissions), update it instead
        try:
            client.chat_update(channel=channel, ts=thinking_ts, text=" ")
        except Exception:
            pass

    # Post the response, splitting if necessary
    chunks = _split_message(response_text)
    for chunk in chunks:
        say(text=chunk, thread_ts=thread_ts)


def _dispatch_async(
    user_text: str,
    say: Any,
    thread_ts: str,
    channel: str,
    client: WebClient,
) -> None:
    """Bridge from sync Slack bolt handler to our async pipeline."""
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=_loop.run_forever, daemon=True)
        t.start()

    future = asyncio.run_coroutine_threadsafe(
        _handle_message_async(user_text, say, thread_ts, channel, client),
        _loop,
    )
    # Block the Slack bolt worker thread until complete so back-pressure
    # is applied naturally.  Bolt uses a thread pool, so this is fine.
    future.result()


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_mention(event: dict, say: Any, client: WebClient) -> None:
    """Respond to @mentions in any channel the bot is in."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    raw_text = event.get("text", "")
    user_text = _clean_mention(raw_text)

    if not user_text:
        say(
            text="👋 Hey! Ask me about a deployment, pod logs, or cluster metrics "
                 "and I'll investigate.",
            thread_ts=thread_ts,
        )
        return

    logger.info(
        "Mention in %s: %s",
        channel,
        user_text[:120] + ("..." if len(user_text) > 120 else ""),
    )

    # Acknowledge with a reaction
    try:
        client.reactions_add(channel=channel, name="eyes", timestamp=event["ts"])
    except Exception:
        pass  # Non-critical

    _dispatch_async(user_text, say, thread_ts, channel, client)


@app.event("message")
def handle_channel_message(event: dict, say: Any, client: WebClient) -> None:
    """
    Respond to direct messages in designated incident channels.

    Skips messages from bots, threads that the bot didn't start, and channels
    not in the INCIDENT_CHANNELS allowlist (if configured).
    """
    # Ignore bot messages and message edits/deletes
    if event.get("bot_id") or event.get("subtype"):
        return

    channel = event["channel"]

    # If incident channels are configured, only respond in those
    if INCIDENT_CHANNELS and channel not in INCIDENT_CHANNELS:
        return

    # In incident channels, respond to top-level messages only
    # (thread replies are handled by app_mention)
    if event.get("thread_ts"):
        return

    thread_ts = event["ts"]
    user_text = event.get("text", "").strip()

    if not user_text:
        return

    logger.info(
        "Incident channel %s: %s",
        channel,
        user_text[:120] + ("..." if len(user_text) > 120 else ""),
    )

    try:
        client.reactions_add(channel=channel, name="eyes", timestamp=event["ts"])
    except Exception:
        pass

    _dispatch_async(user_text, say, thread_ts, channel, client)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting Slack agent runner")
    logger.info("Gateway binary: %s", GATEWAY_BINARY_PATH)

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    # Graceful shutdown
    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down", signum)
        handler.close()  # type: ignore[attr-defined]
        if _loop is not None:
            _loop.call_soon_threadsafe(_loop.stop)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    handler.start()


if __name__ == "__main__":
    main()
