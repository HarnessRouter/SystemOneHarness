<h1 align="center">
  <img src=".github/images/systemone-harness-logo.png" alt="System One Harness" width="680">
  <br>
  <sub>The harness for System One models.</sub>
</h1>

<p align="center">
  <a href="https://github.com/HarnessRouter/SystemOneHarness" title="Star System One Harness on GitHub"><img src="https://img.shields.io/github/stars/HarnessRouter/SystemOneHarness?style=flat&amp;logo=github&amp;logoColor=white&amp;label=Stars&amp;labelColor=444c56&amp;color=285aff" alt="GitHub stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-285AFF?style=flat&amp;labelColor=444c56" alt="License: Apache 2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Version-0.3.1-285AFF?style=flat&amp;labelColor=444c56" alt="Version 0.3.1"></a>
  <a href="docs/reports/uhp-conformance-core-2026-09-19.json"><img src="https://img.shields.io/badge/UHP-Core-16824B?style=flat&amp;labelColor=444c56" alt="UHP conformance: Core"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-285AFF?style=flat&amp;labelColor=444c56" alt="Python 3.10 or newer"></a>
</p>

**Turn a System One decision model into an agent loop.** System One Harness observes an environment, compiles its finite action space into typed questions, gates each decision by confidence, executes the chosen action, and records the complete trace.

One model call per step. No generated actions. A probability on every transition.

