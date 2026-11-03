# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Analyses over a Model: IPC latency, deadlines, server priority while
handling each client, and suspected priority inversions.

All analyses use trustworthy exchanges only (complete, no warnings) and report
how many were excluded.

Suspected priority inversion. For every instant a client at priority Pc is
SEND- or REPLY-blocked on a server thread, the inference needs:
  * the server thread READY (runnable, not running) at priority Ps < Pc, using
    the priorities known at that instant (from the latest thread event, never
    from a later one);
  * every CPU the server may run on (its run mask from ThreadCtl, or all CPUs
    when no run mask was traced) busy with a thread above Ps, and at least
    one of those below Pc. Those threads are the evidence.
The client's wait is split at every state change of client and server, every
context switch on the eligible CPUs and every run-mask change. While SEND-
blocked, the dependency is the eventual receiver only when it is the only
thread seen receiving on that channel. Intervals where a CPU's occupant, a
state or a priority is unknown, or where an eligible CPU ran something at or
below the server's priority (the scheduler state is not explained by the
trace), count as insufficient evidence. A request is reported as a suspected
inversion when the evidence covers at least min_us; when an unexplained
low-priority READY server covers min_us instead, it is reported as
"insufficient evidence" with the reasons.
"""
from __future__ import annotations

import bisect
from collections import Counter, defaultdict

from .events import state_name
from .model import ST_READY, ST_RECEIVE, ST_REPLY, ST_RUNNING, ST_SEND, ST_UNKNOWN, UNKNOWN


def us(model, cyc):
    return cyc * 1_000_000 / model.kev.cycles_per_sec


def pct(sorted_vals, p):
    """Nearest-rank percentile of a sorted list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, -(-len(sorted_vals) * p // 100) - 1))
    return sorted_vals[int(k)]


class Timeline:
    """Point queries over a Model: thread state and priority at an instant, CPU occupants, run masks."""

    def __init__(self, model):
        self.m = model
        self.end = model.end_ts
        self._states = {}
        self.run = defaultdict(list)   # cpu -> [(t0, t1, key)]
        last = {}
        for ts, cpu, _prev, _pc, _pp, nxt, _np in model.switches:
            if cpu in last:
                t0, key = last[cpu]
                if ts > t0:
                    self.run[cpu].append((t0, ts, key))
            last[cpu] = (ts, nxt)
        for cpu, (t0, key) in last.items():
            if self.end > t0:
                self.run[cpu].append((t0, self.end, key))
        self.run_starts = {cpu: [r[0] for r in lst] for cpu, lst in self.run.items()}
        self.cpus = sorted(set(range(model.kev.ncpu)) | set(self.run))

    def _st(self, key):
        v = self._states.get(key)
        if v is None:
            t = self.m.threads.get(key)
            sts = t.states if t else []
            v = self._states[key] = ([s[0] for s in sts], sts)
        return v

    def state_at(self, key, ts):
        """(state code, prio) from the latest event of the thread at or before ts; (None, None) before any."""
        tss, sts = self._st(key)
        i = bisect.bisect_right(tss, ts) - 1
        if i < 0:
            return None, None
        s = sts[i]
        return s[1], (None if s[1] == ST_UNKNOWN else s[2])

    def prio_at(self, key, ts):
        if key is None or key == UNKNOWN:
            return None
        if self.m.is_idle(key):
            return 0
        return self.state_at(key, ts)[1]

    def segments(self, key, t0, t1):
        """State intervals of a thread clipped to [t0, t1]: [(a, b, code, prio)]; code None before its first event."""
        tss, sts = self._st(key)
        out = []
        i = bisect.bisect_right(tss, t0) - 1
        cur = t0
        while cur < t1:
            nxt = tss[i + 1] if i + 1 < len(tss) else None
            end = t1 if nxt is None or nxt > t1 else nxt
            if end > cur:
                if i < 0:
                    out.append((cur, end, None, None))
                else:
                    s = sts[i]
                    out.append((cur, end, s[1], None if s[1] == ST_UNKNOWN else s[2]))
            cur = max(cur, end)
            i += 1
            if nxt is None:
                break
        return out

    def occupant(self, cpu, ts):
        """Thread running on cpu at ts, UNKNOWN, or None before the CPU's first switch."""
        starts = self.run_starts.get(cpu)
        if not starts:
            return None
        i = bisect.bisect_right(starts, ts) - 1
        if i < 0:
            return None
        t0, t1, key = self.run[cpu][i]
        return key if ts < t1 else None

    def switch_times(self, cpu, t0, t1):
        starts = self.run_starts.get(cpu, [])
        i = bisect.bisect_right(starts, t0)
        j = bisect.bisect_left(starts, t1)
        return starts[i:j]

    def runmask_at(self, key, ts):
        t = self.m.threads.get(key)
        mask = None
        if t:
            for when, mk in t.runmask:
                if when <= ts:
                    mask = mk
                else:
                    break
        return mask

    def runmask_changes(self, key, t0, t1):
        t = self.m.threads.get(key)
        return [w for w, _ in (t.runmask if t else []) if t0 < w < t1]

    def handling_at(self, server, ts):
        """The trustworthy request the server thread was handling at ts (received, not yet replied)."""
        for tx in self._handled(server):
            if tx.t_rx_exit <= ts < tx.t_reply_call:
                return tx
        return None

    def _handled(self, server):
        cache = self.__dict__.setdefault("_handled_cache", {})
        v = cache.get(server)
        if v is None:
            v = cache[server] = [t for t in self.m.txns if t.server == server and t.t_rx_exit is not None
                                 and t.t_reply_call is not None]
        return v


def _channel(tx):
    return (tx.server_pid, tx.info.get("chid")) if tx.info else (tx.server_pid, None)


def receivers_by_channel(model):
    """{(server pid, chid or None): set of threads seen receiving there} from REC_MESSAGE."""
    out = defaultdict(set)
    for tx in model.txns:
        if tx.server is not None:
            out[_channel(tx)].add(tx.server)
            out[(tx.server_pid, None)].add(tx.server)
    return out


def ipc_latency(model):
    groups = defaultdict(list)
    excluded = defaultdict(int)
    for t in model.txns:
        if t.t_call is None or t.t_exit is None:
            continue
        g = (model.label(t.client), model.server_name(t))
        if t.trustworthy:
            groups[g].append(us(model, t.t_exit - t.t_call))
        else:
            excluded[g] += 1
    out = []
    for (c, s) in sorted(set(groups) | set(excluded)):
        v = sorted(groups.get((c, s), []))
        row = {"client": c, "server": s, "n": len(v), "excluded": excluded.get((c, s), 0)}
        if v:
            row.update({"min_us": round(v[0], 1), "p50_us": round(pct(v, 50), 1), "p90_us": round(pct(v, 90), 1),
                        "p99_us": round(pct(v, 99), 1), "max_us": round(v[-1], 1)})
        out.append(row)
    return out


def deadline_misses(model, thread_name, deadline_us):
    """{server process: {requests, missed, max_us, excluded}} for client threads with this name."""
    out = {}
    for t in model.txns:
        th = model.threads.get(t.client)
        if not th or th.name != thread_name or t.t_call is None or t.t_exit is None:
            continue
        r = out.setdefault(model.server_name(t), {"requests": 0, "missed": 0, "max_us": 0.0, "excluded": 0})
        if not t.trustworthy:
            r["excluded"] += 1
            continue
        lat = us(model, t.t_exit - t.t_call)
        r["requests"] += 1
        r["missed"] += lat > deadline_us
        r["max_us"] = max(r["max_us"], round(lat, 1))
    return out


def server_priority_while_handling(model, tl=None):
    """{(server thread, client thread): {prio: us}}: the priority the server thread ran at while handling
    each client's request (from receive to reply), time-weighted."""
    tl = tl or Timeline(model)
    out = defaultdict(lambda: defaultdict(float))
    for tx in model.txns:
        if not tx.trustworthy:
            continue
        for a, b, code, prio in tl.segments(tx.server, tx.t_rx_exit, tx.t_reply_call):
            if code == ST_RUNNING and prio is not None:
                out[(model.label(tx.server), model.label(tx.client))][prio] += us(model, b - a)
    return {k: dict(v) for k, v in out.items()}


def _inversion(tl, tx, receivers):
    """Evidence for one exchange: (inversion cycles, unexplained cycles, culprits, reasons, details)."""
    m = tl.m
    srv = tx.server
    inv = unk = 0
    culprits = defaultdict(int)       # (thread, prio, cpu) -> cycles
    reasons = Counter()
    phase = Counter()
    sprios, masks, busy = set(), set(), []
    for a, b, ccode, _cp in tl.segments(tx.client, tx.t_call, tx.t_exit):
        if ccode not in (ST_SEND, ST_REPLY):
            continue
        if ccode == ST_SEND and tx.t_rx_exit is not None:
            rset = receivers.get(_channel(tx)) or {srv}
            if len(rset) > 1:
                # several threads receive on this channel: the eventual receiver is not the only dependency
                lo_ready = sum(d - c for c, d, sc, sp in tl.segments(srv, a, b) if sc == ST_READY)
                if lo_ready:
                    unk += lo_ready
                    reasons["%d threads receive on this channel" % len(rset)] += lo_ready
                continue
        for c, d, scode, sprio in tl.segments(srv, a, b):
            if scode in (None, ST_UNKNOWN):
                unk += d - c
                reasons["server state unknown"] += d - c
                continue
            if scode != ST_READY:
                continue
            pc = tl.prio_at(tx.client, c)
            if pc is None or sprio is None:
                unk += d - c
                reasons["priority unknown (fast-mode trace?)"] += d - c
                continue
            if sprio >= pc:
                continue
            cuts = {c, d}
            for cpu in tl.cpus:
                cuts.update(tl.switch_times(cpu, c, d))
            cuts.update(tl.runmask_changes(srv, c, d))
            cuts = sorted(cuts)
            for e, f in zip(cuts, cuts[1:]):
                mask = tl.runmask_at(srv, e)
                cpus = [x for x in tl.cpus if mask is None or (mask >> x) & 1]
                occ = [(x, tl.occupant(x, e)) for x in cpus]
                missing = [x for x, o in occ if o is None or o == UNKNOWN]
                if missing:
                    unk += f - e
                    reasons["occupant of CPU %s unknown" % ",".join(map(str, missing))] += f - e
                    continue
                prios = [(x, o, tl.prio_at(o, e)) for x, o in occ]
                if any(p is None for _, _, p in prios):
                    unk += f - e
                    reasons["priority of a running thread unknown"] += f - e
                    continue
                low = [(x, o, p) for x, o, p in prios if p <= sprio]
                if low:
                    x, o, p = low[0]
                    reasons["CPU %d ran %s at prio %d, not above the server's %d%s" % (
                        x, m.label(o), p, sprio, "" if mask is not None else " (no run mask traced)")] += f - e
                    unk += f - e
                    continue
                blockers = [(x, o, p) for x, o, p in prios if p < pc]
                if not blockers:
                    continue
                inv += f - e
                phase[state_name(ccode)] += f - e
                sprios.add(sprio)
                masks.add(mask)
                for x, o, p in blockers:
                    culprits[(o, p, x)] += f - e
                if ccode == ST_SEND:
                    h = tl.handling_at(srv, e)
                    if h is not None and h is not tx and h not in busy:
                        busy.append(h)
    return inv, unk, culprits, reasons, {"phase": phase, "sprios": sprios, "masks": masks, "busy": busy}


def suspected_inversions(model, tl=None, min_us=100.0):
    """{"suspected": [...], "insufficient_evidence": [...], "checked": n, "excluded_untrustworthy": n}"""
    tl = tl or Timeline(model)
    m = model
    receivers = receivers_by_channel(m)
    found, unclear = [], []
    checked = excluded = 0
    for tx in m.txns:
        if tx.server is None or tx.t_call is None or tx.t_exit is None:
            continue
        if not tx.trustworthy:
            excluded += 1
            continue
        checked += 1
        inv, unk, culprits, reasons, d = _inversion(tl, tx, receivers)
        inv_us, unk_us = us(m, inv), us(m, unk)
        if inv_us < min_us and unk_us < min_us:
            continue
        pc = tl.prio_at(tx.client, tx.t_call)
        row = {"txn": tx.idx, "client": m.label(tx.client), "client_prio": pc, "server": m.label(tx.server),
               "latency_us": round(us(m, tx.t_exit - tx.t_call), 1)}
        if inv_us >= min_us:
            masks = sorted("0x%x" % x for x in d["masks"] if x is not None)
            row.update({
                "verdict": "suspected priority inversion",
                "inversion_us": round(inv_us, 1),
                "while": {k: round(us(m, v), 1) for k, v in d["phase"].items()},
                "server_prio_while_ready": min(d["sprios"]),
                "server_run_mask": masks[0] if len(masks) == 1 and None not in d["masks"] else (
                    "not traced (all CPUs assumed)" if None in d["masks"] else masks),
                "preempted_by": [{"thread": m.label(k), "prio": p, "cpu": cpu, "us": round(us(m, v), 1)}
                                 for (k, p, cpu), v in sorted(culprits.items(), key=lambda x: -x[1])],
                "server_busy_with": [{"txn": h.idx, "client": m.label(h.client)} for h in d["busy"]],
                "insufficient_evidence_us": round(unk_us, 1),
            })
            top = row["preempted_by"][0]
            row["evidence"] = (
                "%s (prio %s) waited %.1f us in MsgSend; for %.1f us of it (%s) %s was READY at prio %d while "
                "every CPU it may run on (%s) ran threads above %d and below %s, most of all %s (prio %d) on CPU %d "
                "for %.1f us%s." % (
                    row["client"], pc, row["latency_us"], row["inversion_us"],
                    ", ".join("%s-blocked %.1f us" % kv for kv in row["while"].items()), row["server"],
                    row["server_prio_while_ready"], "run mask %s" % row["server_run_mask"]
                    if isinstance(row["server_run_mask"], str) else row["server_run_mask"],
                    row["server_prio_while_ready"], pc, top["thread"], top["prio"], top["cpu"], top["us"],
                    "; the server was busy with %s" % ", ".join("%s's request (txn %d)" % (b["client"], b["txn"])
                                                               for b in row["server_busy_with"])
                    if row["server_busy_with"] else ""))
            found.append(row)
        else:
            row.update({"verdict": "insufficient evidence", "unexplained_us": round(unk_us, 1),
                        "reasons": {k: round(us(m, v), 1) for k, v in reasons.most_common()}})
            unclear.append(row)
    return {"suspected": found, "insufficient_evidence": unclear, "checked": checked,
            "excluded_untrustworthy": excluded, "min_us": min_us}
