"""Canvas mode: a canvas sampled into a character grid, at a rate and palette the operator sets.

Needs browser-use and a Chrome; skipped otherwise. The page draws a known picture on a canvas
(a sky, a ground band, a small red figure) so every cell can be checked.
"""
import http.server
import threading

import pytest

pytest.importorskip("browser_use", reason="the browser environment needs browser-use (pip install -e '.[browser]')")
from systemone_harness.envs.browser import BrowserEnvironment, default_chrome  # noqa: E402
from systemone_harness.encoder import estimate_tokens  # noqa: E402

if not default_chrome():
    pytest.skip("no Chrome on this machine", allow_module_level=True)

PAGE = """<html><body style="margin:0"><canvas id="game" width="640" height="320"></canvas>
<script>
const c = document.getElementById('game').getContext('2d');
c.fillStyle = '#5599ff'; c.fillRect(0, 0, 640, 320);        // sky
c.fillStyle = '#cc4400'; c.fillRect(0, 240, 640, 80);       // ground, the bottom quarter
c.fillStyle = '#ff0000'; c.fillRect(100, 200, 40, 40);      // a red figure on the ground, left side
</script></body></html>"""


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


def test_the_canvas_is_sampled_at_the_configured_rate_with_named_colours(url):
    env = BrowserEnvironment(headless=True, start_url=url, canvas=True, canvas_cells=(32, 16), canvas_colors=4,
                             canvas_legend={"#ff0000": "player", "#cc4400": "ground"})
    try:
        env.reset("look")
        obs = env.observe()
        grid = obs.fields["canvas"]
        assert len(grid) == 16 and all(len(r) == 32 for r in grid)
        assert "Canvas 32x16 cells sampled from 640x320 px" in obs.text
        assert "(player)" in obs.text and "(ground)" in obs.text
        # the sky is the most common colour and '.'; the ground is the bottom quarter
        assert grid[0] == "." * 32
        legend = obs.text.split("Legend: ")[-1]
        ground_ch = next(seg.split("'")[1] for seg in legend.split(", ") if "(ground)" in seg)
        player_ch = next(seg.split("'")[1] for seg in legend.split(", ") if "(player)" in seg)
        assert grid[15] == ground_ch * 32 and grid[12] == ground_ch * 32
        # the 40 px figure at x 100..140, y 200..240 is 2 cells wide and 2 tall at 20 px a cell
        assert grid[10][5:7] == player_ch * 2 and grid[11][5:7] == player_ch * 2
        assert grid[10][0:5] == "....." and grid[10][7:] == "." * 25
        assert estimate_tokens(grid) < 200
        # a finer rate costs more tokens and keeps the picture
        env.canvas_cells = (128, 64)
        fine = env.observe().fields["canvas"]
        assert len(fine) == 64 and len(fine[0]) == 128 and estimate_tokens(fine) > 1500
        assert fine[40][20:28] == player_ch * 8
    finally:
        env.close()


def test_a_rare_named_colour_survives_the_palette(url):
    """The figure is 1.25 percent of the picture; with a palette of two it is gone unless named."""
    env = BrowserEnvironment(headless=True, start_url=url, canvas=True, canvas_cells=(32, 16), canvas_colors=2)
    try:
        env.reset("look")
        plain = env.observe()
        assert "#ff0000" not in plain.text
        env.canvas_legend = {"#ff0000": "player"}
        named = env.observe()
        assert "#ff0000 (player)" in named.text
    finally:
        env.close()


def test_without_canvas_mode_nothing_is_sampled(url):
    env = BrowserEnvironment(headless=True, start_url=url)
    try:
        env.reset("look")
        obs = env.observe()
        assert "canvas" not in obs.fields and "Canvas" not in obs.text
    finally:
        env.close()
