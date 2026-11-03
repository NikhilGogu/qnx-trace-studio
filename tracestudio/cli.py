# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""trace-studio command line.

  tracestudio info     TRACE.kev
  tracestudio convert  TRACE.kev [-o OUT.perfetto-trace] [--no-critical-path]
  tracestudio analyze  TRACE.kev [--deadline THREAD=US] [--request TXN] [--slowest N] [--json]
  tracestudio diff     BEFORE.kev AFTER.kev [--deadline THREAD=US] [--json]
  tracestudio validate TRACE.kev --logs DIR       (workload ground truth)

Exit status: 0 on success, 1 when validation fails or finds no logged requests,
or when a trace has no events, 2 on usage errors.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time

from .kev import KevError, KevFile
from .model import Model


def _load(path):
    t0 = time.time()
    kev = KevFile(path)
    model = Model(kev)
    return kev, model, time.time() - t0


def _us(text):
    """A finite, nonnegative number of microseconds."""
    try:
        v = float(text)
    except ValueError:
        v = None
    if v is None or not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError("want a finite number of microseconds >= 0, got %r" % text)
    return v


def _count(text):
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError("want a whole number >= 0, got %r" % text)
    return n


def _deadline_spec(spec):
    """THREAD=US -> (thread name, microseconds)."""
    name, sep, val = spec.rpartition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError("want THREAD=MICROSECONDS, got %r" % spec)
    return name, _us(val)


def _runmask_spec(spec):
    """PROCESS[/THREAD]=MASK (hex) -> (process, thread or None, mask)."""
    name, sep, val = spec.rpartition("=")
    proc, _, thr = name.partition("/")
    try:
        mask = int(val, 16)
    except ValueError:
        mask = None
    if not sep or not proc or mask is None or mask <= 0:
        raise argparse.ArgumentTypeError("want PROCESS[/THREAD]=MASK with a hex mask, got %r" % spec)
    return proc, thr or None, mask


def _timeline(model, a):
    from .analyses import Timeline
    return Timeline(model, getattr(a, "runmask", None) or [], getattr(a, "assume_unpinned", None) or [])


def _loss_summary(kev):
    return [{"cpu": l.cpu, "kind": l.kind, "count": l.count, "detail": l.detail} for l in kev.losses]


def cmd_info(a):
    kev, model, dt = _load(a.trace)
    st = kev.stats
    print(json.dumps({
        "file": a.trace, "version": "%d.%02d" % kev.version, "cpus": kev.ncpu, "cycles_per_sec": kev.cycles_per_sec,
        "system": " ".join(kev.attrs.get(k, "") for k in ("SYSNAME", "SYS_RELEASE", "MACHINE")).strip(),
        "tracelogger": kev.attrs.get("TRACELOGGER_ARGS"), "slots": st.slots, "events": st.events,
        "wide_mode": model.wide, "combine_events": st.combine_events, "partial_combines": st.partial_combines,
        "orphan_slots": st.orphan_slots, "stale_combines": st.stale_combines, "rollovers": st.rollovers,
        "interlaced_regressions": st.per_cpu_regressions, "losses": _loss_summary(kev),
        "lost_buffers": st.lost_buffers, "dropped_events": st.dropped_events, "short_buffers": st.short_buffers,
        "trailing_bytes": st.trailing_bytes, "unanchored_slots": st.unanchored_slots,
        "processes": len(model.processes), "threads": len(model.threads), "switches": len(model.switches),
        "ipc_transactions": len(model.txns), "trustworthy_transactions": model.counters["txns_trustworthy"],
        "pulses": len(model.pulses), "interrupts": len(model.interrupts),
        "counters": dict(sorted(model.counters.items())), "parse_seconds": round(dt, 2)}, indent=1))
    return 0 if st.events else 1


