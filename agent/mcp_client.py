"""
MCP JSON-RPC Client

Manages the Go gateway binary as a subprocess communicating over stdin/stdout
using the Model Context Protocol (JSON-RPC 2.0).
"""

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

GATEWAY_BINARY_PATH = os.getenv("GATEWAY_BINARY_PATH", "/shared/gateway")

# Timeout for individual JSON-RPC calls (seconds)
RPC_TIMEOUT = int(os.getenv("MCP_RPC_TIMEOUT", "30"))


class MCPClientError(Exception):
    """Raised when an MCP JSON-RPC call fails."""


class MCPClient:
    """
    Manages a single MCP gateway subprocess over stdio JSON-RPC.

    Lifecycle:
        client = MCPClient()
        await client.start()
        tools = await client.list_tools()
        result = await client.call_tool("get_deployment_status", {...})
        await client.stop()
    """

    def __init__(self, binary_path: str | None = None):
        self._binary_path = binary_path or GATEWAY_BINARY_PATH
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Spawn the gateway subprocess and perform the MCP handshake."""
        if self._process is not None and self._process.returncode is None:
            logger.warning("MCPClient.start() called but process already running")
            return {}

        logger.info("Spawning MCP gateway: %s", self._binary_path)
        self._process = await asyncio.create_subprocess_exec(
            self._binary_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # MCP handshake: initialize → initialized notification
        init_result = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "kube-log-mcp-slack-agent",
                "version": "1.0.0",
            },
        })

        # Send the initialized notification (no id → notification)
        await self._send_notification("notifications/initialized", {})

        logger.info("MCP gateway initialized: %s", json.dumps(init_result, indent=2))
        return init_result

    async def stop(self) -> None:
        """Gracefully terminate the gateway subprocess."""
        if self._process is None:
            return

        if self._process.returncode is None:
            try:
                self._process.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Gateway did not exit gracefully, killing")
                self._process.kill()
                await self._process.wait()

        self._process = None
        logger.info("MCP gateway stopped")

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools exposed by the MCP server."""
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """
        Invoke a tool on the MCP server and return its text content.

        Raises MCPClientError on RPC-level errors or tool-level isError responses.
        """
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        # MCP tool results contain a list of content blocks
        content_parts = result.get("content", [])
        is_error = result.get("isError", False)

        text_parts: list[str] = []
        for part in content_parts:
            if part.get("type") == "text":
                text_parts.append(part["text"])

        combined = "\n".join(text_parts) if text_parts else json.dumps(result)

        if is_error:
            raise MCPClientError(f"Tool '{tool_name}' returned error: {combined}")

        return combined

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the matching response."""
        async with self._lock:
            self._request_id += 1
            req_id = self._request_id

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        raw = json.dumps(message, separators=(",", ":")) + "\n"
        logger.debug("→ MCP send: %s", raw.strip())

        proc = self._ensure_process()
        proc.stdin.write(raw.encode("utf-8"))  # type: ignore[union-attr]
        await proc.stdin.drain()  # type: ignore[union-attr]

        # Read lines until we get the response matching our request id.
        # MCP servers may emit log notifications interleaved with responses.
        async with self._read_lock:
            response = await self._read_response(req_id)

        return response

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        raw = json.dumps(message, separators=(",", ":")) + "\n"
        logger.debug("→ MCP notify: %s", raw.strip())

        proc = self._ensure_process()
        proc.stdin.write(raw.encode("utf-8"))  # type: ignore[union-attr]
        await proc.stdin.drain()  # type: ignore[union-attr]

    async def _read_response(self, expected_id: int) -> dict[str, Any]:
        """
        Read lines from stdout until we receive the JSON-RPC response
        matching ``expected_id``. Server-initiated notifications are logged
        and discarded.
        """
        proc = self._ensure_process()
        deadline = asyncio.get_event_loop().time() + RPC_TIMEOUT

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise MCPClientError(
                    f"Timed out waiting for response to request {expected_id}"
                )

            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),  # type: ignore[union-attr]
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise MCPClientError(
                    f"Timed out reading from MCP gateway (request {expected_id})"
                )

            if not line:
                # EOF – process probably died
                stderr_output = ""
                if proc.stderr:
                    try:
                        stderr_bytes = await asyncio.wait_for(
                            proc.stderr.read(), timeout=2
                        )
                        stderr_output = stderr_bytes.decode("utf-8", errors="replace")
                    except asyncio.TimeoutError:
                        pass
                raise MCPClientError(
                    f"MCP gateway process exited unexpectedly. stderr: {stderr_output}"
                )

            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            logger.debug("← MCP recv: %s", line_str)

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("Non-JSON line from gateway: %s", line_str)
                continue

            # Server-initiated notification (no "id" field) → log and skip
            if "id" not in msg:
                logger.debug("MCP notification: %s", msg.get("method", "unknown"))
                continue

            if msg["id"] != expected_id:
                logger.warning(
                    "Unexpected response id %s (expected %s)", msg["id"], expected_id
                )
                continue

            # Check for JSON-RPC error
            if "error" in msg:
                err = msg["error"]
                raise MCPClientError(
                    f"JSON-RPC error {err.get('code')}: {err.get('message')}"
                )

            return msg.get("result", {})

    def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            raise MCPClientError("MCP gateway process is not running")
        return self._process
