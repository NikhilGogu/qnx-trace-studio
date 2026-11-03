# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Analyses over a Model: IPC latency, deadlines, server priority while
handling each client, and suspected priority inversions.

All analyses use trustworthy exchanges only (complete, no warnings) and report
how many were excluded and how many requests did not finish in the trace.

Suspected priority inversion. For every instant a client at priority Pc is
SEND- or REPLY-blocked on a server thread, the inference needs:
  * the server thread READY (runnable, not running) at priority Ps < Pc, using
    the priorities known at that instant (from the latest thread event, never
    from a later one), under FIFO, round-robin or other scheduling (not
    sporadic) and with no adaptive partitioning events in the trace;
  * the server's run mask known at that instant (traced ThreadCtl, not
    invalidated by lost events or by receiving on a channel that may pass the
    client's mask on), naming at least one CPU of the trace;
  * every CPU in that mask busy with a thread above Ps, and at least one of
    those threads below Pc. Those threads are the evidence.
While the client is SEND-blocked, the eventual receiver is the dependency only
when it is provably the only thread of its process: the process was created
during the trace (or its threads were listed at trace start) and no other
thread of it was alive. The client's wait is split at every state change of
client and server, every context switch on the eligible CPUs and every
run-mask change. Anything else that could explain a low-priority READY server
(unknown mask, occupant, state or priority, an eligible CPU running something
at or below Ps, several possible receivers) counts as insufficient evidence. A
request is reported as a suspected inversion when the evidence covers at least
min_us; when unexplained time does instead, it is reported as "insufficient
evidence" with the reasons.
"""
from __future__ import annotations

import bisect
from collections import Counter, defaultdict

from .events import state_name
from .model import SCHED_SPORADIC, ST_DEAD, ST_DESTROY, ST_READY, ST_RUNNING, ST_SEND, ST_REPLY, ST_UNKNOWN, UNKNOWN


def us(model, cyc):
    return cyc * 1_000_000 / model.kev.cycles_per_sec


def pct(sorted_vals, p):
    """Nearest-rank percentile of a sorted list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, -(-len(sorted_vals) * p // 100) - 1))
    return sorted_vals[int(k)]


def mask_cpus(mask, cpus):
    return [x for x in cpus if (mask >> x) & 1]


class Timeline:
    """Point queries over a Model: thread state and priority at an instant, CPU occupants, run masks.

    runmasks [(process, thread or None, mask)] and unpinned {process} are what the user states about
    run masks set before the trace (the trace only records masks set while tracing). They apply to a thread
    only until the trace shows anything about its mask (a ThreadCtl, an invalidation) and only before the
    first lost events, and every verdict that uses them says so."""

    def __init__(self, model, runmasks=(), unpinned=()):
        self.m = model
        self.runmasks = list(runmasks)
        self.unpinned = set(unpinned)
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
        starts = [w0 for lst in model.loss_windows.values() for w0, _ in lst]
        self.first_loss = min(starts) if starts else None

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

    def policy_at(self, key, ts):
        tss, sts = self._st(key)
        i = bisect.bisect_right(tss, ts) - 1
        return sts[i][5] if i >= 0 and len(sts[i]) > 5 else None

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

    def runmask_source(self, key, ts):
        """(mask, source): the run mask traced for the thread at ts ("traced"), else one the user supplied
        ("--runmask", "--assume-unpinned"), else (None, None)."""
        t = self.m.threads.get(key)
        seen = False
        mask = None
        if t:
            for when, mk in t.runmask:
                if when <= ts:
                    seen, mask = True, mk
                else:
                    break
        if seen:      # the trace says something about this thread's mask: a supplied one no longer holds
            return (mask, "traced") if mask is not None else (None, None)
        if self.first_loss is not None and ts >= self.first_loss:
            return None, None
        p = self.m.processes.get(key[0])
        pname = p.name if p else None
        tname = t.name if t else None
        for proc, thr, mk in self.runmasks:
            if proc == pname and (thr is None or thr == tname):
                return mk, "--runmask"
        if pname in self.unpinned:
            return sum(1 << c for c in self.cpus), "--assume-unpinned"
        return None, None

    def runmask_at(self, key, ts):
        """The run mask of the thread at ts (traced, else supplied by the user), or None when not known."""
        return self.runmask_source(key, ts)[0]

    def runmask_changes(self, key, t0, t1):
        t = self.m.threads.get(key)
        out = [w for w, _ in (t.runmask if t else []) if t0 < w < t1]
        if (self.runmasks or self.unpinned) and self.first_loss is not None and t0 < self.first_loss < t1:
            out.append(self.first_loss)
        return out

    def _handled_index(self, server):
        """Requests the server thread received, sorted by receive time, each open until its reply call (or the
        end of the trace when it was never replied to), with the running maximum of those ends."""
        cache = self.__dict__.setdefault("_handled_cache", {})
        v = cache.get(server)
        if v is None:
            lst = sorted((t for t in self.m.txns if t.server == server and t.t_rx_exit is not None),
                         key=lambda t: t.t_rx_exit)
            ends = [t.t_reply_call if t.t_reply_call is not None else self.end for t in lst]
            reach, mx = [], -1
            for e in ends:
                mx = max(mx, e)
                reach.append(mx)
            v = cache[server] = (lst, [t.t_rx_exit for t in lst], reach, ends)
        return v

    def open_at(self, server, ts):
        """All requests the server thread had received and not yet replied to at ts."""
        lst, rx, reach, ends = self._handled_index(server)
        out = []
        i = bisect.bisect_right(rx, ts) - 1
        while i >= 0 and reach[i] > ts:
            if ts < ends[i]:
                out.append(lst[i])
            i -= 1
        return out

    def handling_at(self, server, ts):
        """The one request the server thread was handling at ts, or None (none, or several open)."""
        o = self.open_at(server, ts)
        return o[0] if len(o) == 1 else None

    def next_received_after(self, server, ts):
        """The first request the server thread received after ts, or None."""
        lst, rx, _, _ = self._handled_index(server)
        i = bisect.bisect_right(rx, ts)
        return lst[i] if i < len(lst) else None

    def client_txn_at(self, key, ts):
        """The exchange in which thread `key` was the client at ts (MsgSendv entered, not yet returned)."""
        cache = self.__dict__.setdefault("_client_cache", {})
        v = cache.get(key)
        if v is None:
            lst = sorted((t for t in self.m.txns if t.client == key and t.t_call is not None), key=lambda t: t.t_call)
            v = cache[key] = (lst, [t.t_call for t in lst])
        lst, calls = v
        i = bisect.bisect_right(calls, ts) - 1
        if i >= 0 and (lst[i].t_exit is None or ts < lst[i].t_exit):
            return lst[i]
        return None

    def sole_thread(self, key, t0, t1):
        """True when `key` is provably the only live thread of its process during [t0, t1]."""
        m = self.m
        p = m.processes.get(key[0])
        if p is None or (p.created_ts is None and key[0] not in m.injected_pids):
            return False      # threads that existed before the trace may never show up
        for other, t in m.threads.items():
            if other[0] != key[0] or other == key or not t.states:
                continue
            if t.states[0][0] > t1:
                continue
            gone = [s[0] for s in t.states if s[1] in (ST_DEAD, ST_DESTROY)]
            if gone and gone[0] <= t0:
                continue
            return False
        return True


def thread_key(model, key, prio):
    """Role of a client thread for comparisons: (process name, thread name, priority)."""
    t = model.threads.get(key)
    p = model.processes.get(key[0])
    return (p.name if p else "pid%d" % key[0], t.name if t and t.name else "tid%d" % key[1], prio)


def role_label(role):
    return "%s/%s%s" % (role[0], role[1], "@%s" % role[2] if role[2] is not None else "")


def ipc_latency(model):
    """Rows per (client role, server process): trustworthy requests, excluded and unfinished counts."""
    groups = defaultdict(list)
    excluded, unfinished, keys = defaultdict(int), defaultdict(int), defaultdict(set)
    for t in model.txns:
        g = (role_label(thread_key(model, t.client, t.client_prio)), model.server_name(t))
        keys[g].add(t.client)
        if t.t_call is None or t.t_exit is None:
            unfinished[g] += 1
        elif t.trustworthy:
            groups[g].append(us(model, t.t_exit - t.t_call))
        else:
            excluded[g] += 1
    out = []
    for (c, s) in sorted(set(groups) | set(excluded) | set(unfinished)):
        v = sorted(groups.get((c, s), []))
        row = {"client": c, "server": s, "n": len(v), "excluded": excluded.get((c, s), 0),
               "unfinished": unfinished.get((c, s), 0), "client_threads": len(keys[(c, s)])}
        if v:
            row.update({"min_us": round(v[0], 1), "p50_us": round(pct(v, 50), 1), "p90_us": round(pct(v, 90), 1),
                        "p99_us": round(pct(v, 99), 1), "max_us": round(v[-1], 1)})
        out.append(row)
    return out


def deadline_misses(model, thread_name, deadline_us):
    """{server process: {requests, missed, max_us, excluded, unfinished}} for client threads with this name."""
    out = {}
    for t in model.txns:
        th = model.threads.get(t.client)
        if not th or th.name != thread_name:
            continue
        r = out.setdefault(model.server_name(t), {"requests": 0, "missed": 0, "max_us": 0.0, "excluded": 0,
                                                 "unfinished": 0})
        if t.t_call is None or t.t_exit is None:
            r["unfinished"] += 1
            continue
        if not t.trustworthy:
            r["excluded"] += 1
            continue
        lat = us(model, t.t_exit - t.t_call)
        r["requests"] += 1
        r["missed"] += lat > deadline_us
        r["max_us"] = max(r["max_us"], round(lat, 1))
    return out


SEVERAL = "(several requests open)"


def server_priority_while_handling(model, tl=None):
    """{(server thread, client thread): {prio: us}}: the priority each server thread ran at while it held
    exactly one received request, credited to that request's client. Running time while it held several is
    credited once, to (server, SEVERAL); time while the only open request is untrustworthy is left out."""
    tl = tl or Timeline(model)
    out = defaultdict(lambda: defaultdict(float))
    for srv in {t.server for t in model.txns if t.server is not None and t.t_rx_exit is not None}:
        lst, rx, _, ends = tl._handled_index(srv)
        if not lst:
            continue
        cuts = sorted(set(rx) | set(ends))
        for a, b in zip(cuts, cuts[1:]):
            opened = tl.open_at(srv, a)
            if not opened:
                continue
            if len(opened) > 1:
                who = SEVERAL
            elif opened[0].trustworthy:
                who = model.label(opened[0].client)
            else:
                continue
            for c, d, code, prio in tl.segments(srv, a, b):
                if code == ST_RUNNING and prio is not None:
                    out[(model.label(srv), who)][prio] += us(model, d - c)
    return {k: dict(v) for k, v in out.items()}


def _channel(tx):
    return (tx.server_pid, tx.info.get("chid", tx.rx_chid))


def _inversion(tl, tx):
    """Evidence for one exchange: (inversion cycles, unexplained cycles, culprits, reasons, details)."""
    m = tl.m
    srv = tx.server
    inv = unk = 0
    culprits = defaultdict(int)       # (thread, prio, cpu) -> cycles
    reasons = Counter()
    phase = Counter()
    sprios, masks, busy, assumed = set(), set(), [], set()
    for a, b, ccode, _cp in tl.segments(tx.client, tx.t_call, tx.t_exit):
        if ccode not in (ST_SEND, ST_REPLY):
            continue
        send_phase = ccode == ST_SEND and (tx.t_rx_exit is None or a < tx.t_rx_exit)
        sole = not send_phase or tl.sole_thread(srv, a, b)
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
            if not sole:
                unk += d - c
                reasons["other threads of %s may receive on the channel" % m.server_name(tx)] += d - c
                continue
            if m.aps_seen:
                unk += d - c
                reasons["adaptive partitioning in use: budgets not evaluated"] += d - c
                continue
            if tl.policy_at(srv, c) == SCHED_SPORADIC:
                unk += d - c
                reasons["server uses sporadic scheduling"] += d - c
                continue
            cuts = {c, d}
            for cpu in tl.cpus:
                cuts.update(tl.switch_times(cpu, c, d))
            cuts.update(tl.runmask_changes(srv, c, d))
            cuts = sorted(cuts)
            for e, f in zip(cuts, cuts[1:]):
                mask, src = tl.runmask_source(srv, e)
                if mask is None:
                    unk += f - e
                    reasons["run mask of %s not traced" % m.label(srv)] += f - e
                    continue
                cpus = mask_cpus(mask, tl.cpus)
                if not cpus:
                    unk += f - e
                    reasons["run mask 0x%x names no CPU of the trace" % mask] += f - e
                    continue
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
                    reasons["CPU %d ran %s at prio %d, not above the server's %d" % (x, m.label(o), p, sprio)] += f - e
                    unk += f - e
                    continue
                blockers = [(x, o, p) for x, o, p in prios if p < pc]
                if not blockers:
                    continue
                inv += f - e
                phase[state_name(ccode)] += f - e
                sprios.add(sprio)
                masks.add(mask)
                if src != "traced":
                    assumed.add("run mask 0x%x supplied by %s" % (mask, src))
                for x, o, p in blockers:
                    culprits[(o, p, x)] += f - e
                if ccode == ST_SEND:
                    h = tl.handling_at(srv, e)
                    if h is not None and h is not tx and h not in busy:
                        busy.append(h)
    return inv, unk, culprits, reasons, {"phase": phase, "sprios": sprios, "masks": masks, "busy": busy,
                                         "assumed": sorted(assumed)}


def suspected_inversions(model, tl=None, min_us=100.0):
    """{"suspected": [...], "insufficient_evidence": [...], "checked": n, "excluded_untrustworthy": n}"""
    tl = tl or Timeline(model)
    m = model
    found, unclear = [], []
    checked = excluded = ambiguous = 0
    for tx in m.txns:
        if tx.server is None or tx.t_call is None or tx.t_exit is None:
            continue
        if not tx.trustworthy:
            excluded += 1
            continue
        if not tx.causal:
            ambiguous += 1
            continue
        checked += 1
        inv, unk, culprits, reasons, d = _inversion(tl, tx)
        inv_us, unk_us = us(m, inv), us(m, unk)
        if inv_us < min_us and unk_us < min_us:
            continue
        pc = tl.prio_at(tx.client, tx.t_call)
        row = {"txn": tx.idx, "client": m.label(tx.client), "client_prio": pc, "server": m.label(tx.server),
               "latency_us": round(us(m, tx.t_exit - tx.t_call), 1)}
        if inv_us >= min_us:
            ms = sorted(d["masks"])
            cpus = sorted({x for mk in ms for x in mask_cpus(mk, tl.cpus)})
            row.update({
                "verdict": "suspected priority inversion",
                "inversion_us": round(inv_us, 1),
                "while": {k: round(us(m, v), 1) for k, v in d["phase"].items()},
                "server_prio_while_ready": min(d["sprios"]),
                "server_run_mask": ", ".join("0x%x" % x for x in ms),
                "eligible_cpus": cpus,
                "preempted_by": [{"thread": m.label(k), "prio": p, "cpu": cpu, "us": round(us(m, v), 1)}
                                 for (k, p, cpu), v in sorted(culprits.items(), key=lambda x: -x[1])],
                "server_busy_with": [{"txn": h.idx, "client": m.label(h.client)} for h in d["busy"]],
                "insufficient_evidence_us": round(unk_us, 1),
                "assumptions": d["assumed"],
            })
            top = row["preempted_by"][0]
            row["evidence"] = (
                "%s (prio %s) waited %.1f us in MsgSend; for %.1f us of it (%s) %s was READY at prio %d while "
                "every CPU it may run on (CPU %s, run mask %s) ran a thread above prio %d, at least one of them "
                "below the client's %s; most of the time %s (prio %d) on CPU %d, %.1f us%s." % (
                    row["client"], pc, row["latency_us"], row["inversion_us"],
                    ", ".join("%s-blocked %.1f us" % kv for kv in row["while"].items()), row["server"],
                    row["server_prio_while_ready"], ",".join(map(str, cpus)), row["server_run_mask"],
                    row["server_prio_while_ready"], pc, top["thread"], top["prio"], top["cpu"], top["us"],
                    "; the server was busy with %s" % ", ".join("%s's request (txn %d)" % (b["client"], b["txn"])
                                                               for b in row["server_busy_with"])
                    if row["server_busy_with"] else "")) + (
                " Relies on %s." % "; ".join(d["assumed"]) if d["assumed"] else "")
            found.append(row)
        else:
            row.update({"verdict": "insufficient evidence", "unexplained_us": round(unk_us, 1),
                        "reasons": {k: round(us(m, v), 1) for k, v in reasons.most_common()}})
            unclear.append(row)
    return {"suspected": found, "insufficient_evidence": unclear, "checked": checked,
            "excluded_untrustworthy": excluded, "excluded_ambiguous_parent": ambiguous,
            "suspected_using_assumptions": sum(1 for r in found if r["assumptions"]), "min_us": min_us}