def cmd_convert(a):
    from .perfetto_out import write
    kev, model, _ = _load(a.trace)
    if not kev.events():
        print("error: %s has no events" % a.trace, file=sys.stderr)
        return 1
    out = a.output or a.trace.rsplit(".", 1)[0] + ".perfetto-trace"
    stats = write(model, out, critical_paths=not a.no_critical_path)
    print("wrote %s: %s" % (out, json.dumps(stats, sort_keys=True)))
    for l in kev.losses:
        print("warning: lost events on CPU %d: %s %s" % (l.cpu, l.kind, l.detail))
    if model.wide is False:
        print("warning: fast-mode trace: no priorities or _msg_info; links rely on COMM events only")
    n_bad = len(model.txns) - model.counters["txns_trustworthy"]
    if n_bad:
        print("note: %d of %d message exchanges are incomplete or carry warnings (see their 'warnings' argument)"
              % (n_bad, len(model.txns)))
    return 0


def _analysis(model, a, tl):
    """Everything analyze and diff report, for text and JSON alike; --client filters every per-client part."""
    from .analyses import (deadline_misses, ipc_latency, mutex_waits, server_priority_while_handling,
                           suspected_inversions, us)
    only = (lambda key: a.client in model.label(key)) if a.client else None
    out = {"trace": a.trace, "client_filter": a.client, "transactions": len(model.txns),
           "trustworthy": model.counters["txns_trustworthy"],
           "ambiguous_parent": sum(1 for t in model.txns if t.trustworthy and t.parent_candidates),
           "losses": _loss_summary(model.kev),
           "ipc_latency": [r for r in ipc_latency(model) if a.client is None or a.client in r["client"]]}
    out["inversions"] = suspected_inversions(model, tl=tl, min_us=a.min_us, only=only)
    out["fixed_priority_channels"] = [{"process": model.processes[k[0]].name if k[0] in model.processes else k[0],
                                       "pid": k[0], "chid": k[1], "flags": "0x%x" % v["flags"]}
                                      for k, v in sorted(model.fixed_priority_channels().items())]
    out["deadlines"] = {}
    for name, dl in a.deadline or []:
        out["deadlines"][name] = {"deadline_us": dl, "by_server": deadline_misses(model, name, dl, only)}
    sp = server_priority_while_handling(model, tl)
    out["server_priority_while_handling"] = [
        {"server": s, "client": c, "prio_us": {str(p): round(v, 1) for p, v in sorted(d.items())},
         "prio_share": {str(p): v / sum(d.values()) for p, v in sorted(d.items())} if sum(d.values()) > 0 else {}}
        for (s, c), d in sorted(sp.items()) if a.client is None or a.client in c]
    out["mutex_waits"] = mutex_waits(model, tl, a.min_us, only)
    return out


