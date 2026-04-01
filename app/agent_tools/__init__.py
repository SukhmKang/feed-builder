"""Claude Agent SDK tool package for feed discovery and feed preview workflows."""

from claude_agent_sdk import create_sdk_mcp_server

from app.agent_tools.custom_blocks import CUSTOM_BLOCK_TOOLS
from app.agent_tools.discovery import DISCOVERY_TOOLS
from app.agent_tools.environment import UTILITY_TOOLS
from app.agent_tools.feeds import FEED_TOOLS

TOOLS = [*DISCOVERY_TOOLS, *FEED_TOOLS, *CUSTOM_BLOCK_TOOLS, *UTILITY_TOOLS]

MCP_SERVER = create_sdk_mcp_server(
    name="feed_builder_tools",
    version="1.0.0",
    tools=TOOLS,
)

__all__ = ["CUSTOM_BLOCK_TOOLS", "DISCOVERY_TOOLS", "FEED_TOOLS", "MCP_SERVER", "TOOLS", "UTILITY_TOOLS"]
