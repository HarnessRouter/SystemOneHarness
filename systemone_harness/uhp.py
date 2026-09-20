"""A UHP Core server around the loop.

The Unified Harness Protocol (unifiedharnessprotocol.org) is how a client drives any harness
through a server without knowing what the harness is made of. This server implements the Core
class: discovery, harness and model discovery, tasks in both modes, sessions by
`previous_response_id`, cancellation and the error model. It is one file over the standard
library so the harness stays light; it is not a general web server.

The mapping from a decision loop to UHP's text-shaped task surface:

  input text                  -> the goal
  each executed action        -> a `function_call` item and its `function_call_output`
  a refused or finish step    -> a `reasoning` item summarising the distribution and the gate
  the terminal sentence       -> one `message` item, harness prose about the outcome
  usage                       -> the provider's per-call usage, summed
  status                      -> completed | incomplete | failed | cancelled, as the loop ended
"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

from . import __version__
from .actions import ActionSpace
from .controller import Controller
from .environment import Environment
from .trace import Run, Step, reasoning_text

VERSIONS = ["2026-09-12"]
BASE = "systemone"
BASE_LABEL = "System One"
MODELS = ["~typesafe/jev-latest", "typesafe/jev-1.13"]
TERMINAL = {"completed", "failed", "incomplete", "cancelled"}


class UhpError(Exception):
    def __init__(self, status: int, code: str, message: str, etype: str = "invalid_request_error",
                 param: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.etype, self.param, self.detail = status, code, message, etype, param, detail


@dataclass
class HarnessDef:
    """A configured harness: an action space, an environment factory, a default model."""
    id: str
    name: str
    space: ActionSpace
    env_factory: Callable[[], Environment]
    default_model: str = MODELS[0]
    max_step: int | None = None
    timeout_seconds: int | None = None
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        return {"id": self.id, "object": "harness", "name": self.name, "base": BASE, "baseLabel": BASE_LABEL,
                "defaultModel": self.default_model, "systemPrompt": self.space.instructions, "mcpServers": [],
                "skills": [], "plugins": [], "disabledTools": [], "maxStep": self.max_step,
                "timeoutSeconds": self.timeout_seconds, "createdAt": self.created_at}


@dataclass
class Session:
    id: str
    harness_id: str
    env: Environment
    steps: list[Step] = field(default_factory=list)
    running: str | None = None            # the response id in flight, if any
    last_response_id: str | None = None


@dataclass
class Task:
    response: dict
    input: object
    session: Session
    controller: Controller | None = None
    events: list[dict] = field(default_factory=list)
    subscribers: list[queue.Queue] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    seq: int = 0
    items: list[dict] = field(default_factory=list)

    def emit(self, ev: dict) -> None:
        ev["sequence_number"] = self.seq
        self.seq += 1
        self.events.append(ev)
        for q in list(self.subscribers):
            q.put(ev)


class Server:
    """The state behind the handler: harnesses, sessions, tasks, and the provider factory."""

    def __init__(self, harnesses: list[HarnessDef], provider_factory: Callable[[str], object],
                 api_keys: set[str], provider_available: bool = True):
        self.harnesses = {h.id: h for h in harnesses}
        self.default_harness = harnesses[0].id
        self.provider_factory = provider_factory
        self.providers: dict[str, object] = {}
        self.api_keys = set(api_keys)
        self.provider_available = provider_available
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()

    # ── discovery ──
    def discovery(self) -> dict:
        return {"object": "uhp.discovery", "protocol": "uhp", "versions": VERSIONS, "default_version": VERSIONS[0],
                "conformance_class": "core",
                "capabilities": {"streaming": True, "sessions": True, "cancellation": True, "files_input": False,
                                 "files_output": False, "session_listing": False, "harness_management": False,
                                 "session_sharing": False, "idempotency": False, "plugins": False},
                "implementation": {"name": "System One Harness", "version": __version__}}

    def models(self) -> dict:
        return {"backends": {BASE: {"default": MODELS[0], "models": [
            {"id": m, "label": m, "backend": BASE, "available": self.provider_available, "default": m == MODELS[0]}
            for m in MODELS]}}}

    def harness(self, hid: str) -> HarnessDef:
        h = self.harnesses.get(hid)
        if not h:
            raise UhpError(404, "harness_not_found", "No harness with that id.", param="harness_id")
        return h

    def harness_models(self, hid: str) -> dict:
        h = self.harness(hid)
        return {"harness_id": h.id, "backend": BASE, "default": h.default_model, "fallback": h.default_model,
                "models": [{"id": m, "available": self.provider_available, "default": m == h.default_model} for m in MODELS]}

    # ── tasks ──
    def create(self, body: dict) -> Task:
        if "input" not in body:
            raise UhpError(422, "invalid_input", "`input` is required.", param="input")
        goal = _input_text(body["input"])
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        ignored = [f for f in ("tools", "include") if f in body]
        model = str(body.get("model") or "")
        if model and model not in MODELS:
            raise UhpError(422, "model_unavailable", f"model {model!r} is not served by this harness's backend",
                           param="model", detail={"available": MODELS})
        prev = body.get("previous_response_id")
        with self.lock:
            if prev:
                ptask = self.tasks.get(prev)
                if not ptask:
                    raise UhpError(404, "response_not_found", "No response with that id.", param="previous_response_id")
                session = ptask.session
                if meta.get("harness_id") and meta["harness_id"] != session.harness_id:
                    raise UhpError(409, "harness_mismatch",
                                   "previous_response_id belongs to a session on a different harness.",
                                   param="harness_id")
                if session.running:
                    raise UhpError(409, "session_busy", "A task is already running in this session.")
                hdef = self.harness(session.harness_id)
            else:
                hdef = self.harness(str(meta.get("harness_id") or self.default_harness))
                session = Session(id="hsess" + secrets.token_hex(16), harness_id=hdef.id, env=hdef.env_factory())
                self.sessions[session.id] = session
            rid = "resp_" + secrets.token_hex(16)
            model = model or hdef.default_model
            response = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "in_progress",
                        "error": None, "incomplete_details": None, "previous_response_id": prev or None,
                        "model": model, "output": [], "store": bool(body.get("store", True)), "usage": None,
                        "metadata": {"session_id": session.id, "harness_id": hdef.id,
                                     **({"ignored_fields": ignored} if ignored else {})}}
            task = Task(response=response, input=body["input"], session=session)
            self.tasks[rid] = task
            session.running = rid
            session.last_response_id = rid
        provider = self._provider(model)
        controller = Controller(hdef.space, session.env, provider,
                                max_steps=int(body.get("max_step") or hdef.max_step or 100),
                                timeout_seconds=body.get("timeout_seconds") or hdef.timeout_seconds,
                                on_step=lambda s: self._on_step(task, s))
        task.controller = controller
        threading.Thread(target=self._run, args=(task, controller, goal, str(body.get("instructions") or ""),
                                                 prev is not None), daemon=True).start()
        return task

    def _provider(self, model: str):
        if model not in self.providers:
            self.providers[model] = self.provider_factory(model)
        return self.providers[model]

    def _run(self, task: Task, controller: Controller, goal: str, instructions: str, continued: bool) -> None:
        session = task.session
        task.emit({"type": "response.created", "response": dict(task.response)})
        task.emit({"type": "response.in_progress", "response": dict(task.response)})
        try:
            run = controller.run(goal, reset=not continued, task_instructions=instructions, prior=list(session.steps))
        except Exception as exc:  # noqa: BLE001 - a crash is a failed task with a reason, never a hang
            run = Run(goal=goal, status="failed", reason="harness_error", error=f"{type(exc).__name__}: {exc}")
            run.finished_at = time.time()
        session.steps.extend(run.steps)
        self._finish(task, run)
        with self.lock:
            session.running = None
        task.done.set()

    def _on_step(self, task: Task, step: Step) -> None:
        n = step.index
        if step.verdict == "run":
            fc = {"id": f"fc_{n}", "type": "function_call", "call_id": f"call_{n}", "name": step.action,
                  "arguments": json.dumps(step.params, default=str), "status": "completed"}
            self._add_item(task, fc)
            out = (step.result or {}).get("text", "")
            fco = {"id": f"fco_{n}", "type": "function_call_output", "call_id": f"call_{n}", "output": out,
                   "status": "completed"}
            self._add_item(task, fco)
        else:
            rs = {"id": f"rs_{n}", "type": "reasoning", "status": "completed",
                  "summary": [{"type": "summary_text", "text": reasoning_text(step)}]}
            self._add_item(task, rs)

    def _add_item(self, task: Task, item: dict) -> None:
        idx = len(task.items)
        task.items.append(item)
        task.response["output"] = list(task.items)
        task.emit({"type": "response.output_item.added", "output_index": idx,
                   "item": {k: v for k, v in item.items() if k in ("id", "type", "status", "call_id", "name")} | {"status": "in_progress"}})
        task.emit({"type": "response.output_item.done", "output_index": idx, "item": item})

    def _finish(self, task: Task, run: Run) -> None:
        text = run.summary()
        idx = len(task.items)
        msg = {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": text, "annotations": []}]}
        task.emit({"type": "response.output_item.added", "output_index": idx,
                   "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}})
        task.emit({"type": "response.content_part.added", "item_id": "msg_1", "output_index": idx, "content_index": 0,
                   "part": {"type": "output_text", "text": "", "annotations": []}})
        task.emit({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": idx, "content_index": 0,
                   "delta": text})
        task.emit({"type": "response.output_text.done", "item_id": "msg_1", "output_index": idx, "content_index": 0,
                   "text": text})
        task.emit({"type": "response.content_part.done", "item_id": "msg_1", "output_index": idx, "content_index": 0,
                   "part": msg["content"][0]})
        task.items.append(msg)
        task.emit({"type": "response.output_item.done", "output_index": idx, "item": msg})
        r = task.response
        r["output"] = list(task.items)
        r["status"] = run.status
        r["model"] = run.served_model or r["model"]
        usage = run.usage if run.steps else None
        r["usage"] = ({**usage, "total_tokens": usage["input_tokens"] + usage["output_tokens"]} if usage else None)
        r["metadata"] = {**r["metadata"], "reason": run.reason, "steps": len(run.steps), "executed": run.executed}
        if run.status == "failed":
            r["error"] = {"type": "harness_error", "code": run.reason, "message": run.error or run.summary(), "param": None,
                          "detail": None}
        if run.status == "incomplete":
            r["incomplete_details"] = {"reason": run.reason}
        event = {"completed": "response.completed", "incomplete": "response.incomplete"}.get(run.status, "response.failed")
        task.emit({"type": event, "response": dict(r)})

    def get_task(self, rid: str) -> Task:
        t = self.tasks.get(rid)
        if not t:
            raise UhpError(404, "response_not_found", "No response with that id.", param="response_id")
        return t

    def cancel(self, rid: str) -> dict:
        t = self.get_task(rid)
        if t.response["status"] in TERMINAL:
            return dict(t.response)
        if t.controller:
            t.controller.cancel()
        return dict(t.response)

    def cancel_session(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            raise UhpError(404, "session_not_found", "No session with that id.", param="session_id")
        if s.running:
            return self.cancel(s.running)
        return {"session_id": sid, "status": "idle"}

    def delete(self, rid: str) -> dict:
        t = self.get_task(rid)
        if t.response["status"] not in TERMINAL:
            # deletion is not cancellation: the task keeps running, only the stored record goes
            t.response["store"] = False
        with self.lock:
            self.tasks.pop(rid, None)
        return {"id": rid, "object": "response", "deleted": True}


def _input_text(inp) -> str:
    if isinstance(inp, str):
        return inp
    parts = []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        for c in content or [] if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") in ("input_text", "text") and c.get("text"):
                parts.append(str(c["text"]))
    return "\n".join(parts)


# ── HTTP ──

def make_handler(server: Server):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet by default; the trace is the record
            pass

        # every response carries the version; errors use the envelope
        def _send(self, status: int, body: dict, extra: dict | None = None) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            self.send_header("UHP-Version", VERSIONS[0])
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, e: UhpError) -> None:
            self._send(e.status, {"error": {"type": e.etype, "code": e.code, "message": e.message, "param": e.param,
                                            "detail": e.detail}, "detail": e.message})

        def _version(self) -> None:
            v = self.headers.get("UHP-Version")
            if v and v not in VERSIONS:
                raise UhpError(400, "unsupported_protocol_version", f"UHP version {v!r} is not served here.",
                               detail={"supported": VERSIONS})

        def _auth(self) -> None:
            h = self.headers.get("authorization") or ""
            tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
            if not tok or tok not in server.api_keys:
                raise UhpError(401, "unauthorized", "A valid bearer token is required.", etype="authentication_error")

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw.strip():
                return {}
            try:
                d = json.loads(raw)
            except ValueError:
                raise UhpError(400, "invalid_json", "The request body is not JSON.")
            if not isinstance(d, dict):
                raise UhpError(422, "invalid_request", "The request body must be a JSON object.")
            return d

        def _route(self, method: str) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            parts = path.split("/")
            try:
                self._version()
                if method == "GET" and path == "/v1/uhp":
                    return self._send(200, server.discovery())
                if not path.startswith("/v1/"):
                    raise UhpError(404, "not_found", "No such route.")
                self._auth()
                if method == "GET" and path == "/v1/harnesses":
                    return self._send(200, {"harnesses": [h.to_dict() for h in server.harnesses.values()]})
                if method == "GET" and path == "/v1/models":
                    return self._send(200, server.models())
                if method == "GET" and len(parts) == 4 and parts[2] == "harnesses":
                    return self._send(200, server.harness(parts[3]).to_dict())
                if method == "GET" and len(parts) == 5 and parts[2] == "harnesses" and parts[4] == "models":
                    return self._send(200, server.harness_models(parts[3]))
                if method == "POST" and path == "/v1/responses":
                    return self._responses(self._body())
                if len(parts) >= 4 and parts[2] == "responses":
                    rid = parts[3]
                    if method == "GET" and len(parts) == 4:
                        return self._send(200, dict(server.get_task(rid).response))
                    if method == "GET" and len(parts) == 5 and parts[4] == "input_items":
                        t = server.get_task(rid)
                        return self._send(200, {"object": "list", "data": [
                            {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": _input_text(t.input)}]}]})
                    if method == "POST" and len(parts) == 5 and parts[4] == "cancel":
                        return self._send(200, server.cancel(rid))
                    if method == "DELETE" and len(parts) == 4:
                        return self._send(200, server.delete(rid))
                if method == "POST" and len(parts) == 5 and parts[2] == "sessions" and parts[4] == "cancel":
                    return self._send(200, server.cancel_session(parts[3]))
                raise UhpError(404, "not_found", "No such route.")
            except UhpError as e:
                self._error(e)
            except Exception as exc:  # noqa: BLE001 - never a stack trace on the wire
                self._error(UhpError(500, "internal_error", f"{type(exc).__name__}: {exc}"[:300], etype="server_error"))

        def _responses(self, body: dict) -> None:
            task = server.create(body)
            if body.get("stream"):
                return self._stream(task)
            if body.get("background"):
                return self._send(200, dict(task.response))
            task.done.wait()
            return self._send(200, dict(task.response))

        def _stream(self, task: Task) -> None:
            q: queue.Queue = queue.Queue()
            task.subscribers.append(q)
            for ev in list(task.events):        # what was emitted before we subscribed
                q.put(ev)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("UHP-Version", VERSIONS[0])
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            seen = -1
            while True:
                ev = q.get()
                if ev["sequence_number"] <= seen:
                    continue
                seen = ev["sequence_number"]
                chunk = f"data: {json.dumps(ev)}\n\n".encode()
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                if ev["type"] in ("response.completed", "response.incomplete", "response.failed"):
                    break
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            task.subscribers.remove(q)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def serve(server: Server, host: str = "127.0.0.1", port: int = 8710) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    httpd.daemon_threads = True
    return httpd