def cmd_analyze(a):
    from .analyses import Timeline, us
    from .critical import critical_path, render, to_json
    kev, model, _ = _load(a.trace)
    if not kev.events():
        print("error: %s has no events" % a.trace, file=sys.stderr)
        return 1
    tl = _timeline(model, a)
    out = _analysis(model, a, tl)
    picks = []
    for i in a.request or []:
        if not 0 <= i < len(model.txns):
            print("error: no transaction %d (trace has 0..%d)" % (i, len(model.txns) - 1), file=sys.stderr)
            return 2
        picks.append(model.txns[i])
    if a.slowest:
        top = sorted((t for t in model.txns if t.causal and t.parent is None),
                     key=lambda t: t.t_exit - t.t_call, reverse=True)
        picks += [t for t in top if a.client is None or a.client in model.label(t.client)][:a.slowest]
    cps = []
    for tx in picks:
        if tx.t_call is None or tx.t_exit is None:
            cps.append((tx, None))
        else:
            cps.append((tx, critical_path(model, tx, tl)))
    if a.json:
        out["critical_paths"] = [to_json(model, tx, segs) if segs is not None else
                                 {"txn": tx.idx, "error": "MsgSend entry or exit not traced"} for tx, segs in cps]
        print(json.dumps(out, indent=1, default=str))
        return 0
    p = print
    p("trace %s: %d message exchanges, %d trustworthy (analyses use only those)" % (
        a.trace, out["transactions"], out["trustworthy"]))
    if out["ambiguous_parent"]:
        p("  %d of them have an ambiguous parent: kept in latency, left out of critical paths and inversions"
          % out["ambiguous_parent"])
    for l in out["losses"]:
        p("  lost events on CPU %d: %s %s" % (l["cpu"], l["kind"], l["detail"]))
    rows = sorted((r for r in out["ipc_latency"] if r["n"]), key=lambda r: -r["max_us"])[:a.top]
    p("\nMsgSend latency of trustworthy requests, %d slowest client@priority -> server pairs (us):" % len(rows))
    p("  %-34s %-22s %5s %9s %9s %9s %9s  %s" % ("client", "server", "n", "p50", "p90", "p99", "max",
                                               "excluded/unfinished"))
    for r in rows:
        p("  %-34s %-22s %5d %9.1f %9.1f %9.1f %9.1f  %d/%d%s" % (
            r["client"][:34], r["server"][:22], r["n"], r["p50_us"], r["p90_us"], r["p99_us"], r["max_us"],
            r["excluded"], r["unfinished"], "  (%d threads)" % r["client_threads"] if r["client_threads"] > 1 else ""))
    for name, d in out["deadlines"].items():
        for srv, r in d["by_server"].items():
            p("\ndeadline %s=%g us on %s: %d of %d requests missed, max %.1f us%s%s" % (
                name, d["deadline_us"], srv, r["missed"], r["requests"], r["max_us"],
                " (%d excluded as untrustworthy)" % r["excluded"] if r["excluded"] else "",
                " (%d unfinished)" % r["unfinished"] if r["unfinished"] else ""))
    inv = out["inversions"]
    p("\nsuspected priority inversions: %d (checked %d exchanges, %d excluded as untrustworthy, %d with an ambiguous "
      "parent, threshold %g us)" % (len(inv["suspected"]), inv["checked"], inv["excluded_untrustworthy"],
                                    inv["excluded_ambiguous_parent"], inv["min_us"]))
    if inv["suspected_using_assumptions"]:
        p("  %d of them rely on --runmask or --assume-unpinned" % inv["suspected_using_assumptions"])
    for r in inv["suspected"][:a.top]:
        p("  txn %d: %s" % (r["txn"], r["evidence"]))
    if len(inv["suspected"]) > a.top:
        p("  ... %d more (use --json)" % (len(inv["suspected"]) - a.top))
    if inv["insufficient_evidence"]:
        p("insufficient evidence (the trace cannot show whether the server was held up): %d" %
          len(inv["insufficient_evidence"]))
        for r in inv["insufficient_evidence"][:a.top]:
            p("  txn %d: %s -> %s, %.1f us unexplained: %s" % (r["txn"], r["client"], r["server"], r["unexplained_us"],
                                                              "; ".join("%s %.1f us" % kv for kv in r["reasons"].items())))
    if out["fixed_priority_channels"]:
        p("\nchannels created with _NTO_CHF_FIXED_PRIORITY: " + ", ".join(
            "%s chid %d" % (c["process"], c["chid"]) for c in out["fixed_priority_channels"]))
    mw = out["mutex_waits"]
    if mw:
        from .analyses import mutex_wait_text
        p("\nmutex waits of at least %g us: %d (owner known for %d)" % (a.min_us, len(mw),
                                                                     sum(1 for r in mw if r["owner"])))
        for r in sorted(mw, key=lambda r: -r["blocked_us"])[:a.top]:
            p("  wait %d: %s" % (r["wait"], mutex_wait_text(r)))
        if len(mw) > a.top:
            p("  ... %d more (use --json)" % (len(mw) - a.top))
    for tx, segs in cps:
        p("")
        if segs is None:
            p("txn %d: MsgSend entry or exit not traced" % tx.idx)
        else:
            for line in render(model, tx, segs):
                p(line)
    return 0


