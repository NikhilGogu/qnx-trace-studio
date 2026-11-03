# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Critical path of one request: where a client's MsgSend latency went.

The window from the client's MsgSendv entry to its exit is partitioned into
consecutive segments that never overlap, so the durations add up to the
latency exactly:
  client running     the client thread executes (kernel call entry and exit)
  client ready       the client is runnable while threads at or above its priority hold the CPUs it may use
  unexplained ready  a thread is runnable but the trace does not show why it did not run (an idle,
                     lower-priority or unknown occupant, or no CPU it is known to be allowed on)
  server busy        the receiving thread runs, but not this request (SEND-blocked client),
                     or it holds several received requests and the trace does not say which one it serves
  server waiting     the receiving thread is still RECEIVE-blocked (message not yet delivered)
  server preempted   the server thread is READY while threads at or above its priority hold its CPUs
  server running     the server thread runs inside the MsgReceivev call that returns this request,
                     runs this request, or replies to it
  nested request     the server thread waits in its own request to another server;
                     that request's critical path is nested inside, recursively, when it is trustworthy
  server blocked     the server thread is blocked in another state, in a request that is not part of
                     this one, or on a mutex whose owner is not known
  mutex owner running / mutex owner preempted / mutex owner blocked
                     the server thread is MUTEX-blocked on a mutex whose owner is known (see
                     model.MutexWait) and the owner runs, is READY behind threads at or above its
                     priority, or is blocked; an owner waiting in a trustworthy request of its own
                     is a nested request
  unknown            no events (lost data), nothing known yet, or the request was handed off (another
                     thread replied, or the receiver went back to MsgReceive with the request open)
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .analyses import Timeline, mask_cpus, mutex_owner_at, us
from .events import state_name
from .model import ST_MUTEX, ST_READY, ST_RECEIVE, ST_REPLY, ST_RUNNING, ST_SEND, ST_UNKNOWN, UNKNOWN


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
    pieces: list = field(default_factory=list)    # READY waits: (start, end, (text, short text)) of who held the CPUs
    short: str = ""                               # compact name for the Perfetto slice

    @property
    def held_by(self):
        out = {}
        for e, f, h in self.pieces:
            out[h] = out.get(h, 0) + (f - e)
        return out


def _clip(m, segs, a, b):
    """Segments cut to [a, b]; READY waits keep only the pieces inside, so their attribution is recomputed."""
    out = []
    for s in segs:
        lo, hi = max(s.t0, a), min(s.t1, b)
        if hi > lo:
            pieces = [(max(e, lo), min(f, hi), h) for e, f, h in s.pieces if min(f, hi) > max(e, lo)]
            out.append(Segment(lo, hi, s.kind, s.label, s.thread, s.prio, s.txn, _clip(m, s.children, lo, hi),
                               pieces, s.short))
    _label_waits(m, out)
    return out


def _label_waits(m, segs):
    for sg in segs:
        if not sg.pieces:
            continue
        top = sorted(sg.held_by.items(), key=lambda kv: -kv[1])
        who = m.label(sg.thread)
        if sg.kind == "unexplained ready":
            sg.label = "%s READY at prio %s, not explained by the trace: %s" % (
                who, sg.prio, ", ".join("%s %.1f us" % (k[0], us(m, v)) for k, v in top[:3]))
            sg.short = "%s READY (%s), unexplained: %s" % (_tn(m, sg.thread), sg.prio, top[0][0][1])
            continue
        if len(top) == 1:
            sg.label = "%s READY at prio %s, CPU held by %s" % (who, sg.prio, top[0][0][0])
        else:
            more = sum(v for _, v in top[3:])
            sg.label = "%s READY at prio %s, CPUs held by %s%s" % (
                who, sg.prio, ", ".join("%s %.1f us" % (k[0], us(m, v)) for k, v in top[:3]),
                ", %d more %.1f us" % (len(top) - 3, us(m, more)) if more else "")
        sg.short = "%s READY (%s), CPU: %s%s" % (_tn(m, sg.thread), sg.prio, top[0][0][1],
                                                " +%d more" % (len(top) - 1) if len(top) > 1 else "")


def _tn(m, key):
    t = m.threads.get(key)
    return t.name if t and t.name else m.label(key)


