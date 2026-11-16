#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Build a static examples page from the captures in tests/data.

    python3 tools/build_examples.py --out site [--with-timing] [--only run1/prio_fixed ...]

For each capture the page shows what `tracestudio analyze` (and, for the priority scenarios,
`tracestudio check`) concludes, and has a button that hands the converted trace to ui.perfetto.dev
with the important tracks pinned. Serve the directory over HTTP (GitHub Pages, or
`python3 -m http.server -d site`); the hand-off does not work from file://.

By default every measured time in the text is replaced by "[timing withheld]": the QNX licence does
not allow publishing performance results of QNX software without written approval. Verdicts,
thread names, priorities, states and counts stay. --with-timing keeps the times, for local use.
"""
import argparse
import contextlib
import html
import io
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from tracestudio.check import render, run_checks  # noqa: E402
from tracestudio.cli import _analysis, _timeline, build_parser, slowest_requests  # noqa: E402
from tracestudio.critical import critical_path, to_json  # noqa: E402
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402
from tracestudio.perfetto_out import write  # noqa: E402
from tracestudio.serve import UI_ORIGIN, default_pins, ui_url  # noqa: E402

WITHHELD = "[timing withheld]"
TIMING = re.compile(r"\d[\d,]*(?:\.\d+)?\s?(?:us|ms|s)\b")

PRIO_GATE = ["--deadline", "hi@ts_psrv=5000", "--no-inversions", "--client", "ts_pload/"]
PRIO_ANALYZE = ["--deadline", "hi@ts_psrv=5000", "--client", "ts_pload/hi", "--slowest", "1"]
MUTEX_ANALYZE = ["--slowest", "1", "--client", "ts_mutex/hi"]

# (capture, title, what the workload does, analyze options, check options or None)
EXAMPLES = [
    ("run1/prio_fixed", "Fixed-priority channel",
     "hi (prio 30) asks a server for work; the server's channel has _NTO_CHF_FIXED_PRIORITY, so the server keeps "
     "prio 10 while a hog at 20 holds CPU 0.", PRIO_ANALYZE, PRIO_GATE),
    ("run1/prio_inherit", "The same workload with priority inheritance",
     "The channel inherits the client's priority, so the server runs hi's request at 30 and the hog does not get "
     "in.", PRIO_ANALYZE, PRIO_GATE),
    ("run3/prio_fixed_rx", "Fixed priority, hog after the receive",
     "As above, but the hog starts after the server received hi's request: hi is REPLY-blocked.",
     PRIO_ANALYZE, PRIO_GATE),
    ("run3/prio_inherit_rx", "Inheritance, hog after the receive",
     "The same with inheritance.", PRIO_ANALYZE, PRIO_GATE),
    ("run5/prio_fixed", "Fixed-priority channel on aarch64le",
     "The fixed-priority scenario captured on an aarch64le QEMU virt target.", PRIO_ANALYZE, PRIO_GATE),
    ("run4/mutex_chain_nopi", "Mutex without priority inheritance",
     "hi's server thread waits on a PTHREAD_PRIO_NONE mutex whose owner (prio 10) waits on another server, "
     "while a hog at 20 holds CPU 0.", MUTEX_ANALYZE, None),
    ("run4/mutex_chain_hog", "Mutex with priority inheritance",
     "The same with QNX's default mutex: the owner runs at 30 while it holds the mutex.", MUTEX_ANALYZE, None),
    ("run1/ipc_wide", "Message passing, wide mode",
     "Four clients, a front server with two receive threads that forwards every fourth request to a back "
     "server, and pulses.", ["--slowest", "1"], None),
    ("run1/ipc_fast", "Message passing, fast mode",
     "The same workload in fast mode: no priorities, so the inversion analysis has insufficient evidence.",
     ["--slowest", "1"], None),
    ("run3/cancel", "Cancelled requests",
     "Requests that end by TimerTimeout (ETIMEDOUT) or a signal (EINTR) while the server replies late.", [], None),
    ("run3/rm_small", "Resource manager traffic",
     "cat and dd on a qnx6 file system served by devb-eide.", [], None),
]


def withhold(text, keep_timing):
    return text if keep_timing else TIMING.sub(WITHHELD, text)


def example(rel, analyze_opts, check_opts, out_dir, keep_timing):
    """The page section for one capture as a dict; writes <run>_<name>.perfetto-trace into out_dir."""
    path = os.path.join("tests", "data", rel + ".kev.gz")
    model = Model(KevFile(os.path.join(ROOT, path)))
    a = build_parser().parse_args(["analyze", path] + analyze_opts)
    tl = _timeline(model, a)
    res = _analysis(model, a, tl)
    fname = rel.replace("/", "_") + ".perfetto-trace"
    with contextlib.redirect_stdout(io.StringIO()):
        write(model, os.path.join(out_dir, fname))
    inv = res["inversions"]
    lines = ["%d message exchanges, %d trustworthy" % (res["transactions"], res["trustworthy"])]
    for name, dl in res["deadlines"].items():
        for srv, r in dl["by_server"].items():
            lines.append("requests of %s to %s over the deadline: %d of %d" % (
                name.split("@")[0], srv, r["missed"], r["requests"]))
    lines.append("suspected priority inversions: %d; insufficient evidence: %d; checked: %d" % (
        len(inv["suspected"]), len(inv["insufficient_evidence"]), inv["checked"]))
    for c in res["fixed_priority_channels"]:
        lines.append("channel created with _NTO_CHF_FIXED_PRIORITY: %s chid %d" % (c["process"], c["chid"]))
    evidence = [r["evidence"] for r in inv["suspected"][:1]]
    evidence += ["txn %d: %s -> %s: insufficient evidence: %s" % (r["txn"], r["client"], r["server"],
                                                                  "; ".join(r["reasons"]))
                 for r in inv["insufficient_evidence"][:2]]
    evidence += ["%s waited on mutex %s owned by %s: %s" % (w["waiter"], w["mutex"], w["owner"], w["chain"])
                 for w in res["mutex_waits"][:1] if w["owner"]]
    paths = []
    for tx in slowest_requests(model, a.slowest, a.client):
        cp = to_json(model, tx, critical_path(model, tx, tl))
        paths.append({"title": "txn %d: %s -> %s, MsgSend latency %.1f us" % (
            cp["txn"], cp["client"], cp["server"], cp["latency_us"]),
            "segments": ["%s: %.1f us" % (s["what"], s["us"]) for s in cp["segments"]]})
    gate = None
    if check_opts:
        c = build_parser().parse_args(["check", path] + check_opts)
        results = run_checks(model, _timeline(model, c), c)
        gate = {"command": "tracestudio check %s %s" % (path, " ".join(check_opts)),
                "passed": all(r.ok for r in results), "lines": render(results)}
    pins, _ = default_pins(model, a.deadline or ())
    sect = {"id": rel.replace("/", "_"), "trace": fname, "ui": ui_url(pins),
            "command": "tracestudio analyze %s %s" % (path, " ".join(analyze_opts)), "lines": lines,
            "evidence": evidence, "paths": paths, "gate": gate}
    return json.loads(withhold(json.dumps(sect), keep_timing))


# The hand-off follows https://perfetto.dev/docs/visualization/deep-linking-to-perfetto-ui, as
# tracestudio open does, but from a static page: the trace comes from the same origin and nothing
# reports back. The window is opened inside the click so that pop-up blockers allow it.
SCRIPT = """
const ORIGIN = %(origin)s;
function show(sect, text, error) {
  const p = sect.querySelector(".status"); p.textContent = text; p.className = "status" + (error ? " err" : "");
}
function openInPerfetto(button) {
  const sect = button.closest("section");
  const win = window.open(button.dataset.ui);
  if (!win) { show(sect, "The browser blocked the Perfetto window. Allow pop-ups for this page.", true); return; }
  show(sect, "Loading the trace ...");
  fetch(button.dataset.trace)
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.arrayBuffer(); })
    .then((buffer) => {
      const started = Date.now();
      const timer = setInterval(() => {
        if (win.closed || Date.now() - started > 60000) {
          clearInterval(timer); window.removeEventListener("message", onMessage);
          show(sect, "The Perfetto UI did not answer. Try again.", true);
          return;
        }
        win.postMessage("PING", ORIGIN);
      }, 250);
      function onMessage(e) {
        if (e.source !== win || e.origin !== ORIGIN || e.data !== "PONG") return;
        clearInterval(timer); window.removeEventListener("message", onMessage);
        win.postMessage({perfetto: {buffer: buffer, title: button.dataset.trace, fileName: button.dataset.trace}},
                        ORIGIN, [buffer]);
        show(sect, "Handed to the Perfetto UI in the other tab.");
      }
      window.addEventListener("message", onMessage);
    })
    .catch((err) => { win.close(); show(sect, "Could not load the trace: " + err.message, true); });
}
"""

STYLE = """body{font:15px/1.5 system-ui,sans-serif;margin:2em auto;max-width:60em;padding:0 1em;color:#1b1b1b}
section{border-top:1px solid #ccc;padding:.5em 0 1em}code,pre{font:13px/1.4 ui-monospace,monospace}
pre{background:#f5f5f5;padding:.6em;overflow-x:auto;white-space:pre-wrap}.pass{color:#1b5e20}.fail{color:#b00020}
.err{color:#b00020}button{font-size:15px;padding:.3em .9em}"""


def page(sections, keep_timing):
    e = html.escape
    body = []
    for s, (rel, title, about, _, _) in sections:
        part = ['<section id="%s"><h2>%s</h2><p>%s</p>' % (e(s["id"]), e(title), e(about)),
                "<pre>$ %s\n%s</pre>" % (e(s["command"]), e("\n".join(s["lines"])))]
        if s["evidence"]:
            part.append("<p>Evidence, as trace-studio reports it:</p><pre>%s</pre>" % e("\n\n".join(s["evidence"])))
        for cp in s["paths"]:
            part.append("<p>Critical path of the slowest request (%s):</p><pre>%s</pre>" % (
                e(cp["title"]), e("\n".join(cp["segments"]))))
        if s["gate"]:
            g = s["gate"]
            part.append('<p>CI gate: <strong class="%s">%s</strong></p><pre>$ %s\n%s</pre>' % (
                "pass" if g["passed"] else "fail", "passes (exit 0)" if g["passed"] else "fails (exit 1)",
                e(g["command"]), e("\n".join(g["lines"]))))
        part.append('<p><button data-trace="%s" data-ui="%s" onclick="openInPerfetto(this)">Open in Perfetto</button> '
                    '<a href="%s">download the converted trace</a> <span class="status"></span></p></section>'
                    % (e(s["trace"]), e(s["ui"]), e(s["trace"])))
        body.append("\n".join(part))
    note = ("" if keep_timing else
            "<p>Measured times are withheld on this page (shown as %s). Run the commands on the captures in "
            "<code>tests/data</code> to see them.</p>" % e(WITHHELD))
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>trace-studio examples</title><style>%s</style></head><body>
<h1>trace-studio examples</h1>
<p>Kernel traces captured on QNX OS 8.0 under QEMU, as checked in under <code>tests/data</code>, and what
trace-studio concludes from each. "Open in Perfetto" hands the converted trace to ui.perfetto.dev in your
browser, with the tracks that matter pinned.</p>%s
%s
<script>%s</script></body></html>
""" % (STYLE, note, "\n".join(body), SCRIPT % {"origin": json.dumps(UI_ORIGIN)})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--with-timing", action="store_true", help="keep measured times (for local use)")
    ap.add_argument("--only", action="append", metavar="RUN/NAME", help="only these captures (repeatable)")
    a = ap.parse_args(argv)
    chosen = [x for x in EXAMPLES if not a.only or x[0] in a.only]
    unknown = set(a.only or ()) - {x[0] for x in EXAMPLES}
    if unknown:
        ap.error("no example named %s" % ", ".join(sorted(unknown)))
    os.makedirs(a.out, exist_ok=True)
    sections = []
    for x in chosen:
        rel, _, _, analyze_opts, check_opts = x
        sections.append((example(rel, analyze_opts, check_opts, a.out, a.with_timing), x))
    with open(os.path.join(a.out, "index.html"), "w") as f:
        f.write(page(sections, a.with_timing))
    print("wrote %s with %d examples" % (os.path.join(a.out, "index.html"), len(sections)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
