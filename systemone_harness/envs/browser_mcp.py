"""The browser environment served as an MCP server, for hosts that configure environments as MCP
servers (HarnessRouter): `python -m systemone_harness.envs.browser_mcp [--http --port 8720]
[--cdp-url URL | --headless] [--start-url URL] [--text name=value ...]`.

The tools are the actions of envs/browser.py with the same descriptions, the candidate lists
named with `x-candidates`, and the server's instructions the same as the action space's; the
compiled action space is therefore the in-process one. Over stdio the server is a child of the
harness; over streamable HTTP it stays beside the browser while the harness runs elsewhere.
"""
import argparse
import os
import sys
from typing import Annotated, Literal

from pydantic import Field

from .browser import ACTION_SPACE, HOLD_KEYS, KEYS, BrowserEnvironment

A = ACTION_SPACE["actions"]


def build(env: BrowserEnvironment):
    import warnings
    warnings.filterwarnings("ignore", message=".*lifespan.*")
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    m = MCPServer("browser", instructions=ACTION_SPACE["instructions"], log_level="WARNING")

    def cand(action: str, param: str, source: str):
        return Annotated[str, Field(description=A[action]["params"][param]["instructions"],
                                    json_schema_extra={"x-candidates": source})]

    # built outside the annotations, where a linter reads a string as a forward reference
    Element, TextField, TextValue = cand("click", "element", "elements"), cand("type_text", "field", "text_fields"), cand("type_text", "value", "text_values")
    Option, Tab = cand("select_option", "option", "options"), cand("switch_tab", "tab", "tabs")

    @m.tool(annotations=ToolAnnotations(readOnlyHint=True), description="The page as it stands: its visible text with the controls numbered.")
    def observe() -> dict:
        return env.observe().to_dict()

    @m.tool(description="Open the start page, if one was given, for a new run.")
    def reset(goal: str = "") -> dict:
        env.reset(goal)
        return {"ok": True}

    @m.tool(description=A["click"]["description"])
    def click(element: Element) -> dict:
        return env.execute("click", {"element": element}).to_dict()

    @m.tool(description=A["type_text"]["description"])
    def type_text(field: TextField, value: TextValue) -> dict:
        return env.execute("type_text", {"field": field, "value": value}).to_dict()

    @m.tool(description=A["select_option"]["description"])
    def select_option(option: Option) -> dict:
        return env.execute("select_option", {"option": option}).to_dict()

    @m.tool(description=A["press_key"]["description"])
    def press_key(key: Annotated[Literal[tuple(KEYS)], Field(description=A["press_key"]["params"]["key"]["instructions"])]) -> dict:
        return env.execute("press_key", {"key": key}).to_dict()

    @m.tool(description=A["hold_key"]["description"])
    def hold_key(key: Annotated[Literal[tuple(HOLD_KEYS)], Field(description=A["hold_key"]["params"]["key"]["instructions"])],
                 duration: Annotated[str, Field(description=A["hold_key"]["params"]["duration"]["instructions"],
                                                json_schema_extra={"oneOf": [{"const": k, "description": v}
                                                                             for k, v in A["hold_key"]["params"]["duration"]["choices"].items()]})]) -> dict:
        return env.execute("hold_key", {"key": key, "duration": duration}).to_dict()

    @m.tool(annotations=ToolAnnotations(readOnlyHint=True), description=A["scroll"]["description"])
    def scroll(direction: Annotated[str, Field(description=A["scroll"]["params"]["direction"]["instructions"],
                                               json_schema_extra={"oneOf": [{"const": k, "description": v}
                                                                            for k, v in A["scroll"]["params"]["direction"]["choices"].items()]})]) -> dict:
        return env.execute("scroll", {"direction": direction}).to_dict()

    @m.tool(description=A["go_back"]["description"])
    def go_back() -> dict:
        return env.execute("go_back", {}).to_dict()

    @m.tool(annotations=ToolAnnotations(readOnlyHint=True), description=A["switch_tab"]["description"])
    def switch_tab(tab: Tab) -> dict:
        return env.execute("switch_tab", {"tab": tab}).to_dict()

    @m.tool(annotations=ToolAnnotations(readOnlyHint=True), description=A["wait"]["description"])
    def wait() -> dict:
        return env.execute("wait", {}).to_dict()

    return m


def parse_texts(items) -> dict:
    out = {}
    for it in items or []:
        k, _, v = str(it).partition("=")
        if k.strip():
            out[k.strip()] = v
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="browser_mcp", description="The browser environment as an MCP server.")
    p.add_argument("--cdp-url", default=os.environ.get("S1_CDP_URL") or None, help="attach to a running Chrome")
    p.add_argument("--headless", action="store_true", help="launch a headless Chrome instead")
    p.add_argument("--start-url", default=os.environ.get("S1_START_URL") or None)
    p.add_argument("--text", action="append", metavar="NAME=VALUE", help="a value the model may type by name, repeatable")
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8720)
    a = p.parse_args(argv)
    env = BrowserEnvironment(cdp_url=a.cdp_url, headless=a.headless, start_url=a.start_url, text_values=parse_texts(a.text))
    m = build(env)
    try:
        if a.http:
            m.run("streamable-http", host=a.host, port=a.port)
        else:
            m.run("stdio")
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
