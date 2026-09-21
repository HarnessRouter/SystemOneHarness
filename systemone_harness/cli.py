"""`s1`: run a goal against an environment, serve the loop over UHP, or benchmark it.

    s1 run --goal "Ship order A-104 with the cheapest carrier." [--env order:ship_cheapest]
    s1 run --actions my_actions.yaml --env-cmd "python3 my_env.py" --goal "…"
    s1 run --mcp "python3 -m my_env_server" --goal "…"
    s1 tools --mcp https://host/mcp
    s1 serve --api-key KEY [--port 8710]
    s1 bench [--runs 3]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .actions import ActionSpace
from .controller import Controller
from .encoder import StateEncoder
from .envs import BrowserEnvironment, McpEnvironment, OrderWorkflow, StdioEnvironment
from .provider import OpenRouterProvider, ProviderError, TypeSafeProvider, provider_from_env
from .trace import Step

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000   # TypeSafe's published price; output tokens are free


def _mcp_entry(a) -> dict:
    spec = a.mcp
    if spec.startswith(("http://", "https://")):
        entry: dict = {"url": spec}
        if a.mcp_transport:
            entry["transport"] = a.mcp_transport
        headers = {}
        for h in a.mcp_header or []:
            k, _, v = h.partition(":")
            if k.strip() and v.strip():
                headers[k.strip()] = v.strip()
        if headers:
            entry["headers"] = headers
        return entry
    return spec          # a command line; the environment splits it


def _config(a):
    """The configuration file (`--config`, or the older `--actions`), with skills compiled in."""
    from .config import Config
    path = getattr(a, "config", None) or getattr(a, "actions", None)
    if not path:
        return None
    return Config.load(path, getattr(a, "skills", None) or [])


def _overlay(path: str | None, cfg=None) -> dict:
    """With an MCP server the tools are the actions; the configuration still sets instructions and gate."""
    if cfg is None and path:
        from .config import Config
        cfg = Config.load(path)
    return cfg.overlay() if cfg is not None else {}


def _browser_env(a) -> BrowserEnvironment:
    from .envs.browser_mcp import browser_kwargs
    return BrowserEnvironment(**browser_kwargs(a))


def _env_and_space(a) -> tuple:
    cfg = _config(a)
    if getattr(a, "browser", False):
        env = _browser_env(a)
        space = BrowserEnvironment.action_space()
        for k, v in _overlay(None, cfg).items():
            setattr(space, k, v)
        return env, space
    if getattr(a, "mcp", None):
        env = McpEnvironment(_mcp_entry(a))
        cat = env.catalogue(**_overlay(None, cfg))
        for name, why in cat.unsupported.items():
            print(f"unsupported tool {name}: {why}", file=sys.stderr)
        return env, cat.space
    if getattr(a, "env_cmd", None):
        if cfg is None or cfg.space() is None:
            sys.exit("--env-cmd needs --config <yaml> with actions: the harness cannot guess an environment's actions")
        return StdioEnvironment(a.env_cmd), cfg.space()
    spec = getattr(a, "env", None) or "order:ship_cheapest"
    if spec.startswith("order"):
        scenario = spec.split(":", 1)[1] if ":" in spec else "ship_cheapest"
        env = OrderWorkflow(scenario)
        declared = cfg.space() if cfg is not None else None
        space = declared or OrderWorkflow.action_space()
        if cfg is not None and declared is None:
            for k, v in _overlay(None, cfg).items():
                setattr(space, k, v)
        return env, space
    sys.exit(f"unknown --env {spec!r}; use order[:scenario], --env-cmd, --mcp or --browser")


def _provider(a):
    """The model from the environment, or a scripted provider for a probe."""
    script = getattr(a, "script", None)
    if script:
        from .provider import ScriptProvider
        return ScriptProvider([x.strip() for x in script.split(",") if x.strip()])
    try:
        return provider_from_env(a.model)
    except ProviderError as exc:
        sys.exit(str(exc))


def _controller(a, env, space, cfg, provider, **kw) -> Controller:
    encoder = None
    if cfg is not None and cfg.encoder:
        encoder = StateEncoder(instructions=space.instructions, **{k: int(v) for k, v in cfg.encoder.items()
                                                                   if k in ("history_steps", "budget_tokens")})
    return Controller(space, env, provider, encoder=encoder, max_steps=a.max_steps, timeout_seconds=a.timeout,
                      config_version=cfg.version if cfg is not None else 0, **kw)


def cmd_tools(a) -> int:
    env = McpEnvironment(_mcp_entry(a))
    try:
        cat = env.catalogue(**_overlay(a.actions))
    finally:
        env.close()
    print(f"{len(cat.space.actions)} actions, {len(cat.unsupported)} unsupported")
    print(cat.table())
    if a.json:
        with open(a.json, "w") as f:
            json.dump(cat.to_dict(), f, indent=1)
    return 0


def _print_step(s: Step) -> None:
    if s.verdict == "run":
        out = (s.result or {}).get("text", "")
        print(f"  {s.index:3d}  {s.action}({', '.join(f'{k}={v!r}' for k, v in s.params.items())})"
              f"  p={s.action_confidence:.2f} weakest={s.weakest:.2f}  {s.latency_ms} ms\n       -> {out}")
    else:
        print(f"  {s.index:3d}  [{s.verdict}] {s.action}  p={s.action_confidence or 0:.2f}"
              + (f"  weakest={s.weakest:.2f} < {s.threshold:.2f}" if s.verdict == "refused" else "")
              + (f"  ({s.note})" if s.note else ""))


def cmd_run(a) -> int:
    env, space = _env_and_space(a)
    cfg = _config(a)
    goal = a.goal or getattr(env, "goal", None)
    if not goal:
        sys.exit("--goal is required for this environment")
    provider = _provider(a)
    print(f"goal: {goal}\nmodel: {provider.model}" + (f"\nconfig: v{cfg.version} {cfg.path}" if cfg is not None else ""))
    ctl = _controller(a, env, space, cfg, provider, on_step=_print_step, trace_path=a.json)
    t0 = time.time()
    run = ctl.run(goal)
    dt = time.time() - t0
    cost = run.usage["input_tokens"] * PRICE_PER_INPUT_TOKEN
    print(f"\n{run.summary()}\nstatus={run.status} reason={run.reason} steps={len(run.steps)} executed={run.executed} "
          f"wall={dt:.2f}s tokens_in={run.usage['input_tokens']} cost=${cost:.6f} model={run.served_model}")
    if hasattr(env, "goal_met"):
        print(f"goal_met={env.goal_met()}")
    if cfg is not None and cfg.objective:
        from .objective import evaluate
        print("metrics:", json.dumps(evaluate(run.to_dict(), cfg.objective)["metrics"]))
    if run.handoff:
        print(f"handoff: {run.handoff['reason']} at step {run.handoff['step']} (weakest {run.handoff['weakest']} < {run.handoff['threshold']})")
    if a.json:
        print(f"trace written to {a.json}")
    env.close()
    return 0 if run.status == "completed" else 1


def cmd_serve(a) -> int:
    from .uhp import HarnessDef, Server, serve
    keys = {k for k in (a.api_key or os.environ.get("S1_API_KEY") or "").split(",") if k}
    if not keys:
        sys.exit("an API key is required: --api-key KEY or S1_API_KEY")
    or_key, ts_key = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("TYPESAFE_API_KEY")
    if not (or_key or ts_key):
        sys.exit("no provider key: set OPENROUTER_API_KEY or TYPESAFE_API_KEY")

    def provider_factory(model: str):
        if or_key:
            return OpenRouterProvider(or_key, model)
        return TypeSafeProvider(ts_key, model.split("/")[-1])

    harnesses = []
    if getattr(a, "browser", False):
        from .envs.browser_mcp import browser_kwargs
        kw = browser_kwargs(a)
        harnesses.append(HarnessDef(id="chrn_browser", name=a.name or "Browser", space=BrowserEnvironment.action_space(),
                                    env_factory=lambda: BrowserEnvironment(**kw)))
    if getattr(a, "mcp", None):
        entry = _mcp_entry(a)
        probe = McpEnvironment(entry)
        try:
            cat = probe.catalogue(**_overlay(a.actions))
        finally:
            probe.close()
        for name, why in cat.unsupported.items():
            print(f"unsupported tool {name}: {why}", file=sys.stderr)
        harnesses.append(HarnessDef(id="chrn_" + _slug(a.name or "mcp"), name=a.name or "mcp", space=cat.space,
                                    env_factory=lambda entry=entry: McpEnvironment(entry)))
    elif a.actions and a.env_cmd:
        space = ActionSpace.from_yaml(a.actions)
        cmd = a.env_cmd
        harnesses.append(HarnessDef(id="chrn_" + _slug(a.name or "custom"), name=a.name or "custom", space=space,
                                    env_factory=lambda cmd=cmd: StdioEnvironment(cmd)))
    for scenario in OrderWorkflow.SCENARIOS:
        harnesses.append(HarnessDef(id="chrn_order_" + scenario, name=f"Order desk ({scenario})",
                                    space=OrderWorkflow.action_space(),
                                    env_factory=lambda sc=scenario: OrderWorkflow(sc)))
    server = Server(harnesses, provider_factory, keys)
    httpd = serve(server, a.host, a.port)
    print(f"UHP core server on http://{a.host}:{a.port}  harnesses: {', '.join(h.id for h in harnesses)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_bench(a) -> int:
    if getattr(a, "mcp", None) or getattr(a, "env_cmd", None) or getattr(a, "browser", False) or getattr(a, "env", None):
        return _bench_env(a)
    try:
        provider = provider_from_env(a.model)
    except ProviderError as exc:
        sys.exit(str(exc))
    rows = []
    for scenario in OrderWorkflow.SCENARIOS:
        for i in range(a.runs):
            env = OrderWorkflow(scenario)
            ctl = Controller(OrderWorkflow.action_space(), env, provider, max_steps=a.max_steps)
            t0 = time.time()
            run = ctl.run(env.goal)
            dt = time.time() - t0
            rows.append({"scenario": scenario, "run": i + 1, "status": run.status, "reason": run.reason,
                         "goal_met": env.goal_met(), "steps": len(run.steps), "executed": run.executed,
                         "refused": sum(1 for s in run.steps if s.verdict == "refused"),
                         "wall_s": round(dt, 2), "ms_per_step": round(dt * 1000 / max(len(run.steps), 1)),
                         "input_tokens": run.usage["input_tokens"],
                         "cost_usd": round(run.usage["input_tokens"] * PRICE_PER_INPUT_TOKEN, 6),
                         "model": run.served_model})
            print(f"{scenario:18s} run {i+1}: {run.status:10s} goal_met={env.goal_met()!s:5s} steps={len(run.steps)} "
                  f"wall={dt:.2f}s tokens={run.usage['input_tokens']}")
    print("\n| scenario | runs | goal met | ended by | steps (mean) | refused (mean) | ms per step (mean) | wall s (mean) | input tokens (mean) | cost per run |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for scenario in OrderWorkflow.SCENARIOS:
        rs = [r for r in rows if r["scenario"] == scenario]
        n = len(rs)
        ends: dict = {}
        for r in rs:
            ends[r["reason"]] = ends.get(r["reason"], 0) + 1
        ended = ", ".join(f"{k} {v}" for k, v in sorted(ends.items(), key=lambda kv: -kv[1]))
        print(f"| {scenario} | {n} | {sum(r['goal_met'] for r in rs)}/{n} | {ended} | {sum(r['steps'] for r in rs)/n:.1f} | "
              f"{sum(r['refused'] for r in rs)/n:.1f} | "
              f"{sum(r['ms_per_step'] for r in rs)/n:.0f} | {sum(r['wall_s'] for r in rs)/n:.2f} | "
              f"{sum(r['input_tokens'] for r in rs)/n:.0f} | ${sum(r['cost_usd'] for r in rs)/n:.6f} |")
    print(f"\nmodel: {rows[0]['model'] if rows else '?'}; price: ${0.042}/M input tokens, output free (TypeSafe's published price)")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1)
    return 0


def _browser_args(p) -> None:
    p.add_argument("--browser", action="store_true", help="a web page as the environment, on Browser Use")
    p.add_argument("--cdp-url", help="attach to a running Chrome (chrome://inspect or --remote-debugging-port)")
    p.add_argument("--headless", action="store_true", help="launch a headless Chrome instead of attaching")
    p.add_argument("--start-url", help="the page to open when a run starts")
    p.add_argument("--text", action="append", metavar="NAME=VALUE", help="a value the model may type by name, repeatable")
    p.add_argument("--canvas", nargs="?", const="auto", metavar="SELECTOR",
                   help="canvas mode: the largest canvas (or this selector) sampled into the state as a character grid")
    p.add_argument("--canvas-cells", default="64x32", metavar="COLSxROWS", help="the sample rate (default 64x32)")
    p.add_argument("--canvas-colors", type=int, default=12, help="palette size, 2 to 63 (default 12)")
    p.add_argument("--canvas-legend", action="append", metavar="#HEX=NAME", help="a colour's name, repeatable (#c84c0c=ground)")


def _mcp_args(p) -> None:
    p.add_argument("--mcp", help="an MCP server as the environment: a command (stdio) or a URL")
    p.add_argument("--mcp-transport", choices=["sse", "http"], help="for a URL; streamable HTTP unless sse")
    p.add_argument("--mcp-header", action="append", metavar="NAME: VALUE", help="a header for a URL server, repeatable")


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower())[:40]


def _bench_env(a) -> int:
    """K runs of one environment, one at a time, then the objective's report."""
    from .objective import report
    cfg = _config(a)
    out_dir = a.json_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    traces = []
    for i in range(a.runs):
        env, space = _env_and_space(a)
        goal = a.goal or getattr(env, "goal", None) or ""
        provider = _provider(a)
        path = os.path.join(out_dir, f"run-{i + 1}.json")
        ctl = _controller(a, env, space, cfg, provider, trace_path=path)
        t0 = time.time()
        run = ctl.run(goal)
        env.close()
        traces.append(run.to_dict())
        print(f"run {i + 1}: {run.status} {run.reason} steps={len(run.steps)} wall={time.time() - t0:.1f}s -> {path}")
    if cfg is not None and cfg.objective:
        _print_report(report(traces, cfg.objective))
    return 0


