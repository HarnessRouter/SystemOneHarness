# Browser use with a System One model

What others built, what this harness does, and what was measured. 2026-09-19.

## What the demos with Jev have in common

Every public browser demo on Jev is the same shape, whatever the runtime:

1. **The page becomes a bounded table of controls.** The DOM is read for visible interactive
   elements, each given an index, a role and an accessible name; visible text rides along, capped.
   No screenshots: the model reads structure. Caps sit at 250 controls, under the model's 255.
2. **One request picks the operation and the target.** A choice over the operations feasible on
   this page, and a choice per operation over its targets, answered in the same round trip.
3. **Typed text never comes from Jev.** Either the caller supplies values and Jev picks which
   field gets which value, or a small text model writes the string and Jev only chooses to type.
4. **Deterministic code executes.** Clicks, typing and keys go over the Chrome DevTools Protocol
   or as DOM events; nothing the model returns becomes a selector or a script.
5. **Termination is a choice.** `DONE` and `BLOCKED` are options in the operation question;
   three unchanged pages in a row count as stuck.

| project | runtime | how the browser is reached | text | notes |
|---|---|---|---|---|
| browser-use/jev-ultrafast (the Browser Use team's own) | Python | CDP through Browser Harness, the user's Chrome | a small model writes it | Google Flights search in 7.1 s, 101 protocol calls where the text-model agent needed 1,092 |
| zurfyx/jev-browser-skill-demo | one Node file, a Claude Code and Codex skill | CDP, `--remote-debugging-port` | caller-supplied `--text` and `--secret` | about 100 ms per decision; a secret is typed only into password fields |
| chy4pro/JevBrowserExt | Chrome extension, MV3 | content script observes, DOM events act; the loop runs in the service worker | a small model writes it | median 7 s over 17 tasks; re-checks below 50 percent |
| forvela/jev-agent-browser | Python over agent-browser | CDP | | escalation when nothing fits |
| fhshaik/typesafe-mario | Python over an NES emulator | not a browser | none | the game's telemetry summarised to text: hazards, gaps, jump timing; seven macros held for N frames |

The Mario project is the one to read for a game: it does not look at pixels or the DOM, it turns
the game's own state into a small literal text every N frames. That is a page-side adapter.

## What this harness does

`envs/browser.py` puts the state definition convention on top of the open-source Browser Use
package rather than writing a fifth DOM reader:

- **Observe** is Browser Use's `get_browser_state_summary`: its indexed page representation as the
  text, and its selector map as the candidates: controls to click, text fields, dropdown options,
  tabs, and the caller-supplied values by name.
- **Act** is Browser Use's action registry by index (click, input, select_dropdown, send_keys,
  scroll, go_back, switch), plus one primitive it lacks, `hold_key`, a key held for a duration over
  the protocol, so a page that reacts to held keys is driven without page-specific code.
- **Finish and escalate** are the harness's own terminal actions, as everywhere else in the loop;
  the gate is the harness's, by risk.
- **The same environment is an MCP server** (`envs/browser_mcp.py`), so a host that configures
  environments as MCP servers (HarnessRouter) runs it unchanged, and it compiles to the identical
  action space (tested).
- **The user's own Chrome** is reached over `cdp_url`: Chrome 144 and later allow a local agent
  connection from `chrome://inspect/#remote-debugging` after a consent dialog; older builds need
  `--remote-debugging-port`. Chrome accepts protocol connections from the machine only, so the
  environment process runs beside the browser and the harness reaches it there.

## Measured

Live model through OpenRouter, headless Chrome on a Mac, 2026-09-19.

**A booking form** (name, time dropdown, window-seat checkbox, submit), goal "Book a table for
Richard at 19:30 with a window seat.", the name supplied as `--text name=Richard`. The final
shape, three runs:

| run | outcome | actions | wall | weakest judgment per executed step |
|---|---|---|---|---|
| 1 | completed, goal-reached 0.92 | 4 | 5.2 s | type 0.91, select 0.77, check 0.99, book 0.78 (one refusal at 0.68 first) |
| 2 | completed | 4 | 4.6 s | type 0.93, select 0.77, check 0.99, book 0.70 |
| 3 | completed | 4 | 4.6 s | type 0.90, select 0.78, check 0.99, book 0.71 |

About 150 to 315 ms a model step; the rest is the page. The same task through the extension's
panel, values entered there: completed, four actions, 4.6 s, 9,147 input tokens.

It did not start that way. The runs before each change, all on the same page and goal:

| state given to the model | select step | book step | outcome |
|---|---|---|---|
| dropdown offered both as a click and through `select_option` | 0.65 to 0.68, refused | | stopped after one action |
| dropdown through `select_option` only | 0.71 to 0.77 | 0.55 to 0.69, two refusals | completed 2 of 3 |
| plus checked / empty / filled on each control's label | 0.61 to 0.69, refused | | stopped: the unchecked box now competed for first place |
| plus "take controls in page order, submit after every one is set" in the instructions | 0.74 to 0.75 | 0.55 to 0.69, two refusals | completed 2 of 3 |
| plus the dropdown's chosen value read from the live node, and one line "Set so far: Your name: 'Richard'; Time: '19:30'; Window seat: checked." | 0.77 to 0.78 | 0.68 to 0.78 | completed 3 of 3 |

Every step was a state change, never a gate change. Three lessons, all the one the order desk
taught, that the model reads literally:

- One act, one name. A dropdown reachable two ways split the belief between the ways. A label
  wrapping a checkbox is a second name for the checkbox and is not offered.
- Order is a fact the model needs stated. Two correct next steps halve each other's probability,
  and a gate on the top probability then refuses a step that is right; "take them in the order
  they appear" turns the tie into a rule.
- What is set must be said. A dropdown's page representation lists its options and not its
  choice, and a value inside a control's markup was not read as done; one plain line was.

What remains: after everything is set, a checked box still draws about a third of the belief
when the goal names it ("with a window seat"), so the submit click sits near the write gate and
passes on the first or second attempt.

**The Super Mario page** (a canvas game), goal "keep Mario running to the right and jump over
anything in the way": the model saw the score line and nothing else, held the right arrow nine
times at 0.86 to 0.91, and the loop stopped as `repeated_action` when the observation stopped
changing. Correct behaviour, and the honest boundary: a DOM observation cannot see a canvas.
Driving that page well needs the page's own state as text, the way typesafe-mario reads the
emulator, which is an adapter for that page rather than something a general browser environment
can invent.

## The extension

The extension in `extension/` is a plain protocol client, on purpose: it signs in to a harness
server (this package's `s1 serve`, or HarnessRouter), lists the harnesses, takes a goal and the
values the page may need, starts a task and streams its items into a side panel, and can cancel.
The browser side is not in the extension: the environment process runs beside Chrome and reaches
it through Chrome's own agent connection. A second extension-hosted environment (content script
observation, DOM events) is possible and what JevBrowserExt does; it is not this one, because it
would be a fifth DOM reader where Browser Use already is one.
