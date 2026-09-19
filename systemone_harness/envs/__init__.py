"""Environments that ship with the harness: a deterministic workflow for tests and the benchmark,
and a stdio adapter that makes any process an environment."""
from .order_workflow import OrderWorkflow
from .stdio import StdioEnvironment

__all__ = ["OrderWorkflow", "StdioEnvironment"]
