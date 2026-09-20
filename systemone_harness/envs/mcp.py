"""An MCP server as the environment.

The harness holds the MCP client; the model never sees a tool. The server's tools are compiled to
actions once (see tools.py); at run time `observe` is called every step and the chosen action is
one tool call with the model's answers as arguments. The client lives on its own event loop in a
background thread so the controller stays synchronous. The SDK is the official `mcp` package,
2.x (the client is `mcp.client.client.Client`, the server `mcp.server.mcpserver.MCPServer`).

A server is declared the way HarnessRouter declares one:

    {"command": "python3", "args": ["-m", "my_env"], "env": {...}}          stdio
    {"url": "https://host/mcp", "headers": {...}}                          streamable HTTP
    {"url": "https://host/sse", "transport": "sse", "headers": {...}}      SSE

or as a plain command string, which is stdio.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import threading
from ..environment import Environment, Observation, Result
from ..tools import OBSERVE, RESET, ToolCatalogue, coerce, compile_tools


def _entry(server: dict | str) -> dict:
    if isinstance(server, str):
        argv = shlex.split(server)
        if not argv:
            raise ValueError("an empty command is not an MCP server")
        return {"command": argv[0], "args": argv[1:]}
    if not (server.get("url") or server.get("command")):
        raise ValueError("an MCP server entry needs a url or a command")
    return dict(server)


def _target(entry: dict):
    """What the SDK's Client connects to, built from what the caller declared and never inferred:
    a URL string is streamable HTTP; a declared SSE server gets the SSE transport; headers ride an
    HTTP client of their own because a bare URL takes none; a command is stdio."""
    url = str(entry.get("url") or "").strip()
    headers = {str(k): str(v) for k, v in (entry.get("headers") or {}).items()} or None
    if url:
        if str(entry.get("transport") or "").lower() == "sse":
            from mcp.client.sse import sse_client
            return sse_client(url, headers=headers)
        if headers:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
            return streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers,
                                                                              timeout=httpx2.Timeout(30, read=300)))
        return url
    from mcp import StdioServerParameters
    return StdioServerParameters(command=str(entry["command"]), args=[str(a) for a in (entry.get("args") or [])],
                                 env=entry.get("env") or None, cwd=entry.get("cwd"))


def _parse(result) -> tuple[dict | None, str, bool]:
    """A CallToolResult as (structured dict if any, text, is_error)."""
    structured = getattr(result, "structured_content", None)
    texts = [b.text for b in (getattr(result, "content", None) or []) if getattr(b, "text", None) is not None]
    text = "\n".join(texts)
    data = structured if isinstance(structured, dict) else None
    if data is not None and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]        # the SDK's wrapper for an unannotated dict return
    if data is None and text.strip().startswith("{"):
        try:
            loaded = json.loads(text)
            data = loaded if isinstance(loaded, dict) else None
        except ValueError:
            data = None
    return data, text, bool(getattr(result, "is_error", False))


class McpEnvironment(Environment):
    def __init__(self, server: dict | str, observe_tool: str = OBSERVE, reset_tool: str = RESET,
                 timeout: float = 120.0):
        from mcp.client.client import Client      # the optional dependency, checked where it is needed
        self.entry = _entry(server)
        self.observe_tool, self.reset_tool, self.timeout = observe_tool, reset_tool, timeout
        self.tools: list[dict] = []
        self.instructions: str = ""
        self._client = None
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="s1-mcp")
        self._thread.start()
        self._future = asyncio.run_coroutine_threadsafe(self._serve(Client), self._loop)
        self._ready.wait(timeout)
        if self._error is not None:
            self.close()
            raise RuntimeError(f"could not open the MCP environment: {self._error}") from self._error
        if self._client is None:
            self.close()
            raise RuntimeError(f"the MCP environment did not answer within {timeout} s")

    async def _serve(self, Client) -> None:
        try:
            async with Client(_target(self.entry), raise_exceptions=False) as client:
                self.instructions = str(getattr(client, "instructions", None) or "")
                listed = await client.list_tools()
                self.tools = [{"name": t.name, "description": t.description or "", "title": t.title,
                               "inputSchema": t.input_schema or {},
                               "annotations": t.annotations.model_dump(exclude_none=True, by_alias=True) if t.annotations else None}
                              for t in listed.tools]
                self._client = client
                self._stop = asyncio.Event()
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the constructor or the caller
            self._error = exc
            self._ready.set()
        finally:
            self._client = None

    # ── the compiled view ──
    def catalogue(self, instructions: str | None = None, gate: dict | None = None, escalate: bool = True) -> ToolCatalogue:
        cat = compile_tools(self.tools, instructions=self.instructions if instructions is None else instructions,
                            gate=gate, escalate=escalate, observe_tool=self.observe_tool, reset_tool=self.reset_tool)
        if not cat.observe:
            raise RuntimeError(f"the MCP server has no {self.observe_tool!r} tool; an environment must be observable")
        self._catalogue = cat
        return cat

    # ── the environment contract ──
    def _call(self, name: str, args: dict | None = None):
        if self._client is None:
            raise RuntimeError(f"the MCP environment is closed{': ' + str(self._error) if self._error else ''}")
        fut = asyncio.run_coroutine_threadsafe(self._client.call_tool(name, args or {}), self._loop)
        return fut.result(self.timeout)

    def reset(self, goal: str) -> None:
        if any(t["name"] == self.reset_tool for t in self.tools):
            schema = next(t["inputSchema"] for t in self.tools if t["name"] == self.reset_tool)
            args = {"goal": goal} if "goal" in (schema.get("properties") or {}) else {}
            data, text, err = _parse(self._call(self.reset_tool, args))
            if err:
                raise RuntimeError(f"reset failed: {text}")

    def observe(self) -> Observation:
        data, text, err = _parse(self._call(self.observe_tool))
        if err:
            raise RuntimeError(f"observe failed: {text}")
        d = data or {}
        return Observation(text=str(d.get("text") or text), fields=d.get("fields") or {},
                           candidates=d.get("candidates") or {}, terminal=bool(d.get("terminal")),
                           artifacts=[str(a) for a in (d.get("artifacts") or [])])

    def execute(self, action: str, params: dict) -> Result:
        schema = next((t["inputSchema"] for t in self.tools if t["name"] == action), {})
        data, text, err = _parse(self._call(action, coerce(params, schema)))
        d = data or {}
        return Result(ok=(not err) and bool(d.get("ok", True)), text=str(d.get("text") or text),
                      fields=d.get("fields") or {}, artifacts=[str(a) for a in (d.get("artifacts") or [])],
                      terminal=bool(d.get("terminal")))

    def close(self) -> None:
        if self._stop is not None and self._client is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            try:
                self._future.result(10)
            except BaseException:  # noqa: BLE001 - closing is best effort
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
