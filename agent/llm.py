"""
LLM Orchestrator

Drives Anthropic Claude with MCP tool definitions, executing the agentic
tool-use loop until a final text response is produced.
"""

import json
import logging
import os
from typing import Any

import anthropic

from .mcp_client import MCPClient, MCPClientError

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "10"))

SYSTEM_PROMPT = """\
You are an expert Kubernetes SRE assistant embedded in a Slack workspace.
You have access to a live Kubernetes cluster through the tools below.

When a user reports an incident or asks about cluster health:
1. Gather relevant data using the available tools (deployment status, pod logs, metrics).
2. Correlate the evidence across signals — don't rely on a single data source.
3. Present a clear, concise analysis with:
   • *Root Cause*: what went wrong and why.
   • *Evidence*: cited log lines, metric values, or status fields.
   • *Remediation*: actionable next steps the team can take.

Format your response using Slack mrkdwn:
- Use *bold* for emphasis, `code` for identifiers, and ```code blocks``` for log excerpts.
- Keep responses focused; avoid unnecessary preamble.
- If tools return errors or missing data, say so honestly rather than guessing.
"""


def _mcp_tools_to_anthropic(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Convert MCP tool definitions into Anthropic's tool format.

    MCP tools have:
        {"name": ..., "description": ..., "inputSchema": {...}}

    Anthropic tools expect:
        {"name": ..., "description": ..., "input_schema": {...}}
    """
    anthropic_tools: list[dict[str, Any]] = []
    for tool in mcp_tools:
        anthropic_tools.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
        })
    return anthropic_tools


async def run_agent_loop(
    user_message: str,
    mcp: MCPClient,
    mcp_tools: list[dict[str, Any]],
) -> str:
    """
    Execute the full agentic loop:
      user message → LLM → (tool calls ↔ MCP gateway)* → final text

    Returns the final text response from the LLM.
    """
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    tools = _mcp_tools_to_anthropic(mcp_tools)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_message},
    ]

    for round_num in range(1, MAX_TOOL_ROUNDS + 1):
        logger.info("LLM round %d/%d", round_num, MAX_TOOL_ROUNDS)

        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        logger.debug(
            "LLM response: stop_reason=%s, content_blocks=%d",
            response.stop_reason,
            len(response.content),
        )

        # If the model finished without requesting tools, extract final text.
        if response.stop_reason == "end_turn":
            return _extract_text(response.content)

        # The model wants to call tools.
        if response.stop_reason != "tool_use":
            # Unexpected stop reason – return whatever text we have.
            logger.warning("Unexpected stop_reason: %s", response.stop_reason)
            return _extract_text(response.content) or (
                f"_(LLM stopped unexpectedly: {response.stop_reason})_"
            )

        # Append the full assistant message (including tool_use blocks)
        # so Claude can see the conversation history.
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool_use block and collect results.
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            tool_use_id = block.id

            logger.info(
                "Calling tool: %s(%s)",
                tool_name,
                json.dumps(tool_input, separators=(",", ":")),
            )

            try:
                result_text = await mcp.call_tool(tool_name, tool_input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_text,
                })
            except MCPClientError as exc:
                logger.error("Tool %s failed: %s", tool_name, exc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"Error executing {tool_name}: {exc}",
                    "is_error": True,
                })

        # Feed tool results back to the LLM.
        messages.append({"role": "user", "content": tool_results})

    # Exhausted the maximum number of rounds.
    logger.warning("Reached max tool rounds (%d)", MAX_TOOL_ROUNDS)
    return (
        "⚠️ I reached the maximum number of investigation steps without a final "
        "conclusion. Here's what I found so far — please refine your question or "
        "ask me to continue."
    )


def _extract_text(content_blocks: list[Any]) -> str:
    """Pull all TextBlock content out of a response."""
    parts: list[str] = []
    for block in content_blocks:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)
