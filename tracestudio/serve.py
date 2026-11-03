# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""tracestudio open: hand a converted trace to ui.perfetto.dev and pin the tracks that matter.

Follows https://perfetto.dev/docs/visualization/deep-linking-to-perfetto-ui: a page served over
HTTP (file:// pages cannot do this) opens the UI, sends "PING" until the UI answers "PONG", then
posts {perfetto: {buffer, title, fileName}}. Startup commands in the UI URL's fragment
(startupCommands, a URL-encoded JSON array) pin tracks with dev.perfetto.PinTracksByRegex once the
trace is loaded. The server listens on 127.0.0.1 only, sends no Cross-Origin-Opener-Policy header
(same-origin would cut the page off from the window it opens), and stops after the trace was
delivered or on Ctrl-C.
"""
from __future__ import annotations

import html
import http.server
import json
import threading
import urllib.parse

UI_ORIGIN = "https://ui.perfetto.dev"
_REGEX_SPECIAL = set("\\^$.|?*+()[]{}")


def regex_literal(text):
    """`text` as a regular expression matching itself (only metacharacters escaped: valid in JS and Python)."""
    return "".join("\\" + c if c in _REGEX_SPECIAL else c for c in text)


def ui_url(pins):
    """The Perfetto UI URL with a PinTracksByRegex startup command per regex."""
    cmds = [{"id": "dev.perfetto.PinTracksByRegex", "args": [p]} for p in pins]
    if not cmds:
        return UI_ORIGIN + "/#!/"
    return UI_ORIGIN + "/#!/?startupCommands=" + urllib.parse.quote(json.dumps(cmds), safe="")


def default_pins(model, deadlines=(), top=2):
    """Regexes for the critical-path and QNX state tracks of the clients that matter most: those that missed
    a --deadline (most misses first), else those with the slowest trustworthy MsgSend to a server other than
    procnto (or to any server when there is none). Returns (regexes, client labels)."""
    from .analyses import us
    from .model import PROCNTO_PID
    worst = {}
    for t in model.txns:
        if not t.trustworthy or t.t_call is None or t.t_exit is None or model.is_idle(t.client):
            continue
        lat = us(model, t.t_exit - t.t_call)
        th = model.threads.get(t.client)
        if deadlines:
            missed = [dl for name, dl in deadlines if th and th.name == name and lat > dl]
            if not missed:
                continue
            k = worst.get(t.client, (0, 0.0))
            worst[t.client] = (k[0] + 1, max(k[1], lat))
        else:   # (has a request to a server other than procnto, worst latency of those, else of any)
            k = worst.get(t.client, (0, 0.0))
            app = t.server_pid != PROCNTO_PID
            if app and not k[0]:
                k = (1, 0.0)
            if app or not k[0]:
                k = (k[0], max(k[1], lat))
            worst[t.client] = k
    keys = sorted(worst, key=lambda c: (-worst[c][0], -worst[c][1], c))[:top]
    pins, labels = [], []
    for c in keys:
        th = model.threads.get(c)
        name = th.name if th and th.name else model.label(c)
        pins.append("^%s (critical path|QNX state)" % regex_literal(name))
        labels.append(model.label(c))
    return pins, labels


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>trace-studio: %(title)s</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:2em;max-width:44em}button{font-size:16px;padding:.4em 1em}</style>
</head><body>
<h1>trace-studio</h1>
<p id="status">Loading %(title)s ...</p>
<p><button id="open" hidden>Open in Perfetto</button></p>
<script>
const UI = %(ui)s, ORIGIN = %(origin)s, TITLE = %(jtitle)s, FILE = %(jfile)s;
const status = document.getElementById("status"), button = document.getElementById("open");
let trace = null;
function deliver(win) {
  const ping = setInterval(() => win.postMessage("PING", ORIGIN), 250);
  window.addEventListener("message", (e) => {
    if (e.source !== win || e.data !== "PONG" || trace === null) return;
    clearInterval(ping);
    win.postMessage({perfetto: {buffer: trace, title: TITLE, fileName: FILE}}, ORIGIN);
    status.textContent = "Sent " + TITLE + " (" + trace.byteLength + " bytes) to the Perfetto UI. You can close this tab.";
    fetch("/delivered", {method: "POST"});
    trace = null;
  });
}
function openUi() {
  const win = window.open(UI);
  if (!win) {          // popup blocked: a click is a user gesture, which browsers allow
    status.textContent = "Your browser blocked the Perfetto window. Click the button to open it.";
    button.hidden = false;
    return;
  }
  button.hidden = true;
  status.textContent = "Waiting for the Perfetto UI to answer ...";
  deliver(win);
}
button.onclick = openUi;
fetch("/trace").then((r) => r.arrayBuffer()).then((b) => { trace = b; openUi(); });
</script>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "tracestudio"

    def log_message(self, fmt, *args):      # keep the terminal for our own messages
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        s = self.server
        if self.path in ("/", "/index.html"):
            self._send(200, s.page, "text/html; charset=utf-8")
        elif self.path == "/trace":
            s.fetched = True
            self._send(200, s.trace, "application/octet-stream")
        else:
            self._send(404, b"not found\n")

    do_HEAD = do_GET

    def do_POST(self):
        if self.path == "/delivered":
            self._send(204)
            self.server.delivered.set()
        else:
            self._send(404, b"not found\n")


def make_server(trace_bytes, title, file_name, pins, port=0):
    """An HTTP server on 127.0.0.1 that serves the page and the trace; .delivered is set once the page has
    posted the trace to the Perfetto UI."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.trace = trace_bytes
    srv.fetched = False
    srv.delivered = threading.Event()
    srv.ui_url = ui_url(pins)
    srv.page = (PAGE % {"title": html.escape(title), "ui": json.dumps(srv.ui_url), "origin": json.dumps(UI_ORIGIN),
                        "jtitle": json.dumps(title), "jfile": json.dumps(file_name)}).encode()
    return srv


def serve_until_delivered(srv, timeout=None):
    """Serve until the trace was delivered (True), Ctrl-C or the timeout (False)."""
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        return srv.delivered.wait(timeout)
    except KeyboardInterrupt:
        return False
    finally:
        srv.shutdown()
        srv.server_close()
