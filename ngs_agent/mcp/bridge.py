"""MCP (Model Context Protocol) bridge — exposes external MCP servers
as BaseTool instances to the agent loop.
"""
from __future__ import annotations

from typing import Any

from ..tools.base import BaseTool, ToolContext, ToolInfo, ToolResponse


class MCPToolBridge(BaseTool):
    """Wraps a remote MCP tool as a local BaseTool."""

    def __init__(self, server_name: str, tool_def: dict, session: Any = None):
        self._server = server_name
        self._def = tool_def
        self._session = session
        self._name = f"mcp_{server_name}_{tool_def['name']}"

    def info(self) -> ToolInfo:
        return ToolInfo(
            name=self._name,
            description=(
                f"[MCP:{self._server}] " +
                self._def.get("description", "MCP tool")
            ),
            parameters=self._def.get("inputSchema", {}).get("properties", {}),
            required=self._def.get("inputSchema", {}).get("required", []),
            deferred=True,
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        if self._session is None:
            return ToolResponse(
                content=f"MCP server '{self._server}' is configured but not connected.",
                is_error=True,
            )
        try:
            result = await self._session.call_tool(self._def["name"], params)
            text_parts = []
            for c in result.content:
                if hasattr(c, "text"):
                    text_parts.append(c.text)
                else:
                    text_parts.append(str(c))
            return ToolResponse(
                content="\n".join(text_parts),
                metadata={"mcp_server": self._server, "mcp_tool": self._def["name"]},
            )
        except Exception as e:
            return ToolResponse(
                content=f"MCP call failed: {type(e).__name__}: {e}",
                is_error=True,
            )


async def discover_mcp_tools(server_name: str, session: Any) -> list[MCPToolBridge]:
    """List tools from a connected MCP session and wrap each as a BaseTool."""
    try:
        tools_resp = await session.list_tools()
        return [
            MCPToolBridge(server_name, t.model_dump() if hasattr(t, "model_dump") else t, session)
            for t in tools_resp.tools
        ]
    except Exception:
        return []
