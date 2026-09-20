# System One Harness

A harness for System One models: a controller that drives an environment through a finite action
space, one decision per step, a few hundred milliseconds and a fraction of a cent each.

A System One model is a decision function, not a text generator. You send it a state and a set of
typed questions (a choice among named options, a yes/no probability, a score on a legend) and it
returns an answer and a probability distribution for each, in one non-autoregressive pass. The
first such model is [Jev](https://typesafe.ai) by TypeSafe, served directly and through
[OpenRouter](https://openrouter.ai). This harness is the loop such a model needs: something to hold
the state, enumerate the actions, ask the questions, judge the confidence, execute the chosen action
against the world, and stop for the right reason.

The design, with the measurements it rests on, is in [docs/design.md](docs/design.md).

## Status

| | |
|---|---|
| Version | 0.2.0, the first loop and the browser |
| Unified Harness Protocol | conformant at `core`, 40 of 40 checks, suite 2026.9.12.post1 ([report](docs/reports/uhp-conformance-core-2026-09-19.json)) |
| Built-in benchmark | 15 of 15 goals met on the live model, in process and over MCP ([numbers](#benchmark)) |
| Models | `~typesafe/jev-latest` and `typesafe/jev-1.13` on OpenRouter; `jev-latest` on TypeSafe directly |
| Python | 3.10 or newer; depends on `httpx` and `pyyaml`; `.[mcp]` adds MCP environments, `.[browser]` adds the browser |

### Real-time environments

An environment whose world moves whether or not a decision is made (a game, a live feed) says so
with `"realtime": true` on its observations. The loop then treats a refused or repeated decision
as a tick with the last action standing rather than a reason to stop, and keeps the history it
shows the model short, since the state is what matters and tokens are latency. Actions in such an
environment are best momentary key states that stay set until changed; the loop is the clock.

## Quickstart

```sh
pip install -e .
export OPENROUTER_API_KEY=sk-or-...        # or TYPESAFE_API_KEY=...
s1 run --env order:ship_fastest_gift
```

That runs the built-in order-fulfilment environment against the live model. The transcript below
is a real one:

```
goal: Order B-220 is a gift: note it, then ship it by the fastest carrier.
model: ~typesafe/jev-latest
    0  add_note(note='gift')  p=0.96 weakest=0.96  241 ms
       -> Note added: gift.
    1  pick_item(item='scarf')  p=1.00 weakest=1.00  269 ms
       -> Picked scarf.
    2  pack()  p=0.99 weakest=0.99  166 ms
       -> Packed the parcel.
    3  choose_carrier(carrier='express')  p=0.98 weakest=0.98  151 ms
       -> Carrier set to express.
    4  ship()  p=0.93 weakest=0.93  152 ms
       -> Shipped by express.

The environment reached a terminal state after 5 actions.
status=completed reason=environment_terminal steps=5 executed=5 wall=0.98s tokens_in=4964 cost=$0.000208 model=typesafe/jev-1.13-20260917
goal_met=True
```

Each line is one step: the action the model chose with its parameters, the probability it put on
that action, the weakest of the judgments the step depended on, the round trip in milliseconds, and
what the environment said back. The cost is computed from the provider's reported usage at
TypeSafe's published price.

## How the loop works

```
            ┌──────────────────────────────────────────────────────────┐
            │                        controller                        │
            │                                                          │
 goal ──►   │  observe ─► compile ─► encode ─► decide ─► gate ─► execute │ ──► trace
            │     ▲          │                    │        │       │     │
            │     │       action space         model   thresholds  │     │
            │     └──────────────────── environment ◄──────────────┘     │
            └──────────────────────────────────────────────────────────┘
```

Every step is one request to the model, and the request carries everything the step could need:
the next action as a choice among the actions that are feasible right now, every parameter of every
feasible action as its own typed question, and a yes/no question on whether the goal is already
reached. The model answers all of them in parallel. The controller then reads only the parameters
of the action it chose.

1. **Observe.** The environment reports its state as text plus structured fields, and enumerates
   any candidate lists (the items still on the shelf, the carriers available).
2. **Compile.** The action space is turned into questions. An action whose candidate list is empty
   is not offered this step. A disabled action is withheld by omission, never by a rule the model
   has to obey.
3. **Encode.** Goal, instructions, observation, a bounded history and any carried memory become the
   state. The encoder measures itself against the model's budget and, if it must, drops the oldest
   history first, then clips the summary, then drops memory, then history, then the largest
   observation field, and records every cut in the trace.
4. **Decide.** One request. The provider retries transient failures with backoff.
5. **Gate.** The weakest judgment the step depends on (the action's probability and every
   parameter's) must clear the threshold for the action's risk level. `read` needs 0.5, `write`
   0.7 and `destructive` 0.9 by default; an action space can set its own. A refused step executes
   nothing and is recorded with the distribution the model gave.
6. **Execute.** The environment runs the action and reports the result. If it raises, the run
   fails with the environment's own words.

The loop ends for exactly one of these reasons, and the trace names it:

| status | reason | meaning |
|---|---|---|
| completed | `goal_reached` | the model chose `finish` and put at least the finish threshold on the goal being reached |
| completed | `finish_insisted` | the model chose `finish` twice in a row while doubting the goal; the controller stops rather than argue |
| completed | `environment_terminal` | the environment declared itself terminal (an order shipped, a booking confirmed) |
| incomplete | `max_steps`, `timeout` | a budget ran out |
| incomplete | `no_confident_action` | the gate refused three steps in a row |
| incomplete | `repeated_action` | the same action on an unchanged state twice running: the model is stuck |
| incomplete | `escalation_requested` | the model chose `escalate`: nothing offered fits, a person or a bigger model should look |
| failed | `environment_error`, `provider_error` | the world or the model was unreachable |
| cancelled | `cancelled` | the caller cancelled; the step in flight finishes and is recorded |

`finish` and `escalate` are always offered alongside the environment's actions. They are terminal
actions, not prose; the model does not write a closing message, the harness does.

## Declaring an action space

An action space is a YAML file (or the same dictionary in code). This is the built-in one,
abridged:

```yaml
instructions: >-
  You run a warehouse's order desk. Move the order to shipped, or cancel it when the goal says so.
  Ship only after every item is picked and the order is packed.

actions:
  pick_item:
    description: Pick one unpicked item off the shelf and put it in the order's bin.
    risk: write
    params:
      item:
        from: unpicked_items          # a candidate list the environment enumerates each step
        instructions: Which unpicked item to pick next?
  ship:
    description: Hand the packed parcel to the chosen carrier. Only after pack and choose_carrier.
    risk: destructive
  cancel_order:
    description: Cancel the order and return any picked items to the shelf.
    risk: destructive
    params:
      reason:
        choices:
          customer_request: the customer asked to cancel
          out_of_stock: an item cannot be fulfilled
          fraud: the order failed a fraud check
        instructions: Why is the order being cancelled?

gate:            # confidence the weakest judgment must clear, by risk; finish is the goal check
  read: 0.5
  write: 0.6
  destructive: 0.8
  finish: 0.5
```

A parameter is one of four kinds, and nothing else:

| kind | declared with | asked as |
|---|---|---|
| fixed choices | `choices: {value: meaning}` | a choice question |
| from candidates | `from: <candidate list name>` | a choice question over what the environment enumerated this step |
| flag | `flag: true` | a yes/no question |
| levels | `levels: [low, mid, high]` | a score question with that legend |

A parameter with none of these is free text, and the action space refuses to load: a System One
model cannot generate text, and the harness will not pretend otherwise. Optional parameters get a
second yes/no question, "is this stated?", and take their default when the model says no. A choice
may carry at most 255 options; a longer candidate list is cut to 255 and the cut is recorded.

The full example is [systemone_harness/examples/order_fulfilment.yaml](systemone_harness/examples/order_fulfilment.yaml).

## Writing an environment

Any process is an environment. The harness starts it, writes one JSON object per line on its stdin
and reads one JSON object per line from its stdout:

```
{"op": "reset", "goal": "..."}                     ->  {"ok": true}
{"op": "observe"}                                  ->  {"text": "...", "fields": {}, "candidates": {}, "terminal": false}
{"op": "execute", "action": "...", "params": {}}   ->  {"ok": true, "text": "...", "fields": {}, "artifacts": [], "terminal": false}
{"op": "close"}                                    ->  (the process exits)
```

`fields` are structured facts the model may need to read; `candidates` are the lists that `from:`
parameters draw on; `terminal: true` ends the run as completed. Anything the process prints to
stderr is left alone. Then:

```sh
s1 run --actions my_actions.yaml --env-cmd "python3 my_env.py" --goal "..."
```

In Python, subclass `Environment` and implement `observe()` and `execute(action, params)`; `reset`
and `close` are optional. The built-in `OrderWorkflow` in `systemone_harness/envs/order_workflow.py`
is the reference: the action-space declaration, three scenarios and the state machine in under two hundred lines.

One rule for any environment, measured on the built-in one: state the goal's own predicates
outright, every step, including the false ones. The model reads literally. When the order desk said
"packed" but never "not shipped", the model kept a tenth of its belief on the order being finished
and its confidence on `ship` fell to the gate; saying "Shipped: no" moved it clear. Details are in
the design document.

## An MCP server as the environment

Any MCP server can be the environment, with no action-space file: the harness lists the server's
tools once and compiles them into actions. This is how the harness plugs into a host that
configures environments as MCP servers, such as HarnessRouter.

```sh
pip install -e ".[mcp]"
s1 tools --mcp "python -m systemone_harness.envs.order_mcp"
s1 run   --mcp "python -m systemone_harness.envs.order_mcp --scenario ship_fastest_gift" \
         --goal "Order B-220 is a gift: note it, then ship it by the fastest carrier."
s1 serve --mcp https://host/mcp --mcp-header "Authorization: Bearer ..." --name booking --api-key ...
```

The convention a server follows, the "state definition" protocol:

| the server declares | the harness makes of it |
|---|---|
| a tool named `observe` returning `{text, fields, candidates, terminal}` | the observation, called every step |
| a tool named `reset` taking `{goal}` | the start of a run (optional) |
| every other tool | an action; its description is what the model reads |
| `readOnlyHint` / `destructiveHint` annotations | the action's risk: read, destructive, else write |
| a parameter with `enum` | a choice over the values |
| a parameter with `oneOf: [{const, description}]` | a choice over the values, each with its meaning |
| a `boolean` parameter | a yes/no flag |
| an `integer` with `minimum` and `maximum`, ten values or fewer | levels |
| a string parameter with `"x-candidates": "<list>"` | a choice over that candidate list in the observation |
| a parameter not in `required` | optional; its schema default applies when unstated |
| the server's `instructions` | the harness instructions (a YAML passed as `--actions` may override instructions and gate) |

A tool whose parameters cannot be answered this way (a free string, an array, an object) is not an
action. It is listed as unsupported with the reason, by `s1 tools` and in the served catalogue,
never dropped in silence. The order desk served this way:

```
$ s1 tools --mcp "python -m systemone_harness.envs.order_mcp"
6 actions, 0 unsupported
  pick_item                write        item: candidates
  pack                     write        (no parameters)
  choose_carrier           write        carrier: candidates
  ship                     destructive  (no parameters)
  cancel_order             destructive  reason: choices
  add_note                 read         note: choices
  protocol: observe=observe reset=reset
```

It compiles to the same action space as the YAML, and the model receives the same state at every
step as it does from the in-process environment; a test checks both. The server itself is fifty
lines over the official MCP SDK (`systemone_harness/envs/order_mcp.py`) and is the pattern for any
simulator or real surface you want the loop to drive.

## A web page as the environment

Any web page can be the environment, on the open-source [Browser Use](https://github.com/browser-use/browser-use)
project: it reads the page into an indexed representation, every control numbered with its role
and name, and executes by index over the Chrome DevTools Protocol. The harness turns that into the
state definition convention: numbered controls become candidate lists, the operations become nine
actions, and the text a form needs comes from you as named values the model picks by name and
never writes.

```sh
pip install -e ".[browser]"
s1 run --browser --headless --start-url https://example.com/book \
       --text name=Richard --text email=r@example.com \
       --goal "Book a table for Richard at 19:30 with a window seat."
s1 run --browser --cdp-url http://127.0.0.1:9222 --goal "..."     # your own Chrome
python -m systemone_harness.envs.browser_mcp --http --port 8720    # the same environment for a host
```

| action | what the model chooses |
|---|---|
| `click` | one numbered control |
| `type_text` | a text field, and which supplied value goes in it |
| `select_option` | an option of a dropdown (a dropdown is reachable this way only) |
| `press_key`, `hold_key` | a key from a fixed list; `hold_key` also a duration, for pages that react to held keys |
| `scroll`, `go_back`, `switch_tab`, `wait` | a direction, nothing, a tab, nothing |

Measured on 2026-09-19 with the live model: a booking form (name, time, window seat, submit)
completed in four actions in three runs of three, 4.6 to 5.2 s each, at 150 to 315 ms a model
step. What it took to get there is in [docs/browser-use.md](docs/browser-use.md): three changes to
what the page says to the model, none to the gate.

The boundary, measured the same day: a page drawn on a canvas gives a DOM observation nothing but
its DOM. On a Super Mario page the model saw the score line, held the right arrow nine times, and
the loop stopped when the observation stopped changing. Driving such a page well needs the page's
own state as text, which is an adapter for that page, not something a general browser environment
can invent.

To reach your own Chrome, allow the connection at `chrome://inspect/#remote-debugging` (Chrome 144
and later) or start Chrome with `--remote-debugging-port=9222`, then pass `--cdp-url`.

## The command line

```
s1 run    [--goal TEXT] [--env order[:scenario]] [--env-cmd CMD --actions YAML] [--mcp CMD_OR_URL]
          [--browser [--cdp-url URL | --headless] [--start-url URL] [--text NAME=VALUE ...]]
          [--model ID] [--max-steps N] [--timeout SECONDS] [--json TRACE_PATH]
s1 tools  --mcp CMD_OR_URL [--mcp-transport sse|http] [--mcp-header "Name: value"] [--actions YAML] [--json PATH]
s1 serve  --api-key KEY [--host HOST] [--port PORT] [--actions YAML --env-cmd CMD --name NAME | --mcp ... --name NAME | --browser ...]
s1 bench  [--runs N] [--model ID] [--max-steps N] [--json PATH]
```

`s1 run` exits 0 when the run completed and 1 otherwise. `--json` writes the whole trace: every
step's state as sent, the questions asked, the answers with their distributions, the verdict, the
result, the latency and the usage.

## Serving over the Unified Harness Protocol

`s1 serve` exposes the loop as a [UHP](https://unifiedharnessprotocol.org) server, so any UHP
client can drive it without knowing what is inside. The built-in scenarios are served as three
harnesses; `--mcp` or `--actions` with `--env-cmd` adds yours.

```sh
export OPENROUTER_API_KEY=sk-or-...
s1 serve --api-key choose-a-secret --port 8710
```

The mapping from a decision loop to UHP's task surface:

| in the loop | on the wire |
|---|---|
| the input text | the goal |
| each executed action | a `function_call` item and its `function_call_output` |
| a refused, finish or escalate step | a `reasoning` item with the distribution and the gate's verdict |
| the terminal sentence | one `message` item, written by the harness |
| the provider's usage, summed | `usage` |
| how the loop ended | `completed`, `incomplete` with `incomplete_details.reason`, `failed` with the error envelope, or `cancelled` |
| `previous_response_id` | the same environment, continued, with the earlier steps in the model's history |

Streaming emits every item as it happens with gapless sequence numbers; cancellation lets the step
in flight finish and records it. Reserved fields such as `tools` are accepted and reported in
`metadata.ignored_fields`, since this harness has no tools to give a text model.

The conformance suite ships with the protocol repository. The report in `docs/reports` came from:

```sh
python3 -m uhp_conformance.cli --base-url http://127.0.0.1:8710 --api-key choose-a-secret \
    --class core --plain --label "System One Harness 0.1.0" --json report.json
```

Result on 2026-09-19: 40 of 40 core checks passed, conformant at `core`. The `extended` and `full`
classes (files, session listing, harness management) are not claimed.

## Benchmark

`s1 bench` runs the built-in scenarios against the live model and prints the numbers. Five runs per
scenario on 2026-09-19, model `typesafe/jev-1.13-20260917` through OpenRouter:

| scenario | runs | goal met | ended by | steps (mean) | refused (mean) | ms per step (mean) | wall s (mean) | input tokens (mean) | cost per run |
|---|---|---|---|---|---|---|---|---|---|
| ship_cheapest | 5 | 5/5 | environment_terminal 5 | 6.0 | 0.0 | 241 | 1.45 | 6304 | $0.000265 |
| ship_fastest_gift | 5 | 5/5 | environment_terminal 5 | 5.0 | 0.0 | 199 | 0.99 | 5094 | $0.000214 |
| cancel_fraud | 5 | 5/5 | environment_terminal 5 | 1.0 | 0.0 | 197 | 0.20 | 1039 | $0.000044 |

Cost is input tokens at TypeSafe's published $0.042 per million; output tokens are free. The raw
rows are in [docs/reports/bench-2026-09-19.json](docs/reports/bench-2026-09-19.json).

What this does and does not show. The environment is small and deterministic and its actions are
described plainly, so a perfect score says the loop, the compiler and the gate are correct and that
the model reads a clear state well. It does not say the model will solve a task with ambiguous
state, arithmetic, dates, or long irrelevant context; TypeSafe documents those as its weak spots,
and the design document lists them. The benchmark is a floor to keep, not a ceiling reached.

The "ended by" and "refused" columns are there because the number moved during the day. Before the
observation said "Shipped: no", the model's confidence on `ship` sat on the 0.8 destructive gate and
two runs in five ended as `no_confident_action`, which is the loop refusing a destructive action it
was 76 percent sure of. That is the gate doing its job; the fix was to make the environment say
what the goal asks about, not to lower the gate. A run that ends that way under HarnessRouter is a
door for a person, never a silent failure.

## Tests

```sh
pip install -e . pytest
pytest -q tests
```

Thirty-nine tests: the compiler (free text refused, reserved names, the 255 ceiling, dynamic
feasibility, disabling by omission), the tool compiler (every enumerable shape, every unsupported
reason, type coercion), the encoder (state shape, truncation order), the gate (thresholds by risk,
weakest judgment, unstated optionals), the controller (every terminal reason, cancellation,
continuation), the MCP environment (the order desk over stdio compiles to the same actions and
ships the order), the browser (what a page is offered as, the actions on a served form, the MCP
server compiling to the same space; skipped without Chrome) and the UHP server over HTTP (discovery, auth, versions, blocking and streamed
tasks, continuation, cancel, delete). A recorded-answers provider stands in for the model, so the
suite needs no key and runs in about two seconds.

## What comes next

Each is a milestone with a measurement attached, in the order they unlock each other:

1. A `systemone` base in [HarnessRouter](https://github.com/HarnessRouter/harnessrouter), so this
   loop sits beside the System Two harnesses: the harness configuration's MCP servers are the
   environment, every action is an item in the task stream, and a run that ends in
   `escalation_requested` or `no_confident_action` opens a door for a person or a System Two harness.
2. Skills as an action: `load_skill` brings a named skill's text into the state for the steps that
   need it, since the state is capped and irrelevant state costs accuracy.
3. A Chrome extension that is a plain protocol client: sign in, pick a harness, give a goal, watch
   the actions stream, with the browser reached through Chrome's own agent connection.
4. The `extended` conformance class.

## Sources

The facts about the model that shaped this design come from TypeSafe's documentation and API
reference, OpenRouter's decisions endpoint, and one live call whose response is quoted in full in
the design document. Prices and limits are as published on 2026-09-19 and may change.

## License

Apache 2.0. See [LICENSE](LICENSE).
