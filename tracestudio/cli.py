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
import json
import sys
import time

from .kev import KevError, KevFile
from .model import Model


def _load(path):
    t0 = time.time()
    kev = KevFile(path)
    model = Model(kev)
    return kev, model, time.time() - t0


def _deadlines(specs):
    out = []
    for spec in specs or []:
        name, sep, val = spec.rpartition("=")
        if not sep or not name:
            raise SystemExit("--deadline wants THREAD=MICROSECONDS, got %r" % spec)
        out.append((name, float(val)))
    return out


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
    from .analyses import deadline_misses, ipc_latency, server_priority_while_handling, suspected_inversions, us
    out = {"trace": a.trace, "transactions": len(model.txns), "trustworthy": model.counters["txns_trustworthy"],
           "ambiguous_parent": sum(1 for t in model.txns if t.trustworthy and t.parent_candidates),
           "losses": _loss_summary(model.kev), "ipc_latency": ipc_latency(model)}
    out["inversions"] = suspected_inversions(model, tl=tl, min_us=a.min_us)
    out["fixed_priority_channels"] = [{"process": model.processes[k[0]].name if k[0] in model.processes else k[0],
                                       "pid": k[0], "chid": k[1], "flags": "0x%x" % v["flags"]}
                                      for k, v in sorted(model.fixed_priority_channels().items())]
    out["deadlines"] = {}
    for name, dl in _deadlines(a.deadline):
        out["deadlines"][name] = {"deadline_us": dl, "by_server": deadline_misses(model, name, dl)}
    sp = server_priority_while_handling(model, tl)
    out["server_priority_while_handling"] = [
        {"server": s, "client": c, "prio_us": {str(p): round(v, 1) for p, v in sorted(d.items())}}
        for (s, c), d in sorted(sp.items())]
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
    rows = out["ipc_latency"]
    if a.client:
        rows = [r for r in rows if a.client in r["client"]]
    rows = sorted((r for r in rows if r["n"]), key=lambda r: -r["max_us"])[:a.top]
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
    if a.json:
        print(json.dumps({"before": A, "after": B}, indent=1, default=str))
        return 0
    p = print
    p("before: %s\nafter:  %s" % (a.before, a.after))
    for name, d in A["deadlines"].items():
        for srv in sorted(set(d["by_server"]) | set(B["deadlines"][name]["by_server"])):
            ra, rb = d["by_server"].get(srv), B["deadlines"][name]["by_server"].get(srv)
            fmt = lambda r: "%d/%d missed, max %.1f us%s" % (
                r["missed"], r["requests"], r["max_us"],
                ", %d excluded, %d unfinished" % (r["excluded"], r["unfinished"])
                if r["excluded"] or r["unfinished"] else "") if r else "-"
            p("deadline %s=%g us on %s: before %s | after %s" % (name, d["deadline_us"], srv, fmt(ra), fmt(rb)))
    p("suspected priority inversions: before %d | after %d (insufficient evidence: %d | %d; relying on supplied "
      "run masks: %d | %d)" % (
        len(A["inversions"]["suspected"]), len(B["inversions"]["suspected"]),
        len(A["inversions"]["insufficient_evidence"]), len(B["inversions"]["insufficient_evidence"]),
        A["inversions"]["suspected_using_assumptions"], B["inversions"]["suspected_using_assumptions"]))
    fc = lambda S: ", ".join("%s chid %d" % (c["process"], c["chid"]) for c in S["fixed_priority_channels"]) or "none"
    p("fixed-priority channels: before %s | after %s" % (fc(A), fc(B)))
    # rows are matched by role: client process, thread name and priority, and server process
    la = {(r["client"], r["server"]): r for r in A["ipc_latency"] if r["n"]}
    lb = {(r["client"], r["server"]): r for r in B["ipc_latency"] if r["n"]}
    keys = [k for k in set(la) | set(lb) if a.client is None or a.client in k[0]]
    keys.sort(key=lambda k: -abs((la.get(k) or {"p99_us": 0})["p99_us"] - (lb.get(k) or {"p99_us": 0})["p99_us"]))
    p("\nMsgSend latency (us), %d pairs with the largest p99 change:" % min(len(keys), a.top))
    p("  %-30s %-16s %-6s %5s %9s %9s %9s %9s  %s" % ("client@priority", "server", "", "n", "p50", "p90", "p99",
                                                    "max", "excluded/unfinished"))
    for k in keys[:a.top]:
        for tag, r in (("before", la.get(k)), ("after", lb.get(k))):
            if r:
                p("  %-30s %-16s %-6s %5d %9.1f %9.1f %9.1f %9.1f  %d/%d" % (
                    k[0][:30], k[1][:16], tag, r["n"], r["p50_us"], r["p90_us"], r["p99_us"], r["max_us"],
                    r["excluded"], r["unfinished"]))
            else:
                p("  %-30s %-16s %-6s %5s" % (k[0][:30], k[1][:16], tag, "-"))
    sa = {(r["server"], r["client"]): r["prio_us"] for r in A["server_priority_while_handling"]}
    sb = {(r["server"], r["client"]): r["prio_us"] for r in B["server_priority_while_handling"]}
    keys = sorted((k for k in set(sa) | set(sb) if (a.client is None or a.client in k[1])
                   and set(sa.get(k, {})) != set(sb.get(k, {}))),
                  key=lambda k: -(sum(sa.get(k, {}).values()) + sum(sb.get(k, {}).values())))
    if keys:
        p("\nserver priority while handling each client's request (share of the server's running time):")
        share = lambda d: ", ".join("prio %s %.0f%%" % (pr, 100 * v / sum(d.values()))
                                    for pr, v in sorted(d.items(), key=lambda kv: int(kv[0]))) if d else "-"
        for k in keys[:a.top]:
            p("  %s for %s: before %s | after %s" % (k[0], k[1], share(sa.get(k)), share(sb.get(k))))
    return 0


