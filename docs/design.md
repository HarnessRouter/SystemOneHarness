# System One Harness: design

A harness for System One models. The first target is Jev, TypeSafe AI's System One model, reached
through OpenRouter. The harness is open source and will plug into HarnessRouter as a base.

This document is the design of record. Section 1 is what the model is, measured. Section 2 derives
the harness from that. Sections 3 to 9 are the architecture, the loop, the action space, skills and
tools, UHP, providers, and the first release. Section 10 lists the decisions still open.

## 1. What a System One model is, measured

Everything below is from TypeSafe's documentation, OpenRouter's model endpoint, or one live call made
on 2026-09-19 through OpenRouter. Nothing is inferred.

### 1.1 The call

One endpoint, one request shape, one response shape. No messages, no tokens streamed, no text
generated.

```
POST https://api.typesafe.ai/v1/systemone            (TypeSafe direct)
POST https://openrouter.ai/api/alpha/decisions       (OpenRouter, beta since 2026-09-18)
Authorization: Bearer <key>
```

```json
{
  "model": "~typesafe/jev-latest",
  "state": { "goal": "…", "observation": "…", "history": ["…"] },
  "questions": {
    "next_action":  { "type": "choice", "instructions": "…", "criteria": { "a": "…", "b": "…" } },
    "goal_reached": { "type": "noul",   "instructions": "…" },
    "progress":     { "type": "score",  "instructions": "…", "criteria": ["not started", "some", "almost", "done"] }
  }
}
```

The response, verbatim from the live call (389 ms end to end):

```json
{
  "model": "typesafe/jev-1.13-20260917",
  "answers": {
    "next_action":  { "type": "choice", "choice": "set_party_size",
                      "probabilities": { "finish": 0, "set_party_size": 0.73, "select_time_slot": 0.27, "search_again": 0, "submit_booking": 0 },
                      "confidence": 0.66 },
    "goal_reached": { "type": "noul", "noul": 0.05 },
    "progress":     { "type": "score", "score": 0.97, "legend": { "0": "not started", "1": "some steps done", "2": "almost done", "3": "done" },
                      "probabilities": { "0": 0.06, "1": 0.91, "2": 0.03, "3": 0 }, "confidence": 0.91 }
  },
  "usage": { "input_tokens": 543, "output_tokens": 93, "cost": 2.2806e-05 },
  "id": "gen-dec-1789855664-Eflg9ypRmKmvcf7BWJgA",
  "provider": "TypeSafe"
}
```

### 1.2 The three primitives

| Type | Asks | Returns |
|---|---|---|
| `choice` | pick one of N named options | the option, a probability per option, a confidence |
| `noul` | yes or no | a probability in 0 to 1 |
| `score` | a level on an ordered scale | a probability-weighted value, a probability per level, a confidence |

Every question carries `instructions`; `choice` and `score` carry `criteria` (the options or the
levels, each with a description, which may itself be structured: what it is, what it is not,
examples). Many questions ride one request and are answered in parallel in a single forward pass.
There is no multi-select primitive; a set is composed from one `noul` per member, which is what
TypeSafe's own function-calling cookbook does.

### 1.3 Limits

| Limit | Value | Source |
|---|---|---|
| State per request | 32k tokens (64k total with questions) | TypeSafe models page; OpenRouter context_length 32000 |
| Options per choice | up to 255 | TypeSafe announcement |
| Latency | 70 to 500 ms; 389 ms measured | announcement; live call |
| Price | $0.042 per million input tokens, output free | models page; OpenRouter pricing |
| Rate limit | 250,000 tokens per second, 1,200 requests per minute | models page |
| Input | text only: string, object, or array of text | models page |
| Models | `jev-1.13.0`; `jev-latest` and `jev-preview` alias it | models page |

### 1.4 What it cannot do, in its own words

From TypeSafe's "Jev 1.13 jaggedness" page. Each row is a constraint the harness is built around.

