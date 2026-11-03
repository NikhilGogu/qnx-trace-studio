# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Analyses over a Model: IPC latency, blocking chains, suspected inversions.

A suspected priority inversion is reported only with evidence: a client at
priority Pc waits on a server whose thread sits READY (runnable, not running)
at priority Ps < Pc while another thread at priority Ps < Pr < Pc runs. That is
the pattern priority inheritance prevents; plain blocking is not flagged.
"""
from __future__ import annotations

import bisect
from collections import defaultdict

from .events import state_name
from .model import ST_READY


def _us(model, cyc):
    return cyc * 1_000_000 / model.kev.cycles_per_sec


class Timeline:
    def __init__(self, model):
        self.m = model
        evs = model.kev.events()
        self.end = evs[-1].ts if evs else 0
        self.prio_ts = {k: [p[0] for p in t.prios] for k, t in model.threads.items()}
        self.run = defaultdict(list)   # cpu -> [(t0, t1, key)]
        last = {}
        for ts, cpu, _prev, _pc, _pp, nxt, _np in model.switches:
            if cpu in last:
                t0, key = last[cpu]
                self.run[cpu].append((t0, ts, key))
            last[cpu] = (ts, nxt)
        for cpu, (t0, key) in last.items():
            self.run[cpu].append((t0, self.end, key))
        self.run_starts = {cpu: [r[0] for r in lst] for cpu, lst in self.run.items()}

    def prio(self, key, ts):
        t = self.m.threads.get(key)
        if not t or not t.prios:
            return None
        i = bisect.bisect_right(self.prio_ts[key], ts) - 1
        return t.prios[max(i, 0)][1]

    def running_during(self, t0, t1):
        for cpu, lst in self.run.items():
            i = max(bisect.bisect_right(self.run_starts[cpu], t0) - 1, 0)
            while i < len(lst) and lst[i][0] < t1:
                a, b, key = lst[i]
                lo, hi = max(a, t0), min(b, t1)
                if hi > lo:
                    yield cpu, lo, hi, key
                i += 1


def ipc_latency(model):
    groups = defaultdict(list)
    for t in model.txns:
        if t.t_call is not None and t.t_exit is not None:
            sp = model.processes.get(t.server_pid)
            groups[(model.label(t.client), sp.name if sp else str(t.server_pid))].append(_us(model, t.t_exit - t.t_call))
    out = []
    for (c, s), v in sorted(groups.items()):
        v.sort()
        out.append({"client": c, "server": s, "n": len(v), "min_us": round(v[0], 1),
                    "median_us": round(v[len(v) // 2], 1), "max_us": round(v[-1], 1)})
    return out


def suspected_inversions(model, min_us=100.0, tl=None):
    tl = tl or Timeline(model)
    m = model
    out = []
    for tx in m.txns:
        if tx.t_call is None or tx.t_exit is None or tx.server is None:
            continue
        pc = tl.prio(tx.client, tx.t_call)
        if pc is None:
            continue
        # the server thread that will receive this message, and whatever it was doing meanwhile
        stt = m.threads.get(tx.server)
        if not stt:
            continue
        culprits = defaultdict(float)
        ready_us = 0.0
        for a, b, code, ps in m.intervals(tx.server, tl.end):
            lo, hi = max(a, tx.t_call), min(b, tx.t_rx_exit or tx.t_exit)
            if hi <= lo or code != ST_READY or ps is None or ps >= pc:
                continue
            ready_us += _us(m, hi - lo)
            for cpu, r0, r1, key in tl.running_during(lo, hi):
                if key in (tx.client, tx.server) or m.is_idle(key):
                    continue
                pr = tl.prio(key, r0)
                if pr is not None and ps < pr < pc:
                    culprits[(key, pr, cpu)] += _us(m, r1 - r0)
        total = sum(culprits.values())
        if total >= min_us:
            busy_with = None
            for other in m.txns:
                if other.server == tx.server and other is not tx and other.t_rx_exit is not None \
                        and other.t_rx_exit <= tx.t_call and (other.t_reply_call or 1 << 62) >= tx.t_call:
                    busy_with = other
            out.append({
                "txn": tx.idx, "client": m.label(tx.client), "client_prio": pc,
                "server": m.label(tx.server), "server_prio_while_ready": min(p for _, _, c2, p in m.intervals(tx.server, tl.end)
                                                                            if c2 == ST_READY and p is not None),
                "client_blocked": tx.client_block, "latency_us": round(_us(m, tx.t_exit - tx.t_call), 1),
                "server_ready_us": round(ready_us, 1), "preempted_by": [
                    {"thread": m.label(k), "prio": p, "cpu": cpu, "us": round(v, 1)}
                    for (k, p, cpu), v in sorted(culprits.items(), key=lambda x: -x[1])],
                "server_busy_with_txn": busy_with.idx if busy_with else None,
                "server_busy_with_client": m.label(busy_with.client) if busy_with else None,
                "verdict": "suspected priority inversion",
            })
    return out


def blocking_chain(model, tx, tl=None, depth=0):
    """Text chain for one transaction: who the client waited on, recursively."""
    tl = tl or Timeline(model)
    m = model
    lines = []
    pad = "  " * depth
    lat = _us(m, tx.t_exit - tx.t_call) if tx.t_exit and tx.t_call else float("nan")
    lines.append("%s%s (prio %s) %s-blocked on %s for %.1f us" % (
        pad, m.label(tx.client), tl.prio(tx.client, tx.t_call or tx.t_smsg), tx.client_block or "?",
        m.label(tx.server), lat))
    if tx.server is not None and tx.t_call is not None and tx.t_exit is not None:
        acc = defaultdict(float)
        for a, b, code, p in m.intervals(tx.server, tl.end):
            lo, hi = max(a, tx.t_call), min(b, tx.t_exit)
            if hi > lo:
                acc[(state_name(code), p)] += _us(m, hi - lo)
        lines.append("%s  server states: %s" % (pad, ", ".join("%s@%s %.1f us" % (s, p, v) for (s, p), v in
                                                             sorted(acc.items(), key=lambda x: -x[1]))))
    for c in tx.children:
        lines.extend(blocking_chain(m, m.txns[c], tl, depth + 1))
    return lines


def deadline_misses(model, thread_name, deadline_us):
    """{server process: (requests, missed, max_us)} for one client thread name."""
    out = {}
    for t in model.txns:
        th = model.threads.get(t.client)
        if th and th.name == thread_name and t.t_call is not None and t.t_exit is not None:
            lat = _us(model, t.t_exit - t.t_call)
            sp = model.processes.get(t.server_pid)
            n, miss, mx = out.get(sp.name if sp else str(t.server_pid), (0, 0, 0.0))
            out[sp.name if sp else str(t.server_pid)] = (n + 1, miss + (lat > deadline_us), max(mx, round(lat, 1)))
    return out
