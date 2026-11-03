# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""tracestudio open: hand a converted trace to ui.perfetto.dev and pin the tracks that matter.

Follows https://perfetto.dev/docs/visualization/deep-linking-to-perfetto-ui: a page served over
HTTP (file:// pages cannot do this) opens the UI, sends "PING" until the UI answers "PONG", then
posts {perfetto: {buffer, title, fileName}} with the buffer transferred. Startup commands in the UI
URL's fragment (startupCommands, a URL-encoded JSON array) pin tracks with
dev.perfetto.PinTracksByRegex once the trace is loaded.

The server listens on 127.0.0.1 only and answers only requests whose Host is 127.0.0.1:PORT or
localhost:PORT (a DNS-rebinding page has another Host). Every path carries a random per-run token,
and the completion report must also come from the page's own origin. It sends no
Cross-Origin-Opener-Policy header (same-origin would cut the page off from the window it opens) and
stops once the page reports that it handed the trace to the UI, at the timeout, or on Ctrl-C.
postMessage does not tell whether the UI loaded the trace, so neither does this module.
"""
from __future__ import annotations

import html
import http.server
import json
import os
import secrets
import stat
import tempfile
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
    from .analyses import selector_matches, us
    from .model import PROCNTO_PID
    worst = {}
    for t in model.txns:
        if not t.trustworthy or t.t_call is None or t.t_exit is None or model.is_idle(t.client):
            continue
        lat = us(model, t.t_exit - t.t_call)
        th = model.threads.get(t.client)
        if deadlines:
            missed = [dl for name, dl in deadlines if th and selector_matches(model, name, th.name, t) and lat > dl]
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


# ---- private cache of converted traces ------------------------------------------------------------------
def cache_dir():
    """The user's private cache directory for converted traces (created with mode 0700); OSError when it is a
    symlink, belongs to someone else or is open to group or others."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "tracestudio")
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OSError("cache directory %s is not private (it must be a directory of yours with mode 0700)" % d)
    return d


def cache_path(trace_path, version):
    """Cache file for this input: named after its absolute path, size, modification time and the tracestudio
    version, so a changed input or a new version never reuses an old conversion."""
    import hashlib
    st = os.stat(trace_path)
    key = "%s|%d|%d|%s" % (os.path.abspath(trace_path), st.st_size, st.st_mtime_ns, version)
    return os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:32] + ".perfetto-trace")


def trace_complete(data):
    """True when `data` is a sequence of whole `Trace.packet` frames (field 1, length-delimited) to its end, as
    the writer frames them; a truncated or foreign file is not."""
    i, n = 0, len(data)
    if not n:
        return False
    while i < n:
        if data[i] != 0x0A:
            return False
        i += 1
        size = shift = 0
        while True:
            if i >= n or shift > 63:
                return False
            b = data[i]
            i += 1
            size |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        i += size
    return i == n