def cmd_report(a) -> int:
    """The objective's report over existing traces, for the outer loop."""
    from .config import Config
    from .objective import report
    cfg = Config.load(a.config)
    if not cfg.objective:
        sys.exit("the configuration declares no objective")
    traces = [json.load(open(p)) for p in a.traces]
    rep = report(traces, cfg.objective)
    _print_report(rep)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=1, default=str)
    return 0


def _print_report(rep: dict) -> None:
    print("\nscoreboard:", json.dumps(rep["scoreboard"]))
    for g in rep["failure_groups"][:8]:
        ex = g["examples"][0] if g["examples"] else {}
        print(f"  {g['count']:3d} x at {g['locus']}: run {ex.get('run')} step {ex.get('step')} {ex.get('action')}: {str(ex.get('text'))[:150]}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="s1", description="A harness for System One models.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one goal against an environment")
    r.add_argument("--goal")
    r.add_argument("--env", help="order[:scenario] (built in)")
    r.add_argument("--env-cmd", help="a command that speaks the stdio environment protocol")
    r.add_argument("--config", help="the configuration YAML: version, instructions, gate, encoder, tunables, objective, and actions when the environment is a command")
    r.add_argument("--actions", help="the same file, older name")
    r.add_argument("--skills", action="append", metavar="DIR", help="a directory of SKILL.md files compiled into the instructions, repeatable")
    r.add_argument("--script", help="a probe: a comma-separated action sequence chosen instead of the model")
    _mcp_args(r)
    _browser_args(r)
    r.add_argument("--model")
    r.add_argument("--max-steps", type=int, default=100)
    r.add_argument("--timeout", type=float)
    r.add_argument("--json", help="write the trace here")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("serve", help="serve the loop as a UHP core server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8710)
    s.add_argument("--api-key", help="bearer token(s) clients present, comma separated (or S1_API_KEY)")
    s.add_argument("--actions")
    s.add_argument("--env-cmd")
    s.add_argument("--name")
    _mcp_args(s)
    _browser_args(s)
    s.set_defaults(fn=cmd_serve)
    t = sub.add_parser("tools", help="show what an MCP server's tools compile to")
    _mcp_args(t)
    t.add_argument("--actions", help="instructions and gate overlay")
    t.add_argument("--json", help="write the catalogue here")
    t.set_defaults(fn=cmd_tools)
    b = sub.add_parser("bench", help="run the built-in scenarios and report the numbers")
    b.add_argument("--runs", type=int, default=3)
    b.add_argument("--model")
    b.add_argument("--max-steps", type=int, default=20)
    b.add_argument("--timeout", type=float)
    b.add_argument("--json", help="for the built-in scenarios: the rows")
    b.add_argument("--json-dir", help="for an environment: the traces, run-N.json")
    b.add_argument("--goal")
    b.add_argument("--env", help="order[:scenario] (built in)")
    b.add_argument("--env-cmd")
    b.add_argument("--config")
    b.add_argument("--actions")
    b.add_argument("--skills", action="append", metavar="DIR")
    b.add_argument("--script")
    b.add_argument("--mcp"); b.add_argument("--mcp-transport", choices=["sse", "http"]); b.add_argument("--mcp-header", action="append")
    b.add_argument("--browser", action="store_true"); b.add_argument("--cdp-url"); b.add_argument("--headless", action="store_true"); b.add_argument("--start-url")
    b.add_argument("--text", action="append"); b.add_argument("--canvas", nargs="?", const="auto"); b.add_argument("--canvas-cells", default="64x32")
    b.add_argument("--canvas-colors", type=int, default=12); b.add_argument("--canvas-legend", action="append")
    rp = sub.add_parser("report", help="the objective's scoreboard and failure groups over traces")
    rp.add_argument("--config", required=True)
    rp.add_argument("traces", nargs="+")
    rp.add_argument("--json")
    rp.set_defaults(fn=cmd_report)
    b.set_defaults(fn=cmd_bench)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
