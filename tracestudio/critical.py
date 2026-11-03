# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Critical path of one request: where a client's MsgSend latency went.

The window from the client's MsgSendv entry to its exit is partitioned into
consecutive segments that never overlap, so the durations add up to the
latency exactly:
  client running     the client thread executes (kernel call entry and exit)
  client ready       the client is runnable while other threads hold the CPUs it may use
  server busy        the receiving thread runs, but not this request (SEND-blocked client)
  server waiting     the receiving thread is still RECEIVE-blocked (message not yet delivered)
  server preempted   the server thread is READY while other threads hold its CPUs
  server running     the server thread receives, runs or replies to this request
  nested request     the server thread waits in its own request to another server;
                     that request's critical path is nested inside, recursively
  server blocked     the server thread is blocked in another state (MUTEX, ...) or in a
                     request that is not part of this one
  unknown            no events (lost data) or nothing known yet
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analyses import Timeline, us
from .events import state_name
from .model import ST_READY, ST_RECEIVE, ST_REPLY, ST_RUNNING, ST_SEND, ST_UNKNOWN, UNKNOWN


@dataclass
class Segment:
    t0: int
    t1: int
    kind: str
    label: str
    thread: tuple | None = None   # the thread whose time this is (runner, or the thread waited for)
    prio: int | None = None
    txn: int | None = None
    children: list = field(default_factory=list)
    held_by: dict = field(default_factory=dict)   # for READY waits: who held the CPUs -> cycles


def _clip(segs, a, b):
    out = []
    for s in segs:
        lo, hi = max(s.t0, a), min(s.t1, b)
        if hi > lo:
            out.append(Segment(lo, hi, s.kind, s.label, s.thread, s.prio, s.txn, _clip(s.children, lo, hi),
                               dict(s.held_by)))
    return out


def _held(tl, key, a, b):
    """Who held the CPUs `key` may run on during [a, b]: [(e, f, text, main thread, its prio)]."""
    m = tl.m
    cuts = {a, b}
    for cpu in tl.cpus:
        cuts.update(tl.switch_times(cpu, a, b))
    cuts.update(tl.runmask_changes(key, a, b))
    cuts = sorted(cuts)
    for e, f in zip(cuts, cuts[1:]):
        mask = tl.runmask_at(key, e)
        cpus = [x for x in tl.cpus if mask is None or (mask >> x) & 1]
        occ = [(x, tl.occupant(x, e)) for x in cpus]
        known = [(x, o, tl.prio_at(o, e)) for x, o in occ if o not in (None, UNKNOWN)]
        if len(known) < len(occ):
            yield e, f, "occupant of a CPU it may use unknown", None, None
            continue
        known.sort(key=lambda r: -(r[2] if r[2] is not None else -1))
        x, o, p = known[0]
        if len(known) == 1 and o == key:
            yield e, f, "the kernel on CPU %d (switching)" % x, None, None
        elif len(known) == 1:
            yield e, f, "%s (prio %s) on CPU %d" % (m.label(o), p, x), o, p
        else:
            yield e, f, " + ".join("%s (prio %s) on CPU %d" % (m.label(o2), p2, x2) for x2, o2, p2 in known), o, p