def cmd_validate(a):
    from .validate import validate
    kev, model, _ = _load(a.trace)
    try:
        res, _, _ = validate(model, a.logs)
    except (OSError, KeyError, ValueError) as e:
        print("error: cannot read workload logs in %s: %s" % (a.logs, e), file=sys.stderr)
        return 1
    res["match_rate"] = round(res["matched"] / res["requests"], 4) if res["requests"] else None
    print(json.dumps(res, indent=1, default=str))
    if not res["requests"]:
        print("error: no logged requests in %s" % a.logs, file=sys.stderr)
    return 0 if res["ok"] else 1


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
            s.add_argument("--request", type=int, action="append", metavar="TXN",
                           help="critical path of this transaction (the 'txn' argument in Perfetto); repeatable")
            s.add_argument("--slowest", type=int, default=0, metavar="N",
                           help="critical paths of the N slowest top-level requests")
        else:
            s.add_argument("before")
            s.add_argument("after")
        s.add_argument("--deadline", action="append", metavar="THREAD=US",
                       help="count requests of client threads with this name slower than US microseconds")
        s.add_argument("--client", help="only client threads whose label contains this text")
        s.add_argument("--min-us", type=float, default=100.0, help="inversion evidence threshold (default 100)")
        s.add_argument("--runmask", action="append", metavar="PROCESS[/THREAD]=MASK", type=_runmask_spec,
                       help="run mask (hex) set before the trace started, from the program's code or configuration "
                            "(the trace records only masks set while tracing); used until the trace shows a mask "
                            "event for the thread or loses events, and named in every verdict that uses it")
        s.add_argument("--assume-unpinned", action="append", metavar="PROCESS",
                       help="treat this process's threads as allowed on every CPU (your statement, not the trace's)")
        s.add_argument("--top", type=int, default=15, help="rows to print (default 15)")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("validate", help="check decoded exchanges against workload ground-truth logs")
    s.add_argument("trace")
    s.add_argument("--logs", required=True)
    s.set_defaults(fn=cmd_validate)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except KevError as e:
        print("error: %s: %s" % (getattr(a, "trace", ""), e), file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