def cmd_diff(a):
    sides = []
    for path in (a.before, a.after):
        kev, model, _ = _load(path)
        if not kev.events():
            print("error: %s has no events" % path, file=sys.stderr)
            return 1
        a.trace = path
        sides.append(_analysis(model, a, _timeline(model, a)))
    A, B = sides
    cmp = _compare(A, B)
    if a.json:
        print(json.dumps({"before": A, "after": B, "comparison": cmp}, indent=1, default=str))
        return 0
    p = print
    p("before: %s\nafter:  %s" % (a.before, a.after))
    if a.client:
        p("client filter: %r (applies to every row below)" % a.client)
    p("message exchanges: before %d (%d trustworthy, %d loss records) | after %d (%d trustworthy, %d loss records)" % (
        A["transactions"], A["trustworthy"], len(A["losses"]), B["transactions"], B["trustworthy"], len(B["losses"])))
    for name, d in A["deadlines"].items():
        for srv in sorted(set(d["by_server"]) | set(B["deadlines"][name]["by_server"])):
            ra, rb = d["by_server"].get(srv), B["deadlines"][name]["by_server"].get(srv)
            fmt = lambda r: "%d/%d missed, max %.1f us%s" % (
                r["missed"], r["requests"], r["max_us"],
                ", %d excluded, %d unfinished" % (r["excluded"], r["unfinished"])
                if r["excluded"] or r["unfinished"] else "") if r else "-"
            p("deadline %s=%g us on %s: before %s | after %s" % (name, d["deadline_us"], srv, fmt(ra), fmt(rb)))
    ia, ib = A["inversions"], B["inversions"]
    p("suspected priority inversions: before %d | after %d (checked %d | %d exchanges; insufficient evidence: %d | %d; "
      "relying on supplied run masks: %d | %d)" % (
        len(ia["suspected"]), len(ib["suspected"]), ia["checked"], ib["checked"], len(ia["insufficient_evidence"]),
        len(ib["insufficient_evidence"]), ia["suspected_using_assumptions"], ib["suspected_using_assumptions"]))
    fc = lambda S: ", ".join("%s chid %d" % (c["process"], c["chid"]) for c in S["fixed_priority_channels"]) or "none"
    p("fixed-priority channels: before %s | after %s" % (fc(A), fc(B)))
    la = {(r["client"], r["server"]): r for r in A["ipc_latency"]}
    lb = {(r["client"], r["server"]): r for r in B["ipc_latency"]}
    lat = cmp["latency"]
    p("\nMsgSend latency (us) by client@priority -> server: %d pairs compared (trustworthy requests on both sides), "
      "%d without trustworthy requests on a side, %d only before, %d only after" % (
          len(lat["compared"]), len(lat["no_samples_on_a_side"]), len(lat["only_before"]), len(lat["only_after"])))
    head = "  %-30s %-16s %-6s %5s %9s %9s %9s %9s  %s" % ("client@priority", "server", "", "n", "p50", "p90", "p99",
                                                          "max", "excluded/unfinished")

    def row(k, tag, r):
        if r is None:
            p("  %-30s %-16s %-6s %5s" % (k[0][:30], k[1][:16], tag, "-"))
        elif r["n"]:
            p("  %-30s %-16s %-6s %5d %9.1f %9.1f %9.1f %9.1f  %d/%d" % (
                k[0][:30], k[1][:16], tag, r["n"], r["p50_us"], r["p90_us"], r["p99_us"], r["max_us"],
                r["excluded"], r["unfinished"]))
        else:
            p("  %-30s %-16s %-6s %5d %39s  %d/%d" % (k[0][:30], k[1][:16], tag, 0, "", r["excluded"], r["unfinished"]))
    for title, keys in (("largest p99 change", lat["compared"]), ("without trustworthy requests on a side",
                        lat["no_samples_on_a_side"]), ("only before (removed)", lat["only_before"]),
                        ("only after (added)", lat["only_after"])):
        if not keys:
            continue
        p("%s%s:" % (title, ", first %d" % a.top if len(keys) > a.top else ""))
        p(head)
        for k in keys[:a.top]:
            row(k, "before", la.get(tuple(k)))
            row(k, "after", lb.get(tuple(k)))
    pr = cmp["server_priority"]
    if pr:
        p("\nserver priority while handling each client's request (share of the server's running time, changes of "
          "1 point or more):")
        share = lambda d: ", ".join("prio %s %.0f%%" % (k, 100 * v) for k, v in sorted(d.items(), key=lambda kv: int(kv[0]))
                                    ) if d else "-"
        for x in pr[:a.top]:
            p("  %s for %s: before %s | after %s" % (x["server"], x["client"], share(x["before"]), share(x["after"])))
    return 0


