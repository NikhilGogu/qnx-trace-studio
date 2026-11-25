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
from test_synthetic import B, C, H, M, NAMES, S, S2, T, exchange, kev, model  # noqa: E402,F401
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
