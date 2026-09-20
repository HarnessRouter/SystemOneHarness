"""A web page as the environment, on Browser Use: what the model is offered and what runs.

Needs browser-use and a Chrome on the machine; skipped otherwise. The page is served from the test
itself over loopback, because Browser Use refuses file:// by policy.
"""
import http.server
import sys
import threading

import pytest

pytest.importorskip("browser_use", reason="the browser environment needs browser-use (pip install -e '.[browser]')")
from systemone_harness.envs.browser import BrowserEnvironment, default_chrome  # noqa: E402
from systemone_harness.envs import McpEnvironment  # noqa: E402

if not default_chrome():
    pytest.skip("no Chrome on this machine", allow_module_level=True)

PAGE = """<html><head><title>Book a table</title></head><body>
<h1>Book a table</h1>
<form onsubmit="event.preventDefault(); document.getElementById('out').textContent='Booked for '+document.getElementById('name').value+' at '+document.getElementById('slot').value;">
<label for="name">Your name</label> <input id="name" name="name" placeholder="Name">
<label for="slot">Time</label> <select id="slot"><option>18:00</option><option>19:30</option></select>
<label><input type="checkbox" id="window"> Window seat</label>
<button type="submit">Book</button>
</form>
<a href="#menu">See the menu</a>
<div id="out"></div>
<script>document.addEventListener('keydown', e => { if (e.key === 'ArrowRight') document.getElementById('out').textContent = 'right held'; });</script>
</body></html>"""


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def url():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.fixture(scope="module")
def env(url):
    e = BrowserEnvironment(headless=True, start_url=url, text_values={"name": "Richard"})
    e.reset("book")
    yield e
    e.close()


def test_the_page_is_offered_as_numbered_controls_fields_options_and_values(env):
    obs = env.observe()
    assert obs.text.startswith("URL: http://127.0.0.1:") and "Book a table" in obs.text
    c = obs.candidates
    assert "elements" in c and "text_fields" in c and "text_values" in c and "options" in c
    labels = " ".join(c["elements"].values())
    assert "button 'Book'" in labels and "link 'See the menu'" in labels and "checkbox" in labels
    assert not any(v.startswith("combobox") for v in c["elements"].values())      # a dropdown is select_option's alone
    assert list(c["text_fields"].values()) == ["textbox 'Your name' (empty)"]
    assert "checkbox 'Window seat' (unchecked)" in labels and "none 'Window seat'" not in labels   # a label is not a second name
    assert c["text_values"] == {"name": "name"}
    assert any(k.endswith(":19:30") and v == "Time: 19:30" for k, v in c["options"].items())
    assert "Set so far: Your name: empty; Time: '18:00'; Window seat: unchecked." in obs.text
    assert obs.fields["controls"] >= 4 and obs.fields["tabs"] == 1 and obs.terminal is False


def test_the_actions_run_and_report_in_the_page_s_words(env):
    obs = env.observe()
    field = next(iter(obs.candidates["text_fields"]))
    r = env.execute("type_text", {"field": field, "value": "name"})
    assert r.ok and "Richard" in r.text
    assert env.execute("type_text", {"field": field, "value": "nope"}).ok is False
    option = next(k for k in obs.candidates["options"] if k.endswith(":19:30"))
    assert env.execute("select_option", {"option": option}).ok
    box = next(k for k, v in obs.candidates["elements"].items() if v.startswith("checkbox 'Window seat'"))
    assert env.execute("click", {"element": box}).ok
    button = next(k for k, v in obs.candidates["elements"].items() if v == "button 'Book'")
    assert env.execute("click", {"element": button}).ok
    after = env.observe()
    assert "Booked for Richard at 19:30" in after.text
    assert "Set so far: Your name: 'Richard'; Time: '19:30'; Window seat: checked." in after.text
    assert "checkbox 'Window seat' (checked)" in after.candidates["elements"].values()
    assert after.candidates["options"][option] == "Time: 19:30 (selected)"
    r = env.execute("hold_key", {"key": "ArrowRight", "duration": "short"})
    assert r.ok and "Held ArrowRight" in r.text and "right held" in env.observe().text
    assert env.execute("scroll", {"direction": "down"}).ok and env.execute("wait", {}).ok
    assert env.execute("teleport", {}).ok is False


def test_the_mcp_server_compiles_to_the_same_action_space(url):
    server = f"{sys.executable} -m systemone_harness.envs.browser_mcp --headless --start-url {url} --text name=Richard"
    m = McpEnvironment(server)
    try:
        cat = m.catalogue()
        assert cat.unsupported == {} and cat.observe == "observe" and cat.reset == "reset"
        a, b = cat.space.to_dict(), BrowserEnvironment.action_space().to_dict()
        assert a["instructions"] == b["instructions"]
        assert a == b
        m.reset("book")
        obs = m.observe()
        assert "Book a table" in obs.text and "text_values" in obs.candidates
    finally:
        m.close()