def _compare(A, B):
    """Latency rows by role (compared, no samples on a side, only before, only after) and server priority shares
    that changed, from both sides' analyses."""
    la = {(r["client"], r["server"]): r for r in A["ipc_latency"]}
    lb = {(r["client"], r["server"]): r for r in B["ipc_latency"]}
    both = set(la) & set(lb)
    compared = sorted((k for k in both if la[k]["n"] and lb[k]["n"]),
                      key=lambda k: (-abs(la[k]["p99_us"] - lb[k]["p99_us"]), k))
    sa = {(r["server"], r["client"]): r for r in A["server_priority_while_handling"]}
    sb = {(r["server"], r["client"]): r for r in B["server_priority_while_handling"]}
    pr = []
    for k in set(sa) | set(sb):
        da, db = (sa[k]["prio_share"] if k in sa else {}), (sb[k]["prio_share"] if k in sb else {})
        delta = max(abs(da.get(q, 0.0) - db.get(q, 0.0)) for q in set(da) | set(db)) if da or db else 0.0
        if set(da) != set(db) or delta >= 0.01:
            total = sum((sa[k]["prio_us"] if k in sa else {}).values()) + sum((sb[k]["prio_us"] if k in sb else {}).values())
            pr.append({"server": k[0], "client": k[1], "before": da, "after": db, "max_share_change": delta,
                       "running_us": total})
    pr.sort(key=lambda x: (-x["running_us"], x["server"], x["client"]))
    return {"latency": {"compared": compared,
                        "no_samples_on_a_side": sorted(k for k in both if not (la[k]["n"] and lb[k]["n"])),
                        "only_before": sorted(set(la) - set(lb)), "only_after": sorted(set(lb) - set(la))},
            "server_priority": pr}


def cmd_validate(a):
    from .validate import validate
    kev, model, _ = _load(a.trace)
    try:
        res, _, _ = validate(model, a.logs)
    except (OSError, KeyError, ValueError, csv.Error) as e:
        print("error: cannot read workload logs in %s: %s" % (a.logs, e), file=sys.stderr)
        return 1
    res["match_rate"] = round(res["matched"] / res["requests"], 4) if res["requests"] else None
    print(json.dumps(res, indent=1, default=str))
    if not res["requests"]:
        print("error: no logged requests in %s" % a.logs, file=sys.stderr)
    return 0 if res["ok"] else 1


def _regex(text):
    try:
        re.compile(text)
    except re.error as e:
        raise argparse.ArgumentTypeError("not a regular expression: %r (%s)" % (text, e))
    return text