| Weakness | The harness's answer |
|---|---|
| No text generation | The harness never asks for text. Every value the loop needs is a choice over candidates the harness found in code. |
| Math, counting, numeric representations | Arithmetic lives in the harness; the model sees computed numbers or named buckets. |
| Literal reading, indirection, double negatives | Questions are written directly and name the state fields they read. |
| Large irrelevant state | The harness filters in code and sends only what a decision needs; 32k is a hard ceiling anyway. |
| Score interpolation | Scores are thresholds, never reconstructed numbers. |
| Wrong choice at high confidence is still possible | Confidence gates action; risk decides the threshold. |

### 1.5 What follows

A System One model is a decision function: state in, typed decision with probability out, in a
fixed time. It does not plan, generate, or remember. Everything else an agent needs is the
harness's job. That is the whole design.

## 2. First principles: what a harness for a decision function is

An LLM harness supplies a loop around a generator: the model writes the next action as text, the
harness parses and runs it. Remove generation and the harness has to supply three things the LLM
harness left to the model:

1. **The action space.** The model cannot invent an action, so the harness declares every feasible
   action and every feasible parameter value before the call. This is the object at the centre of
   the design, and it is exactly what Richard called the attributes or skills that enumerate the
   feasible actions.
2. **The state encoding.** The model cannot go and look, so the harness observes the environment and
   writes what it saw into `state`, in the shape and size the model decides well on.
3. **The transition.** The model cannot act, so the environment executes the decision and reports
   what happened.

Put together, the harness is a controller over a finite state machine: the environment is the
transition function, the model is the policy, the action space is the alphabet, and the loop runs
until a terminal state. Richard's finite-state-automaton framing is the right one; the refinement is
that the policy is a probabilistic function of the observed state, so every transition carries a
distribution over alternatives, which the harness can use (fan-out, hesitation, escalation).

Two consequences decide the rest of the design:

- **Every step is one request.** Because questions are answered in parallel, the action, its
  parameters, and the loop's own guard questions (done? stuck? confident?) ride one call. A step is
  one round trip of a few hundred milliseconds and a few hundred tokens. That is the property that
  makes this harness different from an LLM harness by two orders of magnitude on both axes, and the
  loop is designed to never spend a second call where one will do.
- **The harness owns judgment about its own judgment.** The model reports a distribution; the
  harness decides what confidence is enough for which action. A read-only action can run at 0.6; a
  destructive one waits for 0.9 or asks. That policy is data on the harness, not code in the model.

## 3. Vision

The near release is a loop. The far vision is a layer.

**The reflex layer.** Most of what an agent does between its rare hard decisions is routine: which
button, which branch, which record, is this done, is this safe. Today every one of those costs an
LLM call of seconds and cents. A System One harness makes each of them a call of milliseconds and
micro-cents, with a calibrated probability attached. Anything with a declared action space can be
driven this way: a browser (Browserbase runs browser agents on Jev for fractions of a cent), a game
(the model plays Doom in real time), a workflow engine, a device, a trading loop, a triage queue.

**System One under System Two.** The two kinds of model are complements, and the two harnesses
should be too. A System Two harness (Claude Code, Codex, Hermes, any HarnessRouter base) plans,
writes, and explains; it also *compiles*: given a task and an environment, it can write the action
space, the state encoder, and the guard thresholds that a System One harness then runs a thousand
times. In the other direction, the System One loop escalates when no action clears its threshold:
a typed door to a System Two harness that returns a decision, a new action, or a rewritten policy.
The HarnessRouter gateway is where the two meet: one task, two bases, routed by the shape of each
step.

**The measured claim.** The harness ships with its own benchmark: the same environment driven by
this loop and by an LLM harness, reporting steps to goal, wall time, cost, and the error rate at
each confidence threshold. A claim about speed or cost that is not measured on the same task is a
claim this project does not make.