def _held(tl, key, a, b, wprio):
    """Who held the CPUs `key` may run on while it was READY in [a, b]: [(e, f, (text, short), explained)].
    A wait is explained only when every CPU it may use (its traced run mask, or every CPU when none was
    traced) ran a known thread at or above its priority; an idle, lower-priority or unknown occupant, an
    unknown priority, or a mask naming no CPU of the trace leaves it unexplained."""
    m = tl.m
    cuts = {a, b}
    for cpu in tl.cpus:
        cuts.update(tl.switch_times(cpu, a, b))
    cuts.update(tl.runmask_changes(key, a, b))
    cuts = sorted(cuts)
    for e, f in zip(cuts, cuts[1:]):
        mask, src = tl.runmask_source(key, e)
        note = " (run mask 0x%x supplied by %s)" % (mask, src) if src not in (None, "traced") else ""
        cpus = tl.cpus if mask is None else mask_cpus(mask, tl.cpus)
        if not cpus:
            yield e, f, ("run mask 0x%x names no CPU of the trace" % mask, "mask 0x%x" % mask), False
            continue
        occ = [(x, tl.occupant(x, e)) for x in cpus]
        missing = [x for x, o in occ if o in (None, UNKNOWN)]
        if missing:
            yield e, f, ("occupant of CPU %s unknown" % ",".join(map(str, missing)), "CPU unknown"), False
            continue
        known = sorted(((x, o, tl.prio_at(o, e)) for x, o in occ), key=lambda r: -(r[2] if r[2] is not None else -1))
        if len(known) == 1 and known[0][1] == key:
            yield e, f, ("the kernel on CPU %d (switching)" % known[0][0], "kernel"), True
            continue
        low = [(x, o, p) for x, o, p in known if wprio is None or p is None or p < wprio]
        if low:
            x, o, p = low[0]
            what = "CPU %d idle" % x if m.is_idle(o) else "CPU %d ran %s (prio %s)" % (x, m.label(o), p)
            yield e, f, (what + ("" if mask is not None else " (no run mask traced)"),
                         "CPU %d idle" % x if m.is_idle(o) else "%s (%s)" % (_tn(m, o), p)), False
            continue
        x, o, p = known[0]
        if len(known) == 1:
            yield e, f, ("%s (prio %s) on CPU %d%s" % (m.label(o), p, x, note), "%s (%s)" % (_tn(m, o), p)), True
        else:
            yield e, f, (" + ".join("%s (prio %s) on CPU %d" % (m.label(o2), p2, x2) for x2, o2, p2 in known) + note,
                         " + ".join("%s (%s)" % (_tn(m, o2), p2) for _, o2, p2 in known)), True