def critical_path(model, tx, tl=None, max_depth=4, _depth=0):
    """Segments covering [tx.t_call, tx.t_exit] of one transaction."""
    tl = tl or Timeline(model)
    m = model
    segs = []
    client, srv = tx.client, tx.server
    cl = m.label(client)

    def add(a, b, kind, label, thread=None, prio=None, children=None):
        if b <= a:
            return
        last = segs[-1] if segs else None
        if last and not children and not last.children and last.kind == kind and last.label == label and last.t1 == a:
            last.t1 = b
            return
        segs.append(Segment(a, b, kind, label, thread, prio, tx.idx, children or []))

    def add_wait(a, b, kind, waiter, wprio, holder):
        """READY waits merge while the waiting thread and its priority stay the same."""
        if b <= a:
            return
        last = segs[-1] if segs else None
        if last and last.kind == kind and last.thread == waiter and last.prio == wprio and last.t1 == a:
            last.t1 = b
        else:
            last = Segment(a, b, kind, "", waiter, wprio, tx.idx)
            segs.append(last)
        last.held_by[holder] = last.held_by.get(holder, 0) + (b - a)

    def server_part(a, b):
        if srv is None:
            add(a, b, "unknown", "receiving server thread not identified")
            return
        sl = m.label(srv)
        handled = tl._handled(srv)
        for c, d, scode, sprio in tl.segments(srv, a, b):
            if scode == ST_RUNNING:
                cuts = {c, d}
                for h in handled:
                    for t in (h.t_rx_exit, h.t_reply_call):
                        if c < t < d:
                            cuts.add(t)
                cuts = sorted(cuts)
                for e, f in zip(cuts, cuts[1:]):
                    h = tl.handling_at(srv, e)
                    if h is tx:
                        add(e, f, "server running", "%s running this request" % sl, srv, sprio)
                    elif h is not None:
                        add(e, f, "server busy", "%s running %s's request (txn %d)" % (sl, m.label(h.client), h.idx),
                            srv, sprio)
                    elif tx.t_rx_exit is not None and e < tx.t_rx_exit and (
                            not handled or not any(x.t_rx_exit > e and x.t_rx_exit < tx.t_rx_exit for x in handled)):
                        add(e, f, "server running", "%s receiving this request" % sl, srv, sprio)
                    elif tx.t_reply_call is not None and e >= tx.t_reply_call:
                        add(e, f, "server running", "%s replying" % sl, srv, sprio)
                    else:
                        add(e, f, "server busy", "%s running, not in a request" % sl, srv, sprio)
            elif scode == ST_READY:
                for e, f, text, who, p in _held(tl, srv, c, d):
                    add_wait(e, f, "server preempted", srv, sprio, text)
            elif scode in (ST_SEND, ST_REPLY):
                k = next((t for t in m.txns if t.client == srv and t.t_call is not None and t.t_call <= c
                          and (t.t_exit is None or c < t.t_exit)), None)
                if k is not None and k.parent == tx.idx and k.t_exit is not None and _depth < max_depth:
                    kids = _clip(critical_path(m, k, tl, max_depth, _depth + 1), c, d)
                    add(c, d, "nested request", "%s waits for %s (txn %d)" % (sl, m.server_name(k), k.idx),
                        srv, sprio, kids)
                elif k is not None:
                    add(c, d, "server blocked", "%s %s-blocked on %s (txn %d%s)" % (
                        sl, state_name(scode), m.server_name(k), k.idx,
                        "" if k.parent == tx.idx else ", not part of this request"), srv, sprio)
                else:
                    add(c, d, "server blocked", "%s %s-blocked" % (sl, state_name(scode)), srv, sprio)
            elif scode == ST_RECEIVE:
                add(c, d, "server waiting", "%s RECEIVE-blocked" % sl, srv, sprio)
            elif scode in (None, ST_UNKNOWN):
                add(c, d, "unknown", "no events for %s" % sl, srv)
            else:
                add(c, d, "server blocked", "%s blocked in %s" % (sl, state_name(scode)), srv, sprio)

    for a, b, code, prio in tl.segments(client, tx.t_call, tx.t_exit):
        if code == ST_RUNNING:
            add(a, b, "client running", "%s running" % cl, client, prio)
        elif code == ST_READY:
            for e, f, text, who, p in _held(tl, client, a, b):
                add_wait(e, f, "client ready", client, prio, text)
        elif code in (ST_SEND, ST_REPLY):
            server_part(a, b)
        elif code in (None, ST_UNKNOWN):
            add(a, b, "unknown", "no events for %s" % cl, client)
        else:
            add(a, b, "client blocked", "%s blocked in %s" % (cl, state_name(code)), client, prio)
    for sg in segs:
        if sg.held_by:
            top = sorted(sg.held_by.items(), key=lambda kv: -kv[1])
            if len(top) == 1:
                sg.label = "%s READY at prio %s, CPU held by %s" % (m.label(sg.thread), sg.prio, top[0][0])
            else:
                more = sum(v for _, v in top[3:])
                sg.label = "%s READY at prio %s, CPUs held by %s%s" % (
                    m.label(sg.thread), sg.prio, ", ".join("%s %.1f us" % (k, us(m, v)) for k, v in top[:3]),
                    ", %d more %.1f us" % (len(top) - 3, us(m, more)) if more else "")
    return segs


def totals(model, segs):
    out = {}
    for s in segs:
        out[s.kind] = out.get(s.kind, 0.0) + us(model, s.t1 - s.t0)
    return {k: round(v, 1) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def to_json(model, tx, segs):
    def seg(s):
        d = {"start_us": round(us(model, s.t0 - tx.t_call), 1), "us": round(us(model, s.t1 - s.t0), 1),
             "kind": s.kind, "what": s.label, "txn": s.txn}
        if s.thread is not None:
            d["thread"], d["prio"] = model.label(s.thread), s.prio
        if s.held_by:
            d["cpu_held_by_us"] = {k: round(us(model, v), 1) for k, v in sorted(s.held_by.items(), key=lambda kv: -kv[1])}
        if s.children:
            d["nested"] = [seg(c) for c in s.children]
        return d
    return {"txn": tx.idx, "client": model.label(tx.client), "server": model.server_name(tx),
            "message": tx.msg_type, "latency_us": round(us(model, tx.t_exit - tx.t_call), 1),
            "trustworthy": tx.trustworthy, "warnings": tx.warnings, "by_kind_us": totals(model, segs),
            "segments": [seg(s) for s in segs]}


def render(model, tx, segs):
    lat = us(model, tx.t_exit - tx.t_call)
    lines = ["txn %d: %s -> %s%s, MsgSend latency %.1f us%s" % (
        tx.idx, model.label(tx.client), model.server_name(tx), " (%s)" % tx.msg_type if tx.msg_type else "", lat,
        "" if tx.trustworthy else "  [not trustworthy: %s]" % "; ".join(tx.warnings or ["incomplete"]))]
    lines.append("   start us       us      %  what")

    def walk(ss, depth):
        for s in ss:
            lines.append("%9.1f %8.1f %6.1f  %s%s" % (us(model, s.t0 - tx.t_call), us(model, s.t1 - s.t0),
                                                    100 * (s.t1 - s.t0) / (tx.t_exit - tx.t_call),
                                                    "  " * depth, s.label))
            walk(s.children, depth + 1)
    walk(segs, 0)
    lines.append("   by kind: " + ", ".join("%s %.1f us" % kv for kv in totals(model, segs).items()))
    return lines