**Where it lives.** A HarnessRouter base named `systemone`, with Jev as its first model family and
any provider that speaks the System One wire (TypeSafe direct, OpenRouter, others as they appear).
The console shows a System One task as a sequence of decisions with their distributions, which no
LLM harness can show.

## 4. Architecture

```
                 ┌────────────────────────────────────────────────────────────┐
  goal, budget   │                        Controller                          │
 ───────────────▶│  observe ─▶ encode ─▶ ask ─▶ gate ─▶ execute ─▶ record ─┐   │
                 │     ▲                                                  │   │
                 │     └──────────────────── until terminal ◀─────────────┘   │
                 └──────┬──────────────┬──────────────┬──────────────┬────────┘
                        │              │              │              │
                 ┌──────▼─────┐ ┌──────▼──────┐ ┌─────▼──────┐ ┌─────▼──────┐
                 │ Environment│ │ ActionSpace │ │  Provider  │ │   Trace    │
                 │ observe()  │ │ compile()   │ │ decide()   │ │ steps,     │
                 │ execute()  │ │ gate policy │ │ Jev via    │ │ usage,     │
                 │ terminal?  │ │             │ │ OpenRouter │ │ distribu-  │
                 └────────────┘ └─────────────┘ └────────────┘ │ tions      │
                                                               └────────────┘
```

| Component | Owns | Never does |
|---|---|---|
| **Environment** | `observe() -> Observation`, `execute(action, params) -> Result`, `is_terminal()`. Where the world is. | Decide anything. |
| **ActionSpace** | The declared actions, their parameters as finite sets, their risk level, the guard questions. Compiles itself into one `questions` map. | Grow at run time from model output. |
| **StateEncoder** | Turns an Observation plus the goal plus a bounded history into the `state` object, within the token budget, with only the fields the questions read. | Send raw dumps. |
| **Provider** | One `decide(state, questions) -> answers` over the System One wire, with retries on 429/529. | Hold prompts or policy. |
| **Gate** | Confidence thresholds per risk level; the "no action clears the bar" outcome. | Run anything. |
| **Controller** | The loop, the budgets, termination, stuck detection, cancellation. | Encode or execute. |
| **Trace** | Every step: state hash and size, questions, answers with full distributions, chosen action, gate verdict, execution result, latency, usage. | Summarise away the distribution. |
| **Escalation** (later) | The typed door to a System Two harness when the gate refuses. | Be silent about it. |

The separation is what lets the same loop drive a browser, a workflow, or a game: only the
Environment and the ActionSpace change.

## 5. The loop

### 5.1 States

```
          ┌─────────┐
  start ─▶│ observe │◀────────────────────────────────┐
          └────┬────┘                                  │
               ▼                                       │
          ┌─────────┐   one request: action, params,   │
          │  decide │   done?, plus guard questions     │
          └────┬────┘                                  │
               ▼                                       │
          ┌─────────┐  below threshold ──▶ [stuck] or escalate
          │  gate   │                                   │
          └────┬────┘                                  │
               ▼                                       │
          ┌─────────┐   finish chosen ──▶ [done]        │
          │ execute │───────────────────────────────────┘
          └─────────┘   error ──▶ [failed]
                        budget ──▶ [incomplete]
                        cancel ──▶ [cancelled]
```

Terminal states, and the UHP status each maps to: `done` (`completed`), `incomplete` (a step or
time budget ended it), `stuck` (the gate refused N times in a row, or the same state and action
repeated; `incomplete` with a reason), `failed` (the environment or provider raised), `cancelled`.

### 5.2 One step

1. **Observe.** `env.observe()` returns an Observation: text and structured fields, plus any
   candidate lists the environment can enumerate (the visible buttons, the open slots, the records
   on screen). Candidates are how free text becomes a choice.
2. **Encode.** The StateEncoder builds `state = {goal, observation, history, memory}`: the goal as
   given, the observation filtered to the fields the action space reads, the last K steps as
   `"<action>(<params>) -> <result summary>"` lines, and any scalar memory the environment keeps.
   Numbers are pre-computed and bucketed in code. The encoder measures the state's size and drops
   history first, then observation detail, to stay under the budget (default 24k tokens, leaving
   room for the questions inside the 32k ceiling).