def cmd_open(a):
    import getpass
    import os
    import socket
    import webbrowser
    from . import __version__
    from .serve import cache_path, default_pins, make_server, serve_until_delivered, trace_complete, write_atomic
    pins, labels = list(a.pin or []), []
    if a.trace.endswith((".perfetto-trace", ".pftrace")):
        out = a.trace
        with open(out, "rb") as f:
            data = f.read()
        if not pins:
            print("note: no tracks pinned (give --pin REGEX; defaults need the .kev)")
    else:
        out = cache_path(a.trace, __version__)      # private cache: ~/.cache/tracestudio, mode 0700
        data = None
        if os.path.exists(out):
            with open(out, "rb") as f:
                data = f.read()
            if not trace_complete(data):
                print("note: cached %s is incomplete; converting again" % out)
                data = None
        if data is None or not pins:
            kev, model, _ = _load(a.trace)
            if not kev.events():
                print("error: %s has no events" % a.trace, file=sys.stderr)
                return 1
            if data is None:
                from .perfetto_out import Writer
                data = Writer(model, True, None).build()
                write_atomic(out, data)
                print("converted %s -> %s" % (a.trace, out))
            else:
                print("using %s (converted earlier from this trace)" % out)
            if not pins:
                pins, labels = default_pins(model, a.deadline or [])
        else:
            print("using %s (converted earlier from this trace)" % out)
    try:
        srv = make_server(data, os.path.basename(a.trace), os.path.basename(a.trace) + ".perfetto-trace", pins, a.port)
    except OSError as e:
        print("error: cannot listen on 127.0.0.1:%d (%s); choose another --port" % (a.port, e), file=sys.stderr)
        return 1
    url, port = srv.url, srv.server_address[1]
    print("serving %s (%d bytes) at %s (this machine only)" % (out, len(data), url))
    if pins:
        print("pinning tracks matching %s%s" % (", ".join(pins), " (clients %s)" % ", ".join(labels) if labels else ""))
    if a.no_browser or not webbrowser.open(url):
        print("open %s in a browser on this machine; from another machine, forward the port first:" % url)
        print("  ssh -N -L %d:127.0.0.1:%d %s@%s     then open the same URL there" % (
            port, port, getpass.getuser(), socket.gethostname()))
    print("waiting for the browser (Ctrl-C to stop) ...", flush=True)
    if serve_until_delivered(srv, a.timeout):
        print("trace handed to Perfetto (%s bytes); the UI loads it in its tab" % (
            srv.handed_bytes if srv.handed_bytes is not None else len(data)))
        return 0
    print("stopped before the trace was handed to Perfetto", file=sys.stderr)
    return 1


