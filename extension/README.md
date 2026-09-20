# The extension

A Chrome side panel that is a plain Unified Harness Protocol client: connect to a harness server,
pick a harness, give a goal and the values the page may need, watch the loop's actions stream in,
stop it, continue it. It automates nothing itself; the browser environment runs beside Chrome and
reaches it through Chrome's own agent connection (see docs/browser-use.md).

## Try it

1. Allow a local agent connection to your Chrome at `chrome://inspect/#remote-debugging` (Chrome 144
   and later), or start Chrome with `--remote-debugging-port=9222`.
2. Serve the harness beside the browser:

       pip install -e ".[browser]"
       export OPENROUTER_API_KEY=...
       s1 serve --browser --cdp-url http://127.0.0.1:9222 --api-key choose-a-secret

3. Load this folder unpacked at `chrome://extensions` (Developer mode, "Load unpacked"), pin the
   toolbar button, click it: the side panel opens. Server `http://127.0.0.1:8710`, the key you chose.
4. Pick the Browser harness, write a goal, add `name=Richard` style values if the page needs typing,
   Start. Each executed action arrives as a step with the page's own reply.

The same panel talks to any protocol server, HarnessRouter included: point it at that server and
its key, and pick a harness on the System One base.