3. **Ask.** One request with:
   - `next_action`: a `choice` over every enabled action plus `finish` and, when escalation is
     configured, `escalate`. Criteria are the actions' descriptions.
   - one question per parameter of every action, asked for every action at once (parallel; the
     answers of actions not chosen are ignored). A parameter is a `choice` over its declared
     values or over the environment's candidates, a `noul` for a flag, or a `score` for a bucket.
     Following TypeSafe's cookbook, an optional parameter also carries a `stated` noul, so an
     absent parameter takes its default rather than a guess.
   - `goal_reached`: a `noul`. It is asked every step, so `finish` is checked against a second
     reading rather than trusted alone.
   - optional guards the action space declares: `is_safe`, `needs_human`, and the like.
4. **Gate.** The chosen action's risk level selects a threshold; the harness compares the
   `next_action` confidence and the weakest parameter probability (TypeSafe's rule: the least
   certain judgment, not the product) against it. Below the bar: no action runs; the step is
   recorded as refused; a refusal counter advances toward `stuck`; with escalation configured, the
   door opens instead.
5. **Execute.** `env.execute(action, params)` returns a Result: outcome text, structured fields,
   artifacts written, and whether the environment considers itself terminal.
6. **Record.** The Trace appends the step whole, distributions included. Usage is summed from the
   provider's `usage`.
7. **Terminate or loop.** `finish` chosen and `goal_reached` above its threshold: `done`. `finish`
   chosen and `goal_reached` low: recorded as a disagreement, one more step, then `done` if it
   repeats (the model is allowed to be sure). Budgets, stuck, cancel, and errors as in §5.1.

### 5.3 Budgets and stuck detection

`max_steps` (default 100; a step is one request, so the default is small compared to an LLM
harness and still finishes in under a minute), `timeout_seconds`, a refusal streak (default 3),
and a repetition guard (the same action with the same parameters on the same state hash, twice).
Every budget ends the loop as `incomplete` with a machine-readable reason; none is silent.

### 5.4 Cancellation

The controller checks a cancel flag between steps; a step in flight completes (it is a few hundred
milliseconds) and its result is recorded, then the loop ends `cancelled`. Nothing is killed
mid-execution, so an environment is never left half-acted.

## 6. The action space and its compiler

### 6.1 Declaring actions

```yaml
actions:
  select_time_slot:
    description: Pick a time slot on the booking page
    risk: write
    params:
      slot:
        from: observation.slots            # a candidate list the environment provides
        instructions: Which slot best matches the goal's time?
  set_party_size:
    description: Set the number of guests
    risk: write
    params:
      size:
        choices: {"1": "one", "2": "two", "3": "three", "4": "four", "5-8": "five to eight"}
  submit_booking:
    description: Press the confirm button
    risk: destructive
  search_again:
    description: Go back and search for another restaurant
    risk: read
gate:
  read: 0.5
  write: 0.7
  destructive: 0.9
```

Three kinds of parameter, and no fourth:

| Kind | Declared as | Compiles to |
|---|---|---|
| Fixed set | `choices: {value: description}` | one `choice` |
| Candidates from the state | `from: <observation field>` | one `choice` whose criteria are the candidates the environment enumerated this step, capped at 255 |
| Flag or bucket | `flag: true` or `levels: [...]` | a `noul` or a `score` |