def cmd_check(a):
    from . import __version__
    from .check import junit_error, junit_xml, render, run_checks, write_report
    if not (a.deadline or a.max_latency or a.no_inversions or a.max_insufficient is not None):
        print("error: nothing to check: give --deadline, --max-latency, --no-inversions or --max-insufficient",
              file=sys.stderr)
        return 2
    t0 = time.time()
    try:
        kev, model, _ = _load(a.trace)
        if not kev.events():
            raise KevError("the trace has no events")
    except (KevError, OSError, EOFError) as e:       # still leave a report, so CI never reads a stale one
        msg = "cannot check %s: %s" % (a.trace, e)
        print("error: " + msg, file=sys.stderr)
        if a.junit:
            write_report(a.junit, junit_error(a.trace, msg, time.time() - t0, __version__))
            print("wrote %s" % a.junit)
        return 1
    results = run_checks(model, _timeline(model, a), a)
    for line in render(results):
        print(line)
    if a.junit:
        write_report(a.junit, junit_xml(results, a.trace, time.time() - t0, __version__))
        print("wrote %s" % a.junit)
    return 0 if all(r.ok for r in results) else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="tracestudio", description="QNX .kev kernel traces to Perfetto, with analyses")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info", help="header, counts, losses and decoder counters as JSON")
    s.add_argument("trace")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("convert", help="write a Perfetto trace")
    s.add_argument("trace")
    s.add_argument("-o", "--output")
    s.add_argument("--no-critical-path", action="store_true", help="skip the per-request critical path tracks")
    s.set_defaults(fn=cmd_convert)
    for name, fn in (("analyze", cmd_analyze), ("diff", cmd_diff)):
        s = sub.add_parser(name, help="latency, deadlines, inversions, critical paths" if name == "analyze"
                           else "compare two traces (before and after a change)")
        if name == "analyze":
            s.add_argument("trace")
            s.add_argument("--request", type=_count, action="append", metavar="TXN",
                           help="critical path of this transaction (the 'txn' argument in Perfetto); repeatable")
            s.add_argument("--slowest", type=_count, default=0, metavar="N",
                           help="critical paths of the N slowest top-level requests")
        else:
            s.add_argument("before")
            s.add_argument("after")
        s.add_argument("--deadline", action="append", metavar="THREAD[@SERVER]=US", type=_deadline_spec,
                       help="count requests of client threads with this name (to this server process only, with "
                            "@SERVER) slower than US microseconds")
        s.add_argument("--client", help="only client threads whose label contains this text")
        s.add_argument("--min-us", type=_us, default=100.0, help="inversion evidence threshold (default 100)")
        s.add_argument("--runmask", action="append", metavar="PROCESS[/THREAD]=MASK", type=_runmask_spec,
                       help="run mask (hex) set before the trace started, from the program's code or configuration "
                            "(the trace records only masks set while tracing); used until the trace shows a mask "
                            "event for the thread or loses events, and named in every verdict that uses it")
        s.add_argument("--assume-unpinned", action="append", metavar="PROCESS",
                       help="treat this process's threads as allowed on every CPU (your statement, not the trace's)")
        s.add_argument("--top", type=_count, default=15, help="rows to print (default 15)")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("validate", help="check decoded exchanges against workload ground-truth logs")
    s.add_argument("trace")
    s.add_argument("--logs", required=True)
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("open", help="open a trace in ui.perfetto.dev with the tracks that matter pinned")
    s.add_argument("trace", help=".kev (converted and cached next to it) or .perfetto-trace")
    s.add_argument("--pin", action="append", type=_regex, metavar="REGEX",
                   help="pin tracks whose name matches (repeatable); default: the critical-path and QNX state tracks "
                        "of the clients that missed a --deadline, else of the two with the slowest MsgSend")
    s.add_argument("--deadline", action="append", metavar="THREAD=US", type=_deadline_spec,
                   help="choose the default pins among the client threads with this name that missed it")
    s.add_argument("--port", type=_count, default=8765, help="local port (default 8765; 0 picks a free one)")
    s.add_argument("--no-browser", action="store_true",
                   help="do not start a browser; print the URL and how to forward the port from another machine")
    s.add_argument("--timeout", type=_count, metavar="SECONDS", help="give up after this many seconds")
    s.set_defaults(fn=cmd_open)
    s = sub.add_parser("check", help="real-time assertions: exit status 1 on any failure, JUnit XML for CI")
    s.add_argument("trace")
    s.add_argument("--deadline", action="append", metavar="THREAD[@SERVER]=US", type=_deadline_spec,
                   help="every trustworthy MsgSend of client threads with this name (to this server process only, "
                        "with @SERVER) ends within US microseconds")
    s.add_argument("--max-latency", action="append", metavar="CLIENT[@SERVER]=US", type=_deadline_spec,
                   help="the same for client threads whose label (process/thread) contains CLIENT")
    s.add_argument("--min-requests", type=_count, default=1, metavar="N",
                   help="each --deadline and --max-latency needs at least N trustworthy requests (default 1)")
    s.add_argument("--min-checked", type=_count, default=1, metavar="N",
                   help="inversion assertions need at least N exchanges checked by the analysis (default 1)")
    s.add_argument("--no-inversions", action="store_true", help="no suspected priority inversion")
    s.add_argument("--max-insufficient", type=_count, metavar="N",
                   help="at most N requests with insufficient evidence for the inversion analysis (default 0 "
                        "with --no-inversions)")
    s.add_argument("--client", metavar="TEXT",
                   help="limit the inversion assertions to client threads whose label contains TEXT (as for analyze)")
    s.add_argument("--allow-loss", action="store_true",
                   help="do not fail because of lost events or untrustworthy requests (they are still reported)")
    s.add_argument("--min-us", type=_us, default=100.0, help="inversion evidence threshold (default 100)")
    s.add_argument("--runmask", action="append", metavar="PROCESS[/THREAD]=MASK", type=_runmask_spec,
                   help="run mask set before the trace started, as for analyze")
    s.add_argument("--assume-unpinned", action="append", metavar="PROCESS", help="as for analyze")
    s.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report")
    s.set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except KevError as e:
        print("error: %s: %s" % (getattr(a, "trace", ""), e), file=sys.stderr)
        return 1
    except (OSError, EOFError) as e:      # unreadable input, unwritable output, truncated gzip
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
