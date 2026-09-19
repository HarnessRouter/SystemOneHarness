"""Any process is an environment: one JSON object per line, in and out.

The harness writes a request line and reads one reply line:

    {"op": "reset", "goal": "..."}                      ->  {"ok": true}
    {"op": "observe"}                                   ->  {"text": "...", "fields": {}, "candidates": {}, "terminal": false}
    {"op": "execute", "action": "...", "params": {}}    ->  {"ok": true, "text": "...", "fields": {}, "artifacts": [], "terminal": false}
    {"op": "close"}                                     ->  (the process exits)

Anything else the process prints goes to its stderr, which the harness leaves alone. The protocol
is this small on purpose: an environment in any language is forty lines.
"""
from __future__ import annotations

import json
import shlex
import subprocess

from ..environment import Environment, Observation, Result


class StdioEnvironment(Environment):
    def __init__(self, command: str | list[str], cwd: str | None = None, timeout: float = 120.0):
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.timeout = timeout

    def _call(self, req: dict) -> dict:
        if self.proc.poll() is not None:
            raise RuntimeError(f"the environment process exited with {self.proc.returncode}")
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the environment process closed its output")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise RuntimeError(f"the environment answered with a line that is not JSON: {line[:200]!r}") from exc

    def reset(self, goal: str) -> None:
        self._call({"op": "reset", "goal": goal})

    def observe(self) -> Observation:
        d = self._call({"op": "observe"})
        return Observation(text=str(d.get("text") or ""), fields=d.get("fields") or {},
                           candidates=d.get("candidates") or {}, terminal=bool(d.get("terminal")),
                           artifacts=list(d.get("artifacts") or []))

    def execute(self, action: str, params: dict) -> Result:
        d = self._call({"op": "execute", "action": action, "params": params})
        return Result(ok=bool(d.get("ok", True)), text=str(d.get("text") or ""), fields=d.get("fields") or {},
                      artifacts=list(d.get("artifacts") or []), terminal=bool(d.get("terminal")))

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self._call({"op": "close"})
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