def write_atomic(path, data):
    """Write to a private temporary file in the same directory, then rename it over `path`."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- the page and the server ------------------------------------------------------------------------------
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>trace-studio: %(title)s</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:2em;max-width:44em}button{font-size:16px;padding:.4em 1em}
.err{color:#b00020}</style>
</head><body>
<h1>trace-studio</h1>
<p id="status">Loading %(title)s ...</p>
<p><button id="open" hidden>Open in Perfetto</button> <button id="retry" hidden>Retry</button></p>
<script>
const UI = %(ui)s, ORIGIN = %(origin)s, TITLE = %(jtitle)s, FILE = %(jfile)s, BASE = %(base)s;
const HANDSHAKE_MS = 60000;
const status = document.getElementById("status"), openBtn = document.getElementById("open"),
      retryBtn = document.getElementById("retry");
let trace = null, busy = false;
function say(text, error) { status.textContent = text; status.className = error ? "err" : ""; }
function fail(text) { busy = false; say(text, true); retryBtn.hidden = false; }
function report(bytes) {
  fetch(BASE + "delivered", {method: "POST", headers: {"Content-Type": "text/plain"}, body: String(bytes)})
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); })
    .catch((err) => say("Handed " + TITLE + " to the Perfetto UI, but could not tell trace-studio (" + err.message +
                        "); stop it with Ctrl-C.", true));
}
function deliver(win) {
  const started = Date.now();
  const timer = setInterval(() => {
    if (win.closed) { cleanup(); fail("The Perfetto window was closed before it answered."); return; }
    if (Date.now() - started > HANDSHAKE_MS) {
      cleanup(); fail("The Perfetto UI did not answer within " + HANDSHAKE_MS / 1000 + " s."); return;
    }
    win.postMessage("PING", ORIGIN);
  }, 250);
  function onMessage(e) {
    if (e.source !== win || e.origin !== ORIGIN || e.data !== "PONG" || trace === null) return;
    cleanup();
    const bytes = trace.byteLength;
    win.postMessage({perfetto: {buffer: trace, title: TITLE, fileName: FILE}}, ORIGIN, [trace]);
    trace = null;
    busy = false;
    say("Handed " + TITLE + " (" + bytes + " bytes) to the Perfetto UI; it loads it in its own tab. " +
        "You can close this tab.");
    report(bytes);
  }
  function cleanup() { clearInterval(timer); window.removeEventListener("message", onMessage); }
  window.addEventListener("message", onMessage);
}
function openUi() {
  const win = window.open(UI);
  if (!win) {          // popup blocked: a click is a user gesture, which browsers allow
    busy = false;
    say("Your browser blocked the Perfetto window. Click the button to open it.");
    openBtn.hidden = false;
    return;
  }
  openBtn.hidden = true;
  say("Waiting for the Perfetto UI to answer ...");
  deliver(win);
}
function start() {
  if (busy) return;
  busy = true;
  retryBtn.hidden = true;
  say("Loading " + TITLE + " ...");
  fetch(BASE + "trace")
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status + " " + r.statusText); return r.arrayBuffer(); })
    .then((b) => { trace = b; openUi(); })
    .catch((err) => fail("Could not load the trace from trace-studio: " + err.message +
                         ". Is tracestudio open still running?"));
}
openBtn.onclick = () => { busy = true; openUi(); };
retryBtn.onclick = start;
start();
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _allowed_host(self):
        return self.headers.get("Host") in self.server.hosts

    def do_GET(self):
        s = self.server
        if not self._allowed_host():
            self._send(421, b"wrong host\n")
        elif self.path == s.base:
            self._send(200, s.page, "text/html; charset=utf-8")
        elif self.path == s.base + "trace":
            self._send(200, s.trace, "application/octet-stream")
        else:
            self._send(404, b"not found\n")

    do_HEAD = do_GET

    def do_POST(self):
        s = self.server
        if not self._allowed_host():
            self._send(421, b"wrong host\n")
        elif self.path != s.base + "delivered":
            self._send(404, b"not found\n")
        elif self.headers.get("Origin") not in s.origins:
            self._send(403, b"wrong origin\n")
        else:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 64)) if n > 0 else b""
            self._send(204)
            s.handed_bytes = int(body) if body.strip().isdigit() else None
            s.delivered.set()


def make_server(trace_bytes, title, file_name, pins, port=0):
    """An HTTP server on 127.0.0.1 that serves the page and the trace under a random token; .delivered is set
    once the page reports that it handed the trace to the Perfetto UI; .url is the page."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    p = srv.server_address[1]
    srv.hosts = {"127.0.0.1:%d" % p, "localhost:%d" % p}
    srv.origins = {"http://" + h for h in srv.hosts}
    srv.token = secrets.token_urlsafe(24)
    srv.base = "/%s/" % srv.token
    srv.url = "http://127.0.0.1:%d%s" % (p, srv.base)
    srv.trace = trace_bytes
    srv.handed_bytes = None
    srv.delivered = threading.Event()
    srv.ui_url = ui_url(pins)
    srv.page = (PAGE % {"title": html.escape(title), "ui": json.dumps(srv.ui_url), "origin": json.dumps(UI_ORIGIN),
                        "jtitle": json.dumps(title), "jfile": json.dumps(file_name),
                        "base": json.dumps(srv.base)}).encode()
    return srv


def serve_until_delivered(srv, timeout=None):
    """Serve until the page reported the hand-off (True), Ctrl-C or the timeout (False)."""
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        return srv.delivered.wait(timeout)
    except KeyboardInterrupt:
        return False
    finally:
        srv.shutdown()
        srv.server_close()