A parameter that would need free text (a search query, a message body, a file's contents) has no
place here, on purpose. The environment either enumerates candidates for it, or the action does not
exist for this harness. That rule is the honest boundary of a System One model, and the compiler
refuses a declaration that crosses it rather than shipping an action that cannot be filled.

### 6.2 Compiling to questions

The compiler produces one `questions` map per step: `next_action` (with `finish` and, if
configured, `escalate` appended), every parameter question of every action (names prefixed by
action so nothing collides), `goal_reached`, and the declared guards. It enforces the ceilings:
at most 255 options per choice (candidate lists are truncated with the truncation recorded in the
trace), and the whole request under the token budget. Descriptions become `criteria` verbatim, so
what the operator wrote is what the model reads.

Per-action parameter questions are asked every step whether or not the action is chosen. That costs
tokens, not calls; one call per step is the property this harness protects. An action space with
many actions and many parameters can opt into a two-request step (choose the action, then fill its
parameters), the trade-off being one more round trip.

### 6.3 Where action spaces come from

- **Declared** by the operator, as above. The first release.
- **From a skill.** A skill bundle carries `actions.yaml` beside `SKILL.md`; installing the skill
  installs its actions, and `SKILL.md` is the text the model may load into state on demand (§7.1).
- **From an MCP server.** Tool schemas compile to actions where every parameter is enumerable
  (§7.2). The compiler runs when the harness is configured, not per step.
- **From a System Two harness.** The far vision: an LLM writes the action space for a task and this
  harness runs it.

## 7. Skills and tools

### 7.1 Skills: loaded on demand, into state

Richard's instinct is right and the model's limits make it a rule: a skill's text belongs in
`state` only while it is needed. The state ceiling is 32k tokens and TypeSafe documents that
irrelevant state degrades accuracy, so skills are not concatenated into every request.

- The action space gains one `load_skill` action whose parameter is a choice over the installed
  skills (name and one-line description as the criteria). The model chooses it when a skill's
  guidance would help; the harness reads `SKILL.md` and its references into a `memory.skill` slot
  of the state for the following steps, replacing any previous skill (one skill live at a time).
- A skill that carries `actions.yaml` contributes actions permanently; a skill that carries only
  prose contributes guidance on demand.
- A skill's scripts are not run by the model (it cannot compose a command line). A script becomes
  an action only through `actions.yaml`, which names the script and declares its parameters as
  finite sets; the environment runs it.

### 7.2 Tools: compiled to state definitions, never streamed

An MCP tool is a function with a JSON-schema input and a text-producing call loop; a System One
model can neither compose the arguments as text nor stream a call. Richard's proposal to block the
tool-call protocol and compile tools into state definitions is the design:

1. At configuration time the harness lists the server's tools once.
2. Each tool whose input schema is fully enumerable compiles to an action: `enum` becomes a
   `choice`, `boolean` a `noul`, a bounded integer a `score` over buckets, an `array` of enums a set
   of nouls. A string parameter compiles only when the declaration maps it to a candidate list from
   the state (`from:`), otherwise the tool is recorded as `unsupported: needs free text` in the
   harness's tool catalogue, visible to the operator, never silently dropped.
3. At run time the environment invokes the MCP tool with the filled arguments and returns its
   result text as the step's observation. The MCP client is the harness's, not the model's.
4. `disabledTools` is enforced by omission from the action space, which is as hard as enforcement
   gets: the model cannot choose what it was never offered.

This keeps every UHP promise about tools (§4.1 and §4.3 of the Harnesses chapter) and states the
one it cannot keep, in the catalogue, where a client can read it.

## 8. UHP

### 8.1 Can it be conformant

Yes at Core, without stretching the protocol, and at Full with skills as the carrier for action
spaces. The protocol tests transport, objects, and honesty, not what the agent is made of; a
harness whose output is a decision trace is a valid harness. The mapping:

| UHP | System One Harness |
|---|---|
| `POST /v1/responses` with `input` text | The text is the goal. `metadata` may carry a structured initial state and an environment id. |
| `instructions` | Standing guidance appended to the goal in `state` for this task. |
| `model` | `~typesafe/jev-latest` or `typesafe/jev-1.13`; `available` computed from the provider key. |
| `output[]` `function_call` | One per executed action: `name` = the action, `arguments` = the filled parameters as JSON. |
| `output[]` `function_call_output` | The environment's result for that call, matched by `call_id`. |
| `output[]` `reasoning` | A summary per step: the chosen action, its probability, the runner-up, the gate verdict. This is the model's distribution, summarised, which is what the reasoning item is for. |
| `output[]` `message` | The terminal sentence, rendered by the harness from the terminal state: "Goal reached after 4 steps." or "Stopped: no action cleared the confidence threshold after 3 attempts." Harness prose, never presented as the model's. |
| `usage` | The sum of the provider's per-call usage. Never fabricated; `null` if a provider reports none. |
| `status` | `completed`, `incomplete` (budget or stuck, with `incomplete_details.reason`), `failed`, `cancelled`, per §5.1. |
| `metadata.session_id` | A session holds the environment's state and the trace; `previous_response_id` continues it with a new goal on the same environment. |
| Streaming | Each step emits `output_item.added` and `done` for its call and result as it happens; the terminal message streams as text deltas. Progressive by construction: a step is a few hundred milliseconds, so the stream is live. |
| Cancel | The controller's cancel flag; the in-flight step completes. |
| `tools`, `include` | Accepted, ignored, reported in `metadata.ignored_fields`, as the spec requires. |
| Skills (Full) | Folders round-trip byte for byte; `SKILL.md` is loadable on demand; `actions.yaml` compiles. |
| MCP servers (Full) | Compiled at configuration; unreachable servers do not fail the task; the catalogue states what compiled. |
| Plugins | Optional at every class; later, as skills plus MCP in one folder. |
| Files (Extended) | A text file as task input becomes part of the initial state; artifacts the environment writes are the session's files. |

The friction Richard expected is real and has one source: UHP's task surface is text in, text out,
because every harness before this one generated text. Two places absorb it. The terminal `message`
is harness prose about the outcome, which principle 4 of the architecture allows (a value the
server knows) and principle 2 demands be a fact rather than a rendering, so it names the terminal
state and the count and nothing more. And the conformance suite's own task prompt ("Reply with
exactly: ok") runs as a goal against the harness's built-in environment; the loop ends, the
response is well formed, and the suite measures the protocol, as it should.

### 8.2 The conformance plan

- First release: the loop as a library plus a small UHP Core server around it, run against the
  conformance suite at `core` and reported in the README with the suite's own output. Core needs
  discovery, harness list and get, models, responses (both modes), sessions by
  `previous_response_id`, cancel, and the error envelope: a few hundred lines around the loop.
- Second: `extended` (file input as state, artifacts from the environment, session listing).
- Third: `full` with harness management, skills as action-space carriers, MCP compilation, and the
  measured badge on unifiedharnessprotocol.org through the conformance-measure workflow.

## 9. Providers

One wire, several hosts. The provider adapter is a base URL, a path, and a header.

| Provider | Endpoint | Model ids | Status |
|---|---|---|---|
| OpenRouter | `POST /api/alpha/decisions` | `~typesafe/jev-latest`, `typesafe/jev-1.13` | Beta since 2026-09-18; measured working. The first release's default. |
| TypeSafe direct | `POST /v1/systemone` | `jev-latest`, `jev-1.13.0` | Early access, waitlist. Same body and answers; `GET /v1/models` lists models. |
| Others | Whoever serves the System One wire | | Added as a base URL and path when they appear. |

The adapter retries 429 and 529 with exponential backoff (TypeSafe's guidance), records
`usage`, the served model name, and the provider's request id per step, and never holds a prompt.
Substitution is impossible by construction (the served model is in every answer), and the harness
reports the served name as `model`, as UHP requires.

## 10. The first release

Ship the loop; make it honest; measure it. Nothing that needs a second model.

**In:**

- Python package `systemone_harness` (Python 3.10+; `httpx` for the provider and `pyyaml` for action-space files are the only dependencies), with
  `ActionSpace`, `Environment`, `StateEncoder`, `Provider` (OpenRouter and TypeSafe direct),
  `Gate`, `Controller`, `Trace`.
- Declarative action spaces in YAML or code, the compiler with its ceilings and refusals.
- Two environments: a deterministic text workflow (an order-fulfilment state machine used by the
  tests and the benchmark) and a JSON-over-stdio environment adapter so any process can be an
  environment (`observe`, `execute`, `terminal` as three JSON lines).
- A CLI: `s1 run --env order[:scenario]` for the built-in environment, or
  `s1 run --actions <yaml> --env-cmd "<command>" --goal "…"` for any process that speaks the stdio
  protocol, printing each step as it happens and, with `--json`, the trace at the end.
- The UHP Core server (`s1 serve`) and the conformance report at `core` in the README.
- The benchmark script: steps, wall time, cost, and refusal rate on the built-in environment, with
  the numbers in the README rather than adjectives.
- Unit tests for the compiler (ceilings, refusals, prefixing, candidates), the encoder (budget,
  truncation order), the gate (thresholds by risk, weakest judgment), the controller (every
  terminal state), and a recorded-answers provider so the loop is tested without a key.

**Out, and why:** escalation to a System Two harness (needs the HarnessRouter door first), MCP
compilation (needs the catalogue surface), plugins, the console view. Each is a milestone with a
measurement attached, not a stub.

**Naming:** the package is `systemone_harness`, the CLI `s1`, the HarnessRouter base `systemone`.
The model family is Jev; the harness is not named after it, because the wire is TypeSafe's System
One shape and the next model that speaks it should need no rename.

## 11. Open decisions

1. **Where `escalate` goes in the first release.** The action can exist from day one (the model may
   choose it; the loop ends `incomplete` with reason `escalation_requested`) so that traces show
   how often a System One model asks for help, with the door itself wired later. Recommended: yes,
   as a terminal action, no callback.
2. **Parameter questions for every action every step, or two requests.** One request is the
   default for the property it protects; the two-request mode is a switch on the action space.
   Recommended: one, with the switch.
3. **The first demo environment beyond the text workflow.** A Playwright page (the booking flow
   from the live probe) shows the browser case that Browserbase already proves; a game shows the
   latency; a triage queue shows the business case. Recommended: the booking page, second release.
4. **Skill loading: one live skill or a small budgeted set.** One is simpler and matches the 32k
   ceiling; a set would let two skills combine. Recommended: one, until a trace shows the need.
5. **Language.** Python matches HarnessRouter's runner and TypeSafe's SDK; TypeScript matches the
   browser environments. Recommended: Python for the harness, environments in anything over the
   stdio adapter.

## 12. Sources

- TypeSafe AI, "Introducing System One Models & Jev": https://typesafe.ai/blog/introducing-system-one-models-and-jev
- TypeSafe docs: System One concept, State, Primitives, Advanced structure, Confidence, Models, API reference, Function calling cookbook, Skill suggestion cookbook, Agent skill, Jev 1.13 jaggedness: https://docs.typesafe.ai/llms.txt (index of every page)
- OpenRouter model endpoints for `typesafe/jev-1.13` and `~typesafe/jev-latest` (modality `text->decisions`, context 32000, pricing): https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints
- OpenRouter Decisions endpoint (`/api/alpha/decisions`, same body as System One), as documented by integrators: https://github.com/pydantic/pydantic-ai/issues/8552 and https://github.com/browser-use/jev-ultrafast/pull/18
- LangChain, "Building a harness with Jev": https://www.langchain.com/blog/building-a-harness-with-jev
- Sean Goedecke, "Jev means structured output is interesting again": https://www.seangoedecke.com/jev-means-structured-output-is-interesting-again/
- Unified Harness Protocol 2026-09-12, chapters Architecture, Lifecycle, Harnesses, Tasks, Streaming, and the conformance suite's check list.
- One live call through OpenRouter on 2026-09-19 (section 1.1).