def critical_path(model, tx, tl=None, max_depth=4, _depth=0):
    """Segments covering [tx.t_call, tx.t_exit] of one transaction."""
    tl = tl or Timeline(model)
    m = model
    segs = []
    client, srv = tx.client, tx.server
    cl = m.label(client)

    def add(a, b, kind, label, thread=None, prio=None, children=None, short=None):
        if b <= a:
            return
        last = segs[-1] if segs else None
        if last and not children and not last.children and last.kind == kind and last.label == label and last.t1 == a:
            last.t1 = b
            return
        segs.append(Segment(a, b, kind, label, thread, prio, tx.idx, children or [], short=short or label))

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
        last.pieces.append((a, b, holder))

    rx0 = tx.t_rx_call if tx.t_rx_call is not None else tx.t_rmsg    # the receive call, as far as it is known

    def mutex_part(c, d, waiter, wprio):
        """A MUTEX wait of `waiter` in [c, d], partitioned by what the mutex's owner did, when it is known."""
        wl, ws = m.label(waiter), _tn(m, waiter)
        w = tl.mutex_wait_at(waiter, c)
        owner, why = mutex_owner_at(tl, w, c) if w is not None else (None, "no SyncMutexLock traced")
        if owner is None:
            add(c, d, "server blocked", "%s MUTEX-blocked on %s, owner unknown (%s)" % (
                wl, "mutex 0x%x" % w.sync if w else "a mutex", why), waiter, wprio, short="%s MUTEX, owner unknown" % ws)
            return
        known_end = min(d, w.t_release) if w.t_release is not None else d
        pre = "%s MUTEX-blocked on mutex 0x%x owned by %s" % (wl, w.sync, m.label(owner))
        on = _tn(m, owner)
        for e, f, ocode, oprio in tl.segments(owner, c, known_end):
            if ocode == ST_RUNNING:
                add(e, f, "mutex owner running", pre + ", which runs", owner, oprio, short="%s: owner %s runs" % (ws, on))
            elif ocode == ST_READY:
                for g, h, text, explained in _held(tl, owner, e, f, oprio):
                    add_wait(g, h, "mutex owner preempted" if explained else "unexplained ready", owner, oprio, text)
            elif ocode in (ST_SEND, ST_REPLY):
                k = tl.client_txn_at(owner, e)
                if k is not None and k.causal and _depth < max_depth:
                    kids = _clip(m, critical_path(m, k, tl, max_depth, _depth + 1), e, f)
                    add(e, f, "nested request", "%s, which waits for %s (txn %d)" % (pre, m.server_name(k), k.idx),
                        owner, oprio, kids, short="%s: owner %s waits for %s" % (ws, on, m.server_name(k)))
                else:
                    add(e, f, "mutex owner blocked", "%s, which is %s-blocked%s" % (
                        pre, state_name(ocode), " on %s (txn %d, not followed)" % (m.server_name(k), k.idx) if k else ""),
                        owner, oprio, short="%s: owner %s %s-blocked" % (ws, on, state_name(ocode)))
            elif ocode in (None, ST_UNKNOWN):
                add(e, f, "unknown", pre + ", no events for the owner", owner, short="owner %s: no events" % on)
            else:
                add(e, f, "mutex owner blocked", "%s, which is blocked in %s" % (pre, state_name(ocode)), owner, oprio,
                    short="%s: owner %s %s" % (ws, on, state_name(ocode)))
        if d > known_end:
            add(known_end, d, "server blocked", "%s MUTEX-blocked on mutex 0x%x after its owner's SyncMutexUnlock began "
                "(handover; the next owner is not traced)" % (wl, w.sync), waiter, wprio, short="%s MUTEX handover" % ws)

    def server_part(a, b):
        if srv is None:
            add(a, b, "unknown", "receiving server thread not identified", short="server unknown")
            return
        sl = m.label(srv)
        ss = _tn(m, srv)
        if tx.replier is not None and tx.replier != srv and tx.t_rx_exit is not None and b > tx.t_rx_exit:
            # replied by another thread: which threads worked on it after the receive is not in the trace
            if a < tx.t_rx_exit:
                server_part(a, tx.t_rx_exit)
            add(max(a, tx.t_rx_exit), b, "unknown", "handed off: %s received it, %s replied (not followed)" % (
                sl, m.label(tx.replier)), srv, short="handed off to %s" % _tn(m, tx.replier))
            return
        for c, d, scode, sprio in tl.segments(srv, a, b):
            if scode == ST_RUNNING:
                cuts = {c, d}
                for x in (rx0, tx.t_rx_exit):
                    if x is not None and c < x < d:
                        cuts.add(x)
                lst, rx, reach, ends = tl._handled_index(srv)
                i0 = j = bisect.bisect_right(rx, c)
                while j < len(lst) and rx[j] < d:          # requests received inside (c, d)
                    cuts.add(rx[j])
                    if ends[j] < d:
                        cuts.add(ends[j])
                    j += 1
                i = i0 - 1
                while i >= 0 and reach[i] > c:             # requests received before c, replied inside
                    if c < ends[i] < d:
                        cuts.add(ends[i])
                    i -= 1
                cuts = sorted(cuts)
                for e, f in zip(cuts, cuts[1:]):
                    opened = tl.open_at(srv, e)
                    h = opened[0] if len(opened) == 1 else None
                    if rx0 is not None and rx0 <= e < tx.t_rx_exit:
                        add(e, f, "server running", "%s receiving this request (in MsgReceivev)" % sl, srv, sprio,
                            short="%s receiving" % ss)
                    elif len(opened) > 1:     # which of them the thread works on is not in the trace
                        add(e, f, "server busy", "%s running with %d requests open (%s)" % (
                            sl, len(opened), ", ".join("txn %d" % x.idx for x in sorted(opened, key=lambda x: x.idx))),
                            srv, sprio, short="%s: %d requests open" % (ss, len(opened)))
                    elif h is tx:
                        add(e, f, "server running", "%s running this request" % sl, srv, sprio,
                            short="%s runs this request" % ss)
                    elif h is not None:
                        add(e, f, "server busy", "%s running %s's request (txn %d)" % (sl, m.label(h.client), h.idx),
                            srv, sprio, short="%s busy: %s's request" % (ss, _tn(m, h.client)))
                    elif tx.t_reply_call is not None and e >= tx.t_reply_call:
                        add(e, f, "server running", "%s replying" % sl, srv, sprio, short="%s replying" % ss)
                    else:
                        add(e, f, "server busy", "%s running, not in a request" % sl, srv, sprio,
                            short="%s busy (no request)" % ss)
            elif scode == ST_READY:
                for e, f, text, explained in _held(tl, srv, c, d, sprio):
                    add_wait(e, f, "server preempted" if explained else "unexplained ready", srv, sprio, text)
            elif scode in (ST_SEND, ST_REPLY):
                k = tl.client_txn_at(srv, c)
                if k is not None and k.parent == tx.idx and k.trustworthy and _depth < max_depth:
                    kids = _clip(m, critical_path(m, k, tl, max_depth, _depth + 1), c, d)
                    add(c, d, "nested request", "%s waits for %s (txn %d)" % (sl, m.server_name(k), k.idx),
                        srv, sprio, kids, short="%s waits for %s" % (ss, m.server_name(k)))
                elif k is not None and k.parent == tx.idx:
                    add(c, d, "server blocked", "%s waits for %s (txn %d, not followed: %s)" % (
                        sl, m.server_name(k), k.idx, "; ".join(k.warnings) or "depth limit or incomplete"),
                        srv, sprio, short="%s waits for %s (not followed)" % (ss, m.server_name(k)))
                elif k is not None:
                    add(c, d, "server blocked", "%s %s-blocked on %s (txn %d%s)" % (
                        sl, state_name(scode), m.server_name(k), k.idx,
                        ", parent ambiguous, not followed" if tx.idx in k.parent_candidates
                        else ", not part of this request"), srv, sprio,
                        short="%s %s-blocked on %s" % (ss, state_name(scode), m.server_name(k)))
                else:
                    add(c, d, "server blocked", "%s %s-blocked" % (sl, state_name(scode)), srv, sprio,
                        short="%s %s-blocked" % (ss, state_name(scode)))
            elif scode == ST_RECEIVE and tx.t_rx_exit is not None and c >= tx.t_rx_exit:
                # received, not yet replied, waiting for other messages: the work may be anywhere
                add(c, d, "unknown", "%s RECEIVE-blocked, this request still open (handed off, not followed)"
                    % sl, srv, sprio, short="%s handed off" % ss)
            elif scode == ST_RECEIVE:
                add(c, d, "server waiting", "%s RECEIVE-blocked" % sl, srv, sprio, short="%s RECEIVE-blocked" % ss)
            elif scode in (None, ST_UNKNOWN):
                add(c, d, "unknown", "no events for %s" % sl, srv, short="unknown (no events)")
            elif scode == ST_MUTEX:
                mutex_part(c, d, srv, sprio)
            else:
                add(c, d, "server blocked", "%s blocked in %s" % (sl, state_name(scode)), srv, sprio,
                    short="%s %s" % (ss, state_name(scode)))

    for a, b, code, prio in tl.segments(client, tx.t_call, tx.t_exit):
        if code == ST_RUNNING:
            add(a, b, "client running", "%s running" % cl, client, prio, short="%s running" % _tn(m, client))
        elif code == ST_READY:
            for e, f, text, explained in _held(tl, client, a, b, prio):
                add_wait(e, f, "client ready" if explained else "unexplained ready", client, prio, text)
        elif code in (ST_SEND, ST_REPLY):
            server_part(a, b)
        elif code in (None, ST_UNKNOWN):
            add(a, b, "unknown", "no events for %s" % cl, client, short="unknown (no events)")
        else:
            add(a, b, "client blocked", "%s blocked in %s" % (cl, state_name(code)), client, prio,
                short="%s %s" % (_tn(m, client), state_name(code)))
    _label_waits(m, segs)
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
            d["cpu_held_by_us"] = {k[0]: round(us(model, v), 1)
                                   for k, v in sorted(s.held_by.items(), key=lambda kv: -kv[1])}
        if s.children:
            d["nested"] = [seg(c) for c in s.children]
        return d
    return {"txn": tx.idx, "client": model.label(tx.client), "server": model.server_name(tx),
            "message": tx.msg_label or None, "latency_us": round(us(model, tx.t_exit - tx.t_call), 1),
            "trustworthy": tx.trustworthy, "warnings": tx.warnings, "by_kind_us": totals(model, segs),
            "segments": [seg(s) for s in segs]}


def render(model, tx, segs):
    lat = us(model, tx.t_exit - tx.t_call)
    lines = ["txn %d: %s -> %s%s, MsgSend latency %.1f us%s" % (
        tx.idx, model.label(tx.client), model.server_name(tx), " (%s)" % tx.msg_label if tx.msg_type else "", lat,
        "" if tx.trustworthy else "  [not trustworthy: %s]" % "; ".join(tx.warnings or ["incomplete"]))]
    if tx.parent_candidates:
        lines.append("   parent ambiguous (txn %s): not part of any other request's critical path"
                     % ", ".join(map(str, tx.parent_candidates)))
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