The first supported model is [Jev](https://typesafe.ai) by TypeSafe, available through OpenRouter or TypeSafe directly.

<a href="https://github.com/HarnessRouter/SystemOneHarness" title="Star System One Harness on GitHub">
  <picture>
    <source media="(max-width: 600px)" srcset=".github/images/github-readme-star-cta-mobile.svg">
    <img src=".github/images/github-readme-star-cta-desktop.svg" width="100%" alt="Help build the System One ecosystem. Star this repo.">
  </picture>
</a>

> [!TIP]
> **Start here:** [Run the example](#quickstart) · [Understand the loop](#how-it-works) · [Connect an environment](#connect-an-environment) · [Read the design](docs/design.md)

## Quickstart

```sh
git clone https://github.com/HarnessRouter/SystemOneHarness.git
cd SystemOneHarness
pip install -e .

export OPENROUTER_API_KEY=sk-or-...   # or TYPESAFE_API_KEY=...
s1 run --env order:ship_fastest_gift
```

This runs the built-in order fulfilment environment against the live model:

```text
goal: Order B-220 is a gift: note it, then ship it by the fastest carrier.
model: ~typesafe/jev-latest
    0  add_note(note='gift')              p=0.96  241 ms
    1  pick_item(item='scarf')            p=1.00  269 ms
    2  pack()                             p=0.99  166 ms
    3  choose_carrier(carrier='express')  p=0.98  151 ms
    4  ship()                             p=0.93  152 ms

status=completed reason=environment_terminal steps=5 wall=0.98s cost=$0.000208
```

Each step shows the selected action, its weakest required probability, and the model round trip. The final line records how the run ended, how long it took, and what it cost.

## What it provides

| | |
|---|---|
| **Finite actions** | The model chooses only from actions and parameter values declared by the environment. |
| **Confidence gates** | Read, write, and destructive actions can require different probability thresholds. |
| **Explicit outcomes** | Every run ends as completed, incomplete, failed, or cancelled with a structured reason. |
| **Complete traces** | State, questions, distributions, verdicts, results, latency, and usage are recorded step by step. |
| **Pluggable environments** | Drive Python processes, MCP servers, or web pages with the same controller. |
| **UHP compatibility** | Serve the loop through the Unified Harness Protocol for streaming, continuation, cancellation, and discovery. |

### Real-time environments

Games, live feeds, and other moving environments can return `"realtime": true`. The controller then treats a refused or repeated decision as a clock tick, keeps the model's history short, and lets the last action remain active until it changes.

## How it works

```text
            ┌──────────────────────────────────────────────────────────┐
            │                        controller                        │
 goal ────► │ observe ─► compile ─► encode ─► decide ─► gate ─► execute │ ────► trace
            │    ▲                                           │         │
            │    └────────────── environment ◄───────────────┘         │
            └──────────────────────────────────────────────────────────┘
```

1. **Observe.** The environment reports text, structured fields, candidates, and terminal state.
2. **Compile.** The available actions become typed `choice`, `noul`, and `score` questions.
3. **Encode.** Goal, observation, bounded history, and memory become a state within the model budget.
4. **Decide.** The model answers the action, its parameters, and the goal check in one request.
5. **Gate.** The weakest required probability must clear the selected action's risk threshold.
6. **Execute.** The environment applies the action and returns the next state.

`finish` and `escalate` are actions, not generated prose. The controller always knows why it stopped.

[Read the measured design and architecture →](docs/design.md)

## Connect an environment

Choose the smallest boundary that fits your system.

| Environment | Use it when | Start with |
|---|---|---|
| **Action space + process** | You own a local program or service loop. | `s1 run --actions actions.yaml --env-cmd "python3 env.py" --goal "..."` |
| **MCP server** | Your tools already expose enumerable inputs over MCP. | `s1 run --mcp "python -m your_server" --goal "..."` |
| **Browser** | The task is expressed through DOM controls in Chrome. | `s1 run --browser --headless --start-url https://example.com --goal "..."` |
| **Python** | You want an in-process integration. | Subclass `Environment` and implement `observe()` and `execute()`. |

### Declare an action space

An action space is YAML or the same structure in Python. Every parameter must be enumerable.

```yaml
instructions: >-
  Move the order to shipped, or cancel it when the goal says so.

actions:
  choose_carrier:
    description: Select a carrier for the packed order.
    risk: write
    params:
      carrier:
        from: available_carriers
  ship:
    description: Hand the packed order to the selected carrier.
    risk: destructive

gate:
  read: 0.5
  write: 0.6
  destructive: 0.8
  finish: 0.5
```

Parameters can use fixed `choices`, observation `candidates`, a boolean `flag`, or ordered `levels`. Free text is rejected because a System One model does not generate text.

[Open the complete example →](systemone_harness/examples/order_fulfilment.yaml)

### Use an MCP server

The harness lists an MCP server's tools, compiles supported schemas into actions, and explains every unsupported tool instead of silently dropping it.

```sh
pip install -e ".[mcp]"
s1 tools --mcp "python -m systemone_harness.envs.order_mcp"
s1 run --mcp "python -m systemone_harness.envs.order_mcp --scenario ship_fastest_gift" \
  --goal "Order B-220 is a gift. Ship it by the fastest carrier."
```

An `observe` tool provides state. An optional `reset` tool starts a run. Every other compatible tool becomes an action. MCP annotations determine whether the action is read, write, or destructive.

### Drive a browser

The browser environment uses [Browser Use](https://github.com/browser-use/browser-use) to turn visible DOM controls into a finite action space. Text comes from named values supplied by the caller. The model chooses values by name and never writes them.

```sh
pip install -e ".[browser]"
s1 run --browser --headless --start-url https://example.com/book \
  --text name=Customer --text email=user@example.com \
  --goal "Book a table at 19:30 with a window seat."
```

[Browser setup, measurements, and limits →](docs/browser-use.md)

## Serve over UHP

Expose any configured loop as a [Unified Harness Protocol](https://unifiedharnessprotocol.org) server:

```sh
export OPENROUTER_API_KEY=sk-or-...
s1 serve --api-key choose-a-secret --port 8710
```

```text
input                  → goal
function_call          → selected action
function_call_output   → environment result
reasoning              → distribution and gate verdict
previous_response_id   → continued environment and history
```

Streaming emits each item as it happens. Cancellation lets the current model step finish and records it. The included report passes all 40 checks in the UHP `core` conformance class.

[View the conformance report →](docs/reports/uhp-conformance-core-2026-09-19.json)

## Measured, not implied

Five live runs per scenario on 2026-09-19 with `typesafe/jev-1.13-20260917` through OpenRouter:

| Scenario | Goal met | Mean steps | Mean model latency | Mean wall time | Cost per run |
|---|---:|---:|---:|---:|---:|
| Ship by cheapest carrier | 5/5 | 6.0 | 241 ms | 1.45 s | $0.000265 |
| Ship fastest and add gift note | 5/5 | 5.0 | 199 ms | 0.99 s | $0.000214 |
| Cancel a fraudulent order | 5/5 | 1.0 | 197 ms | 0.20 s | $0.000044 |

The benchmark proves the controller, compiler, gate, and model can complete these small deterministic tasks. It does not claim the same result for ambiguous state, arithmetic, dates, or long irrelevant context.

[Inspect the raw benchmark rows →](docs/reports/bench-2026-09-19.json)

## Command line

```text
s1 run    Run one goal against a built-in, process, MCP, or browser environment
s1 tools  Inspect how an MCP server compiles into supported actions
s1 serve  Expose a configured loop as a UHP server
s1 bench  Run the built-in live benchmark
```

Use `s1 <command> --help` for every option. Add `--json trace.json` to `run`, `tools`, or `bench` when you need machine-readable output.

## Test

```sh
pip install -e . pytest
pytest -q tests
```

The suite covers action compilation, unsupported inputs, state truncation, confidence gates, every terminal reason, cancellation, continuation, MCP, browser actions, and the UHP server. Recorded model answers keep the default suite deterministic and keyless.

## Project status

| | |
|---|---|
| Version | `0.3.1` |
| Models | Jev through OpenRouter or TypeSafe directly |
| UHP | `core`, 40 of 40 checks |
| Environments | Python, stdio, MCP, browser, and real-time loops |
| Python | 3.10 or newer |

Next milestones are a `systemone` base in [HarnessRouter](https://github.com/HarnessRouter/harnessrouter), skills as loadable actions, the Chrome side panel as a plain UHP client, and the UHP `extended` conformance class.

## Resources

| Goal | Resource |
|---|---|
| Understand the model and architecture | [Design of record](docs/design.md) |
| Configure the browser environment | [Browser guide](docs/browser-use.md) |
| Try the protocol client | [Chrome extension](extension/README.md) |
| Review benchmark evidence | [Benchmark report](docs/reports/bench-2026-09-19.json) |
| Review protocol evidence | [UHP conformance report](docs/reports/uhp-conformance-core-2026-09-19.json) |

## License

System One Harness is licensed under [Apache 2.0](LICENSE).
