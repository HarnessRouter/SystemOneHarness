"""Any web page as the environment, on Browser Use.

The open-source Browser Use project (browser-use, MIT) already does the two hard parts of driving
a browser without a screenshot: it reads the page into an indexed representation, every control
numbered with its role and name, and it executes actions by index over the Chrome DevTools
Protocol. What a System One model needs on top is exactly the state definition convention: the
numbered controls become candidate lists, the operations become a finite action space, and the
text a form needs comes from the caller as named values the model picks by name and never writes.

    observe   the page's visible text with its controls numbered, plus candidates: the controls
              (for click), the text fields, the dropdown options, the tabs, the supplied values
    actions   click, type_text, select_option, press_key, hold_key, scroll, go_back, switch_tab, wait

`hold_key` is the one primitive Browser Use does not have: a key held for a duration, sent as a
key-down and a key-up over the protocol, which is how a page that listens to the keyboard (a game,
a slideshow, a map) is driven without any page-specific code. A page drawn on a canvas offers this
environment only what its DOM says; that is the honest reach of a DOM-based observation, stated
here rather than hidden.

The browser is either one this process launches (headless or not) or the user's own Chrome
reached over `cdp_url` (Chrome 144+ can allow a local agent connection from chrome://inspect,
older builds need --remote-debugging-port). One environment holds one Browser Use session on
its own event loop, so the controller stays synchronous.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import threading
from typing import Callable

from ..actions import ActionSpace
from ..environment import Environment, Observation, Result

KEYS = ["Enter", "Escape", "Tab", "Backspace", "Space", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
        "PageUp", "PageDown", "Home", "End"]
HOLD_KEYS = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space", "Shift"]
HOLD_SECONDS = {"short": 0.15, "medium": 0.4, "long": 0.8}
_KEY_CODES = {"ArrowLeft": (37, "ArrowLeft", "ArrowLeft"), "ArrowRight": (39, "ArrowRight", "ArrowRight"),
              "ArrowUp": (38, "ArrowUp", "ArrowUp"), "ArrowDown": (40, "ArrowDown", "ArrowDown"),
              "Space": (32, " ", "Space"), "Shift": (16, "Shift", "ShiftLeft"), "Enter": (13, "Enter", "Enter")}
TEXT_FIELD_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
TEXT_FIELD_TAGS = {"input", "textarea"}
NOT_TEXT_INPUT_TYPES = {"checkbox", "radio", "button", "submit", "reset", "file", "image", "range", "color", "hidden"}

ACTION_SPACE: dict = {
    "instructions": (
        "You operate a web page toward a goal. Each step you see the page's visible text with its "
        "controls numbered, and you choose one action: click a control, type one of the supplied values "
        "into a field, pick a dropdown option, press or hold a key, scroll, switch tab, or go back. "
        "When several controls need changing, take them in the order they appear on the page, and "
        "submit only after every one is set. Finish when the page shows the goal is done. Escalate when "
        "nothing offered can move it forward."),
    "actions": {
        "click": {"description": "Click one numbered control: a link, button, checkbox, tab or option.", "risk": "write",
                  "params": {"element": {"from": "elements", "instructions": "Which control to click?"}}},
        "type_text": {"description": "Type one of the supplied values into a text field, replacing what is there.",
                      "risk": "write",
                      "params": {"field": {"from": "text_fields", "instructions": "Which field to type into?"},
                                 "value": {"from": "text_values", "instructions": "Which supplied value belongs in it?"}}},
        "select_option": {"description": "Choose an option in a dropdown.", "risk": "write",
                          "params": {"option": {"from": "options", "instructions": "Which option, in which dropdown?"}}},
        "press_key": {"description": "Press one key once, on whatever has focus.", "risk": "write",
                      "params": {"key": {"choices": KEYS, "instructions": "Which key?"}}},
        "hold_key": {"description": "Hold one key down for a while, for a page that reacts to held keys.", "risk": "write",
                     "params": {"key": {"choices": HOLD_KEYS, "instructions": "Which key to hold?"},
                                "duration": {"choices": {"short": "about 0.15 s", "medium": "about 0.4 s", "long": "about 0.8 s"},
                                             "instructions": "How long?"}}},
        "scroll": {"description": "Scroll the page one screen.", "risk": "read",
                   "params": {"direction": {"choices": {"down": "further down the page", "up": "back up"},
                                            "instructions": "Which way?"}}},
        "go_back": {"description": "Go back to the previous page.", "risk": "write"},
        "switch_tab": {"description": "Bring another open tab to the front.", "risk": "read",
                       "params": {"tab": {"from": "tabs", "instructions": "Which tab?"}}},
        "wait": {"description": "Wait a second for the page to settle.", "risk": "read"},
    },
}


CANVAS_ALPHABET = ".abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# Runs in the page. Finds the canvas, draws it scaled onto an offscreen canvas of cols x rows (the
# browser's own box sampling), reads the pixels back, quantises each to 4 bits a channel, keeps the
# K most common colours and maps every other pixel to the nearest kept one. One character per cell,
# '.' for the most common colour, which is nearly always the background.
_CANVAS_JS = """(function(cols, rows, maxColors, selector, keep){
  var cs = selector ? [document.querySelector(selector)] : Array.prototype.slice.call(document.querySelectorAll('canvas'));
  cs = cs.filter(function(c){ return c && c.width > 0 && c.height > 0; });
  cs.sort(function(a, b){ return b.width * b.height - a.width * a.height; });
  var c = cs[0];
  if (!c) return null;
  var off = document.createElement('canvas'); off.width = cols; off.height = rows;
  var ctx = off.getContext('2d', {willReadFrequently: true});
  ctx.imageSmoothingEnabled = true;
  try { ctx.drawImage(c, 0, 0, cols, rows); } catch (e) { return {error: String(e)}; }
  var d = ctx.getImageData(0, 0, cols, rows).data;
  var n = cols * rows, keys = new Array(n), counts = {};
  for (var i = 0; i < n; i++) {
    var k = ((d[i*4] >> 4) << 8) | ((d[i*4+1] >> 4) << 4) | (d[i*4+2] >> 4);
    keys[i] = k; counts[k] = (counts[k] || 0) + 1;
  }
  var all = Object.keys(counts).map(function(k){ return [parseInt(k, 10), counts[k]]; });
  all.sort(function(a, b){ return b[1] - a[1]; });
  // colours the operator named come first, each as the nearest colour actually on the canvas
  // (within the 4-bit quantisation), then the most common colours up to the palette size
  var top = [], used = {};
  (keep || []).forEach(function(want){
    var wr = want >> 8, wg = (want >> 4) & 15, wb = want & 15, best = null, bd = 1e9;
    all.forEach(function(e){ var q = e[0], r = q >> 8, g = (q >> 4) & 15, b = q & 15;
      var dd = (r-wr)*(r-wr) + (g-wg)*(g-wg) + (b-wb)*(b-wb); if (dd < bd) { bd = dd; best = e; } });
    if (best && bd <= 3 && !used[best[0]]) { top.push(best); used[best[0]] = 1; }
  });
  for (var a = 0; a < all.length && top.length < maxColors; a++) { if (!used[all[a][0]]) { top.push(all[a]); used[all[a][0]] = 1; } }
  top.sort(function(a, b){ return b[1] - a[1]; });
  var alphabet = %ALPHABET%;
  var map = {};
  for (var t = 0; t < top.length; t++) map[top[t][0]] = alphabet[t];
  function nearest(k){
    var r = k >> 8, g = (k >> 4) & 15, b = k & 15, best = top[0][0], bd = 1e9;
    for (var t = 0; t < top.length; t++) {
      var q = top[t][0], tr = q >> 8, tg = (q >> 4) & 15, tb = q & 15;
      var dd = (r-tr)*(r-tr) + (g-tg)*(g-tg) + (b-tb)*(b-tb);
      if (dd < bd) { bd = dd; best = q; }
    }
    return best;
  }
  var lines = [];
  for (var y = 0; y < rows; y++) {
    var line = '';
    for (var x = 0; x < cols; x++) { var kk = keys[y*cols + x]; if (!(kk in map)) { kk = nearest(kk); keys[y*cols + x] = kk; } line += map[kk]; }
    lines.push(line);
  }
  function hex(k){ return '#' + [k >> 8, (k >> 4) & 15, k & 15].map(function(v){ return (v * 17).toString(16).padStart(2, '0'); }).join(''); }
  var legend = top.map(function(e, i){ return {ch: alphabet[i], hex: hex(e[0]), share: Math.round(1000 * e[1] / n) / 10}; });
  return {cols: cols, rows: rows, width: c.width, height: c.height, lines: lines, legend: legend};
})""".replace("%ALPHABET%", repr(CANVAS_ALPHABET))


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def name_legend(legend: list[dict], names: dict[str, str] | None) -> list[dict]:
    """Attach operator-given names to the sampled colours: each name's colour goes to the nearest
    sampled colour within a distance that survives the 4-bit quantisation. The names are
    configuration, never code, and they are the only way a colour means 'the player'."""
    if not names:
        return legend
    out = [dict(e) for e in legend]
    for hx, name in names.items():
        try:
            r, g, b = _hex_rgb(hx)
        except ValueError:
            continue
        best, bd = None, 10 ** 9
        for e in out:
            er, eg, eb = _hex_rgb(e["hex"])
            dd = (r - er) ** 2 + (g - eg) ** 2 + (b - eb) ** 2
            if dd < bd:
                best, bd = e, dd
        if best is not None and bd <= 3 * (24 ** 2):
            best["name"] = name
    return out


def named_clusters(lines: list[str], legend: list[dict], max_clusters: int = 6) -> list[str]:
    """Where each named colour's cells are, as horizontal clusters: "player at columns 12 to 13,
    rows 12 to 13". A grid is a picture the model would have to scan; a named thing's place is a
    fact it can read. Cells of one colour more than two columns apart are different things."""
    out: list[str] = []
    for e in legend:
        name, ch = e.get("name"), e.get("ch")
        if not name or not ch:
            continue
        cols: dict[int, list[int]] = {}
        for r, line in enumerate(lines):
            for c, x in enumerate(line):
                if x == ch:
                    cols.setdefault(c, []).append(r)
        if not cols:
            out.append(f"{name}: not on screen")
            continue
        clusters: list[list[int]] = []
        for c in sorted(cols):
            if clusters and c - clusters[-1][-1] <= 2:
                clusters[-1].append(c)
            else:
                clusters.append([c])
        parts = []
        for cl in clusters[:max_clusters]:
            rows = [r for c in cl for r in cols[c]]
            span = f"column {cl[0]}" if cl[0] == cl[-1] else f"columns {cl[0]} to {cl[-1]}"
            rspan = f"row {min(rows)}" if min(rows) == max(rows) else f"rows {min(rows)} to {max(rows)}"
            parts.append(f"{span}, {rspan}")
        more = f" and {len(clusters) - max_clusters} more" if len(clusters) > max_clusters else ""
        out.append(f"{name} at " + "; ".join(parts) + more)
    return out


def default_chrome() -> str | None:
    """A Chrome to launch: a Playwright-installed Chromium (PLAYWRIGHT_BROWSERS_PATH, or the user's
    cache), else a system Chrome."""
    import glob
    for root in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), os.path.expanduser("~/.cache/ms-playwright")):
        if not root:
            continue
        for pat in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
            hits = sorted(glob.glob(os.path.join(root, pat)))
            if hits:
                return hits[-1]
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(p):
            return p
    return shutil.which("google-chrome") or shutil.which("chromium") or None


class BrowserEnvironment(Environment):
    def __init__(self, cdp_url: str | None = None, executable_path: str | None = None, headless: bool = False,
                 start_url: str | None = None, text_values: dict[str, str] | None = None,
                 max_elements: int = 200, text_chars: int = 3000, timeout: float = 120.0,
                 user_data_dir: str | None = None, canvas: bool | str = False, canvas_cells: tuple[int, int] = (64, 32),
                 canvas_colors: int = 12, canvas_legend: dict[str, str] | None = None):
        """`canvas`: False, True (the largest canvas on the page) or a CSS selector. `canvas_cells` is
        the sample rate, columns by rows; `canvas_colors` the palette size; `canvas_legend` names
        for colours ({"#c84c0c": "ground"}), the operator's knowledge of the page and the only way
        a colour means anything to the model."""
        import importlib
        importlib.import_module("browser_use")     # the optional dependency, checked where it is needed
        self.cdp_url, self.executable_path, self.headless = cdp_url, executable_path, headless
        self.start_url, self.text_values = start_url, dict(text_values or {})
        self.max_elements, self.text_chars, self.timeout, self.user_data_dir = max_elements, text_chars, timeout, user_data_dir
        self.canvas, self.canvas_cells, self.canvas_colors = canvas, tuple(canvas_cells), int(canvas_colors)
        self.canvas_legend = dict(canvas_legend or {})
        if self.canvas and not (2 <= self.canvas_colors <= len(CANVAS_ALPHABET)):
            raise ValueError(f"canvas_colors must be 2 to {len(CANVAS_ALPHABET)}")
        self._session = None
        self._tools = None
        self._action_model = None
        self._last: dict = {}                 # what the last observation enumerated, by index
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="s1-browser")
        self._thread.start()

    @staticmethod
    def action_space() -> ActionSpace:
        import copy
        return ActionSpace.from_dict(copy.deepcopy(ACTION_SPACE))

    # ── the loop thread ──
    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self.timeout)

    async def _start(self):
        if self._session is not None:
            return
        from browser_use import Tools
        from browser_use.browser import BrowserProfile, BrowserSession
        if self.cdp_url:
            profile = BrowserProfile(cdp_url=self.cdp_url, is_local=True)
        else:
            profile = BrowserProfile(headless=self.headless, executable_path=self.executable_path or default_chrome(),
                                     user_data_dir=self.user_data_dir)
        self._session = BrowserSession(browser_profile=profile)
        await self._session.start()
        self._tools = Tools()
        self._action_model = self._tools.registry.create_action_model()

    async def _act(self, **action):
        r = await self._tools.act(self._action_model(**action), self._session)
        text = str(r.extracted_content or r.error or "").strip()
        return Result(ok=not r.error, text=text)

    async def _hold(self, key: str, seconds: float) -> Result:
        code, key_name, code_name = _KEY_CODES[key]
        cdp = await self._session.get_or_create_cdp_session()
        base = {"key": key_name, "code": code_name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        await cdp.cdp_client.send.Input.dispatchKeyEvent(params={"type": "keyDown", **base}, session_id=cdp.session_id)
        await asyncio.sleep(seconds)
        await cdp.cdp_client.send.Input.dispatchKeyEvent(params={"type": "keyUp", **base}, session_id=cdp.session_id)
        return Result(text=f"Held {key} for {seconds:.2f} s.")

    async def _select_values(self, selector_map: dict) -> dict[int, str]:
        """The current value of every dropdown, read from the live node: the page's own
        representation lists a dropdown's options but not which one is chosen, so after a choice
        nothing said the time was set (measured 2026-09-19)."""
        out: dict[int, str] = {}
        selects = [(i, n) for i, n in selector_map.items() if str(n.tag_name or "").lower() == "select"]
        if not selects:
            return out
        try:
            cdp = await self._session.get_or_create_cdp_session()
            for idx, node in selects:
                bid = getattr(node, "backend_node_id", None)
                if not bid:
                    continue
                r = await cdp.cdp_client.send.DOM.resolveNode(params={"backendNodeId": int(bid)}, session_id=cdp.session_id)
                oid = (r.get("object") or {}).get("objectId")
                if not oid:
                    continue
                v = await cdp.cdp_client.send.Runtime.callFunctionOn(
                    params={"objectId": oid, "functionDeclaration": "function(){ return this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : this.value; }",
                            "returnByValue": True}, session_id=cdp.session_id)
                out[idx] = str(((v.get("result") or {}).get("value")) or "").strip()
        except Exception:  # noqa: BLE001 - a dropdown without a readable value is shown without one
            pass
        return out

    async def _sample_canvas(self) -> dict | None:
        cols, rows = self.canvas_cells
        selector = self.canvas if isinstance(self.canvas, str) else ""
        cdp = await self._session.get_or_create_cdp_session()
        keep = []
        for hx in self.canvas_legend:
            try:
                r, g, b = _hex_rgb(hx)
                keep.append(((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4))
            except ValueError:
                continue
        expr = f"({_CANVAS_JS})({int(cols)}, {int(rows)}, {self.canvas_colors}, {selector!r}, {keep!r})"
        r = await cdp.cdp_client.send.Runtime.evaluate(params={"expression": expr, "returnByValue": True}, session_id=cdp.session_id)
        val = (r.get("result") or {}).get("value")
        if not isinstance(val, dict) or "lines" not in val:
            return None
        val["legend"] = name_legend(val["legend"], self.canvas_legend)
        return val

    async def _observe(self) -> Observation:
        await self._start()
        st = await self._session.get_browser_state_summary(include_screenshot=False)
        selector_map = await self._session.get_selector_map()
        rep = st.dom_state.llm_representation() if st.dom_state else ""
        select_values = await self._select_values(selector_map)
        settings: list[str] = []          # every control that holds a value, and the value it holds
        elements: dict[str, str] = {}
        text_fields: dict[str, str] = {}
        options: dict[str, str] = {}
        for idx, node in selector_map.items():
            if len(elements) >= self.max_elements:
                break
            ax = getattr(node, "ax_node", None)
            role = str(getattr(ax, "role", None) or node.tag_name or "").lower()
            name = str(getattr(ax, "name", None) or "").strip() or _text(node)
            attrs = node.attributes or {}
            label = f"{role} {name!r}" if name else role
            if attrs.get("placeholder") and not name:
                label = f"{role} placeholder {attrs['placeholder']!r}"
            tag = str(node.tag_name or "").lower()
            # The control's current state rides on its label, because the model reads literally:
            # a ticked checkbox labelled only 'Window seat' kept a third of the belief on ticking it
            # again after it was done (measured 2026-09-19); "(checked)" is what makes it done.
            input_type = str(attrs.get("type") or "").lower()
            if role in ("checkbox", "radio", "switch") or input_type in ("checkbox", "radio"):
                checked = str(attrs.get("checked", "")).lower() in ("true", "checked", "") and "checked" in attrs \
                    or str(getattr(ax, "checked", "") or "").lower() == "true"
                label += " (checked)" if checked else " (unchecked)"
                settings.append(f"{name or role}: {'checked' if checked else 'unchecked'}")
            elif tag in TEXT_FIELD_TAGS and input_type not in NOT_TEXT_INPUT_TYPES:
                value = str(attrs.get("value") or "").strip()
                label += f" (filled: {value!r})" if value else " (empty)"
                settings.append(f"{name or role}: {value!r}" if value else f"{name or role}: empty")
            elif tag == "select":
                current = select_values.get(idx, "")
                settings.append(f"{name or 'dropdown'}: {current!r}" if current else f"{name or 'dropdown'}: nothing chosen")
            if tag not in ("select", "label"):
                # a dropdown is reached through select_option alone: offered as a click as well, the
                # model split its belief between the two routes to one act and cleared neither gate
                # (measured 2026-09-19: select_option at 0.65 to 0.68 against 0.70, three refusals).
                # A label is a second name for the control it wraps, and the control is listed.
                elements[str(idx)] = label
            if tag != "select" and ((tag in TEXT_FIELD_TAGS and str(attrs.get("type") or "text").lower() not in NOT_TEXT_INPUT_TYPES)
                                    or role in TEXT_FIELD_ROLES or attrs.get("contenteditable") == "true"):
                text_fields[str(idx)] = label
            if tag == "select":
                current = select_values.get(idx, "")
                for opt in [o.strip() for o in _text(node).split("\n") if o.strip()][:60]:
                    options[f"{idx}:{opt}"] = f"{name or 'dropdown ' + str(idx)}: {opt}" + (" (selected)" if opt == current else "")
        tabs = {}
        for t in st.tabs or []:
            tid = str(getattr(t, "target_id", "") or "")[-4:]
            if tid:
                tabs[tid] = str(getattr(t, "title", "") or getattr(t, "url", "") or tid)[:80]
        self._last = {"elements": elements, "text_fields": text_fields, "options": options, "tabs": tabs}
        candidates: dict = {"elements": elements}
        if text_fields and self.text_values:
            candidates["text_fields"] = text_fields
            candidates["text_values"] = {k: k for k in self.text_values}
        if options:
            candidates["options"] = options
        if len(tabs) > 1:
            candidates["tabs"] = tabs
        body = rep if len(rep) <= self.text_chars else rep[: self.text_chars] + " …"
        text = f"URL: {st.url}\nTitle: {st.title}\n{body}"
        if settings:
            # what is set, stated outright and in one place: the model reads literally, and a value
            # only visible inside a control's markup was not read as done (measured 2026-09-19)
            text += "\nSet so far: " + "; ".join(settings) + "."
        canvas = await self._sample_canvas() if self.canvas else None
        if canvas:
            legend = ", ".join(f"'{e['ch']}' = {e['hex']}" + (f" ({e['name']})" if e.get("name") else "") + f" {e['share']}%"
                               for e in canvas["legend"])
            text += (f"\nCanvas {canvas['cols']}x{canvas['rows']} cells sampled from {canvas['width']}x{canvas['height']} px, "
                     f"one character per cell, row 0 at the top, column 0 at the left. Legend: {legend}.")
            places = named_clusters(canvas["lines"], canvas["legend"])
            if places:
                text += "\nOn the canvas: " + "; ".join(places) + "."
        fields = {"url": st.url, "title": st.title, "controls": len(elements), "tabs": len(tabs),
                  "set": settings,
                  **({"canvas": canvas["lines"]} if canvas else {}),
                  "pixels_above": int(getattr(st, "pixels_above", 0) or 0),
                  "pixels_below": int(getattr(st, "pixels_below", 0) or 0)}
        return Observation(text=text, fields=fields, candidates=candidates)

    # ── the environment contract ──
    def reset(self, goal: str) -> None:
        self._run(self._start())
        if self.start_url:
            self._run(self._session.navigate_to(self.start_url))
            self._run(asyncio.sleep(1.0))

    def observe(self) -> Observation:
        return self._run(self._observe())

    def execute(self, action: str, params: dict) -> Result:
        return self._run(self._execute(action, params))

    async def _execute(self, action: str, params: dict) -> Result:
        await self._start()
        try:
            if action == "click":
                return await self._act(click={"index": int(params["element"])})
            if action == "type_text":
                value = self.text_values.get(str(params.get("value")))
                if value is None:
                    return Result(ok=False, text=f"no supplied value named {params.get('value')!r}")
                return await self._act(input={"index": int(params["field"]), "text": value, "clear": True})
            if action == "select_option":
                idx, _, opt = str(params["option"]).partition(":")
                return await self._act(select_dropdown={"index": int(idx), "text": opt})
            if action == "press_key":
                key = str(params["key"])
                return await self._act(send_keys={"keys": " " if key == "Space" else key})
            if action == "hold_key":
                return await self._hold(str(params["key"]), HOLD_SECONDS.get(str(params.get("duration")), 0.4))
            if action == "scroll":
                return await self._act(scroll={"down": str(params.get("direction")) != "up", "pages": 1.0})
            if action == "go_back":
                return await self._act(go_back={})
            if action == "switch_tab":
                return await self._act(switch={"tab_id": str(params["tab"])})
            if action == "wait":
                await asyncio.sleep(1.0)
                return Result(text="Waited 1 s.")
        except (KeyError, ValueError) as exc:
            return Result(ok=False, text=f"{action}: {type(exc).__name__}: {exc}")
        return Result(ok=False, text=f"Unknown action {action!r}.")

    def close(self) -> None:
        async def _close():
            if self._session is not None:
                try:
                    if self.cdp_url:
                        await self._session.stop()
                    else:
                        await self._session.kill()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
                self._session = None
        try:
            self._run(_close())
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


def _text(node) -> str:
    fn: Callable | None = getattr(node, "get_all_children_text", None)
    try:
        return str(fn(max_depth=2) if fn else "").strip()
    except Exception:  # noqa: BLE001
        return ""
