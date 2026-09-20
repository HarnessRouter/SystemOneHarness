"""The order desk served as an MCP server, so the harness can be driven the way HarnessRouter drives
it: `python -m systemone_harness.envs.order_mcp [--scenario NAME]`, speaking stdio.

Every tool delegates to OrderWorkflow; the descriptions are the same declaration the YAML carries.
The risk hints are MCP's own annotations and the candidate lists are the `x-candidates` extension,
which is the whole state definition convention (see tools.py).
"""

import argparse
import os
import sys
from typing import Annotated

from pydantic import Field

from .order_workflow import ACTION_SPACE, OrderWorkflow

A = ACTION_SPACE["actions"]


def build(scenario: str):
    import warnings
    warnings.filterwarnings("ignore", message=".*lifespan.*")     # the SDK's own settings model, not ours
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    env = OrderWorkflow(scenario)
    m = FastMCP("order-desk", instructions=ACTION_SPACE["instructions"], log_level="WARNING")

    def choices(action: str, param: str) -> list[dict]:
        return [{"const": k, "description": v} for k, v in A[action]["params"][param]["choices"].items()]

    @m.tool(annotations=ToolAnnotations(readOnlyHint=True), description="The order as it stands.")
    def observe() -> dict:
        return env.observe().to_dict()

    @m.tool(description="Start the scenario over for a goal.")
    def reset(goal: str = "") -> dict:
        env.reset(goal or env.goal)
        return {"ok": True}

    @m.tool(description=A["pick_item"]["description"])
    def pick_item(item: Annotated[str, Field(description=A["pick_item"]["params"]["item"]["instructions"],
                                            json_schema_extra={"x-candidates": "unpicked_items"})]) -> dict:
        return env.execute("pick_item", {"item": item}).to_dict()

    @m.tool(description=A["pack"]["description"])
    def pack() -> dict:
        return env.execute("pack", {}).to_dict()

    @m.tool(description=A["choose_carrier"]["description"])
    def choose_carrier(carrier: Annotated[str, Field(description=A["choose_carrier"]["params"]["carrier"]["instructions"],
                                                     json_schema_extra={"x-candidates": "carriers"})]) -> dict:
        return env.execute("choose_carrier", {"carrier": carrier}).to_dict()

    @m.tool(annotations=ToolAnnotations(destructiveHint=True), description=A["ship"]["description"])
    def ship() -> dict:
        return env.execute("ship", {}).to_dict()

    @m.tool(annotations=ToolAnnotations(destructiveHint=True), description=A["cancel_order"]["description"])
    def cancel_order(reason: Annotated[str, Field(description=A["cancel_order"]["params"]["reason"]["instructions"],
                                                   json_schema_extra={"oneOf": choices("cancel_order", "reason")})]) -> dict:
        return env.execute("cancel_order", {"reason": reason}).to_dict()

    @m.tool(annotations=ToolAnnotations(readOnlyHint=True), description=A["add_note"]["description"])
    def add_note(note: Annotated[str, Field(description=A["add_note"]["params"]["note"]["instructions"],
                                            json_schema_extra={"oneOf": choices("add_note", "note")})]) -> dict:
        return env.execute("add_note", {"note": note}).to_dict()

    return m, env


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="order_mcp")
    p.add_argument("--scenario", default=os.environ.get("S1_ORDER_SCENARIO", "ship_cheapest"),
                   choices=sorted(OrderWorkflow.SCENARIOS))
    a = p.parse_args(argv)
    m, _ = build(a.scenario)
    m.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
