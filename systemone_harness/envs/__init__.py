"""Environments that ship with the harness: a deterministic workflow for tests and the benchmark,
a stdio adapter that makes any process an environment, and an MCP adapter that makes any MCP
server one (the order desk is also served that way, in order_mcp)."""
from .mcp import McpEnvironment
from .order_workflow import OrderWorkflow
from .stdio import StdioEnvironment

__all__ = ["McpEnvironment", "OrderWorkflow", "StdioEnvironment"]
