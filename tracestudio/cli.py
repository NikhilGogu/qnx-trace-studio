# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""trace-studio command line.

  tracestudio convert  TRACE.kev [-o OUT.perfetto-trace]
  tracestudio validate TRACE.kev --logs DIR       (workload ground truth)
  tracestudio analyze  TRACE.kev [--deadline THREAD=US]
  tracestudio info     TRACE.kev
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .kev import KevFile
from .model import Model


def _load(path):
    t0 = time.time()
    kev = KevFile(path)
    model = Model(kev)
    return kev, model, time.time() - t0


def cmd_info(a):
    kev, model, dt = _load(a.trace)
    st = kev.stats
    print(json.dumps({
        "file": a.trace, "version": "%d.%02d" % kev.version, "cpus": kev.ncpu, "cycles_per_sec": kev.cycles_per_sec,
        "tracelogger": kev.attrs.get("TRACELOGGER_ARGS"), "slots": st.slots, "events": st.events,
        "combine_events": st.combine_events, "partial_combines": st.partial_combines, "orphan_slots": st.orphan_slots,
        "rollovers": st.rollovers, "lost_buffers": st.lost_buffers, "dropped_events": st.dropped_events,
        "processes": len(model.processes), "threads": len(model.threads), "switches": len(model.switches),
        "ipc_transactions": len(model.txns), "pulses": len(model.pulses), "interrupts": len(model.interrupts),
        "wide": model.wide, "counters": dict(model.counters), "parse_seconds": round(dt, 2)}, indent=1))


def cmd_convert(a):
    from .perfetto_out import write
    kev, model, dt = _load(a.trace)
    out = a.output or a.trace.rsplit(".", 1)[0] + ".perfetto-trace"
    stats = write(model, out)
    warn = []
    if kev.stats.lost_buffers:
        warn.append("lost buffers %s" % kev.stats.lost_buffers)
    if kev.stats.dropped_events:
        warn.append("kernel dropped %d events" % kev.stats.dropped_events)
    if model.wide is False:
        warn.append("fast-mode trace: no priorities or _msg_info; links rely on COMM events only")
    print("wrote %s: %s" % (out, json.dumps(stats)))
    for w in warn:
        print("warning:", w)


def cmd_validate(a):
    from .validate import validate
    kev, model, _ = _load(a.trace)
    res, _, _ = validate(model, a.logs)
    res["match_rate"] = round(res["matched"] / res["requests"], 4) if res["requests"] else None
    print(json.dumps(res, indent=1, default=str))


def cmd_analyze(a):
    from .analyses import Timeline, blocking_chain, deadline_misses, ipc_latency, suspected_inversions
    kev, model, _ = _load(a.trace)
    tl = Timeline(model)
    out = {"ipc_latency": ipc_latency(model), "suspected_inversions": suspected_inversions(model, tl=tl),
           "fixed_priority_channels": {"%d/%d" % k: v for k, v in model.fixed_priority_channels().items()}}
    for spec in a.deadline or []:
        name, us = spec.split("=")
        per_server = deadline_misses(model, name, float(us))
        out.setdefault("deadlines", {})[name] = {"deadline_us": float(us), "by_server": {
            s: {"requests": n, "missed": miss, "max_us": mx} for s, (n, miss, mx) in per_server.items()}}
    if a.chains:
        worst = sorted((t for t in model.txns if t.t_call and t.t_exit and t.parent is None),
                       key=lambda t: t.t_exit - t.t_call, reverse=True)[:a.chains]
        out["longest_chains"] = [blocking_chain(model, t, tl) for t in worst]
    print(json.dumps(out, indent=1, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="tracestudio", description="QNX .kev kernel traces to Perfetto, with analyses")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info"); s.add_argument("trace"); s.set_defaults(fn=cmd_info)
    s = sub.add_parser("convert"); s.add_argument("trace"); s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_convert)
    s = sub.add_parser("validate"); s.add_argument("trace"); s.add_argument("--logs", required=True)
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("analyze"); s.add_argument("trace"); s.add_argument("--deadline", action="append")
    s.add_argument("--chains", type=int, default=3); s.set_defaults(fn=cmd_analyze)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
