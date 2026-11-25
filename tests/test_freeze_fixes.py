# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for defects found in the pre-freeze review, one test per fix. Helpers come from
test_synthetic.py. Run with pytest, or through tests/test_tracestudio.py, or directly:
    python3 tests/test_freeze_fixes.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from kevgen import (C_COMM, C_CONTROL, C_KERCALL, C_PRTH, DEAD, EXIT, INT, K_PULSE, K_RECEIVEV, K_REPLYV,  # noqa: E402,F401
                    K_SEM_POST, K_SENDV, M32, NANOSLEEP, READY, RECEIVE, REPLY, REPLYMSG, RPULSE, RUNNING, SEND, Gen)
from test_synthetic import B, C, H, M, NAMES, S, S2, T, W1, W2, exchange, kev, model  # noqa: E402,F401
from tracestudio.analyses import Timeline, suspected_inversions  # noqa: E402,F401


def test_receive_without_traced_send_still_drops_the_run_mask():
    # a request that was already pending when tracing started: its SND_MESSAGE is not in the trace, but the
    # receive on a channel that inherits run masks may still replace the server's mask
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, S, 10)
    g.channel(0, T + 12, 0x4000)                               # _NTO_CHF_INHERIT_RUNMASK
    g.runmask(0, T + 20, 0x1)
    g.wait_receive(0, T + 40, S, 10)
    g.receive(0, T + 1000, S, C, 0x10001, prio=10)             # the client's send was before the trace
    _, m = model(g)
    assert m.counters["receive_exit_without_txn"] == 1
    tl = Timeline(m)
    assert tl.runmask_at(S, T + 500) == 0x1 and tl.runmask_at(S, T + 2000) is None


def test_exchange_without_client_blocked_states_is_not_a_clean_check():
    # the exchange is complete (calls and COMM records) but the client has no SEND or REPLY state in the trace,
    # as when those thread-state events were filtered out. Nothing was analysed: that is no evidence, not a pass.
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, H, 30)
    g.kcall(0, T + ms + 10, K_SENDV, [0x40000001, 1, 1, 0x7101, 1, 0])
    g.comm(0, T + ms + 20, 0, 0x10001, S[0])                    # SND_MESSAGE; no client state event follows
    g.receive(0, T + ms + 40, S, H, 0x10001, prio=10)
    g.state(0, T + ms + 100_000, READY, S, 10)
    g.state(0, T + ms + 100_010, RUNNING, W1, 20)
    g.state(0, T + 3 * ms, NANOSLEEP, W1, 20)
    g.state(0, T + 3 * ms + 10, RUNNING, S, 10)
    g.reply(0, T + 3 * ms + 400_000, S, H, 0x10001, 30)
    g.send_exit(0, T + 3 * ms + 400_100, H, 30)
    _, m = model(g)
    (tx,) = m.txns
    assert tx.trustworthy and tx.causal and tx.t_exit is not None
    r = suspected_inversions(m)
    assert r["checked"] == 1
    assert [x["client"] for x in r["insufficient_evidence"]] == ["high/h"], r


def test_qvm_events_make_the_inversion_verdict_insufficient_evidence():
    # the README promises "insufficient evidence" when QVM guests appear; guest scheduling is not modelled
    def scene(qvm):
        g = Gen(ncpu=2)
        g.buffer(0, T)
        g.names(0, T, NAMES)
        g.state(0, T + 10, RUNNING, S, 10)
        g.channel(0, T + 12, 0x1)
        g.runmask(0, T + 20, 0x1)
        g.wait_receive(0, T + 40, S, 10)
        ms = 1_000_000
        g.state(0, T + ms, RUNNING, H, 30)
        g.send(0, T + ms + 10, H, S[0], 0x10001, prio=30)
        g.receive(0, T + ms + 40, S, H, 0x10001, prio=10)
        if qvm:
            g.event(1, T + ms + 50, 10, 1, [0, 0])             # class QVM
        g.state(0, T + ms + 100_000, READY, S, 10)
        g.state(0, T + ms + 100_010, RUNNING, M, 20)
        g.state(0, T + 3 * ms, NANOSLEEP, M, 20)
        g.state(0, T + 3 * ms + 10, RUNNING, S, 10)
        g.reply(0, T + 3 * ms + 400_000, S, H, 0x10001, 30)
        g.send_exit(0, T + 3 * ms + 400_100, H, 30)
        return model(g)[1]
    r = suspected_inversions(scene(False))
    assert len(r["suspected"]) == 1 and r["insufficient_evidence"] == []      # the same scene, no guest
    m = scene(True)
    r = suspected_inversions(m)
    assert r["suspected"] == [], r["suspected"]
    (row,) = r["insufficient_evidence"]
    assert any("QVM" in k for k in row["reasons"]), row


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
