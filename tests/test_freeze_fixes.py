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
from tracestudio.check import _latency_assertion, _thread_selector  # noqa: E402,F401
from tracestudio.critical import critical_path  # noqa: E402,F401


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


def test_unnamed_procnto_thread_with_a_nonzero_ready_priority_is_not_idle():
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    worker = (1, 7)                                            # a procnto thread the trace never names
    g.state(0, T + 10, RUNNING, S, 10)
    g.state(0, T + 20, READY, worker, 20)                      # READY at prio 20; no RUNNING event for it
    g.state(0, T + 30, READY, (1, 8), 0)
    g.state(0, T + 40, RUNNING, (1, 8), 0)                     # an unnamed thread seen running at prio 0 is idle
    _, m = model(g)
    assert not m.is_idle(worker)
    assert Timeline(m).prio_at(worker, T + 100) == 20
    assert m.is_idle((1, 8))


def test_inversion_does_not_name_the_client_of_a_contradicted_request_as_the_cause():
    # hi is SEND-blocked while s1 is busy with b's request, but that request carries a contradiction warning
    # (its _msg_info names another sender): the evidence may not claim "the server was busy with b's request"
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RUNNING, S, 10)
    g.channel(0, T + 7, 0x1)
    g.runmask(0, T + 20, 0x1)
    g.wait_receive(0, T + 40, S, 10)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, B, 10)
    g.send(0, T + ms + 10, B, S[0], 0x10001, prio=10)
    g.receive(0, T + ms + 50, S, B, 0x10001, prio=10, info_client=(999, 1))
    g.state(0, T + ms + 200, RUNNING, H, 30)
    g.send(0, T + ms + 210, H, S[0], 0x20001, prio=30, block=SEND)
    g.state(0, T + ms + 300, READY, S, 10)
    g.state(0, T + ms + 310, RUNNING, W1, 20)
    g.state(0, T + 3 * ms, NANOSLEEP, W1, 20)
    g.state(0, T + 3 * ms + 10, RUNNING, S, 10)
    g.reply(0, T + 3 * ms + 100, S, B, 0x10001, 10)
    g.wait_receive(0, T + 3 * ms + 150, S, 10)
    g.receive(0, T + 3 * ms + 200, S, H, 0x20001, prio=10)
    g.reply(0, T + 3 * ms + 300, S, H, 0x20001, 30)
    g.send_exit(0, T + 3 * ms + 400, H, 30)
    _, m = model(g)
    busy, hi = m.txns
    assert not busy.trustworthy and hi.trustworthy
    r = suspected_inversions(m)
    (x,) = r["suspected"]
    assert x["client"] == "high/h" and set(x["while"]) == {"SEND"}
    assert x["server_busy_with"] == [] and "busy with" not in x["evidence"], x["evidence"]


def test_duplicate_receive_record_on_the_same_thread_is_not_indexed_twice():
    # REC_MESSAGE twice for one exchange on the same server thread: the exchange is contradictory, and once it is
    # closed it must not stay behind as the parent of the server's next, independent request
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    g.state(0, T + 6, RECEIVE, M, 10)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001, prio=10)
    g.receive(0, T + 100, S, C, 0x10001, prio=10)
    g.comm(0, T + 110, 2, 0x10001, S[0])                       # a second REC_MESSAGE for the same rcvid
    g.reply(0, T + 200, S, C, 0x10001, 10)
    g.send(0, T + 300, S, M[0], 0x20002, prio=10)              # the server's own, independent request
    g.receive(0, T + 340, M, S, 0x20002, prio=10)
    g.reply(0, T + 400, M, S, 0x20002, 10)
    g.send_exit(0, T + 450, S, 10)
    g.send_exit(0, T + 500, C, 10)
    _, m = model(g)
    first, second = m.txns
    assert any("received twice" in w for w in first.warnings), first.warnings
    assert second.parent is None and not second.parent_candidates, (second.parent, second.parent_candidates)


def test_critical_path_of_a_request_whose_receive_exit_is_not_traced_does_not_crash():
    # MsgReceivev entry and exit are missing (REC_MESSAGE is there), the server runs, and the client's MsgSend is
    # complete; `critical --request N` selects it by number and must give a path, not a TypeError
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001, prio=10)
    g.state(0, T + 100, RUNNING, S, 10)
    g.comm(0, T + 110, 2, 0x10001, S[0])                       # REC_MESSAGE, but no MsgReceivev exit follows
    g.reply(0, T + 400, S, C, 0x10001, 10)
    g.send_exit(0, T + 500, C, 10)
    _, m = model(g)
    (tx,) = m.txns
    assert tx.t_rmsg is not None and tx.t_rx_exit is None and tx.t_exit is not None
    segs = critical_path(m, tx)
    assert segs and segs[0].t0 == tx.t_call and segs[-1].t1 == tx.t_exit


def test_deadline_that_matches_no_request_with_min_requests_zero_is_a_result_not_a_crash():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    exchange(g, 0, T + 20, C, S, 0x10001)
    _, m = model(g)
    r = _latency_assertion(m, "deadline nonexistent=1 us", 1, _thread_selector(m, "nonexistent"), False, 0)
    assert r.ok and "0 of 0" in r.summary, r
    r = _latency_assertion(m, "deadline nonexistent=1 us", 1, _thread_selector(m, "nonexistent"), False, 1)
    assert not r.ok and r.kind == "NoData"                     # the default still fails closed


def test_convert_refuses_to_overwrite_its_own_input():
    import contextlib
    import io
    import tempfile
    from tracestudio.cli import main
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    exchange(g, 0, T + 20, C, S, 0x10001)
    with tempfile.TemporaryDirectory(prefix="ts-conv-") as d:
        path = os.path.join(d, "t.kev")
        link = os.path.join(d, "link.pftrace")
        with open(path, "wb") as f:
            f.write(g.bytes())
        os.symlink(path, link)
        with open(path, "rb") as f:
            before = f.read()
        for out in (path, link):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc = main(["convert", path, "-o", out])
            with open(path, "rb") as f:
                assert f.read() == before, "the input trace was overwritten"
            assert rc != 0


def test_unmatched_unlock_exit_does_not_confirm_the_mutex_owner():
    # the unlock entry is followed by another call entry on the same thread, then an unlock exit with nothing
    # open for it: the exit is not the exit of that unlock, so it cannot confirm the owner
    from test_synthetic import K_MUTEX_LOCK, K_MUTEX_UNLOCK, ST_MUTEX_CODE
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RUNNING, S2, 10)
    g.state(0, T + 10, RUNNING, S, 10)
    g.kcall(0, T + 20, K_MUTEX_LOCK, [0x1000, 0x80000000, 0x1002])
    g.state(0, T + 30, ST_MUTEX_CODE, S, 30)
    g.state(0, T + 40, RUNNING, S2, 10)
    g.kcall(0, T + 500, K_MUTEX_UNLOCK, [0x1000, 0x80000000, 0x80001002])
    g.kcall(0, T + 502, K_SEM_POST, [0x2000])                  # another call starts: the unlock entry is dropped
    g.kcall(0, T + 505, K_MUTEX_UNLOCK, [0, 0], EXIT)          # an unlock exit with no matching entry
    g.state(0, T + 510, READY, S, 30)
    g.state(0, T + 600, RUNNING, S, 30)
    g.kcall(0, T + 610, K_MUTEX_LOCK, [0, 0], EXIT)
    _, m = model(g)
    (w,) = m.mutex_waits
    assert w.owner is None and not w.owner_known, (w.owner, w.unknown)


def test_cross_cpu_slot_is_judged_by_the_anchor_of_the_buffer_that_holds_it():
    # a slot that names CPU 1 sits in CPU 0's buffer, and its low 32 bits are placed with CPU 0's anchor. CPU 1
    # has no TIME record at all, but the placement is not a guess: the slot must not be marked partial
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RUNNING, S, 10)
    h = (0 << 30) | (1 << 24) | (C_PRTH << 10) | RUNNING        # S_SIMPLE slot of CPU 1 inside CPU 0's buffer
    g.cur[3].append([h, (T + 50) & M32, S2[0], S2[1]])
    g.state(0, T + 100, RUNNING, S, 10)
    k = kev(g)
    (ev,) = [e for e in k.events() if e.cpu == 1]
    assert ev.ts == T + 50 and not ev.partial, (ev.ts, ev.partial)


def test_inversion_does_not_follow_a_nested_request_whose_parent_is_ambiguous():
    # s1 holds two requests (from hi and from c), then sends z to mid and waits for the reply. Nothing says which of
    # the two requests z serves, so mid being held up by a thread of prio 20 cannot be blamed on either client.
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    g.state(0, T + 10, RUNNING, M, 10)
    g.channel(0, T + 12, 0x1)
    g.runmask(0, T + 20, 0x1)
    g.wait_receive(0, T + 40, M, 10)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, H, 30)
    g.send(0, T + ms + 10, H, S[0], 0x10001, prio=30)
    g.state(0, T + ms + 50, RUNNING, C, 25)
    g.send(0, T + ms + 60, C, S[0], 0x20001, prio=25)
    g.receive(0, T + ms + 100, S, H, 0x10001, prio=10)
    g.wait_receive(0, T + ms + 130, S, 10)
    g.receive(0, T + ms + 150, S, C, 0x20001, prio=10)
    g.send(0, T + ms + 200, S, M[0], 0x30001, prio=10)         # whose request is this for?
    g.receive(0, T + ms + 240, M, S, 0x30001, prio=10)
    g.state(0, T + ms + 300, READY, M, 10)
    g.state(0, T + ms + 310, RUNNING, W1, 20)                  # prio 20 holds the only CPU: below both clients
    g.state(0, T + 3 * ms, NANOSLEEP, W1, 20)
    g.state(0, T + 3 * ms + 10, RUNNING, M, 10)
    g.reply(0, T + 3 * ms + 100, M, S, 0x30001, 10)
    g.send_exit(0, T + 3 * ms + 150, S, 10)
    g.reply(0, T + 3 * ms + 200, S, H, 0x10001, 30)
    g.reply(0, T + 3 * ms + 300, S, C, 0x20001, 25)
    g.send_exit(0, T + 3 * ms + 400, H, 30)
    g.send_exit(0, T + 3 * ms + 500, C, 25)
    _, m = model(g)
    z = m.txns[2]
    assert z.parent is None and z.parent_candidates == [0, 1] and not z.causal
    r = suspected_inversions(m)
    assert r["suspected"] == [], [x["evidence"] for x in r["suspected"]]
    assert {x["client"] for x in r["insufficient_evidence"]} == {"high/h", "client/c"}


def parked_scene(during):
    # s1 takes hi's request, waits in MsgReceivev 2 ms with it still open, then replies itself.
    # `during` runs on CPU 0 meanwhile: (thread, prio) or None for nothing traced.
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, H, 30)
    g.send(0, T + ms + 10, H, S[0], 0x10001, prio=30)
    g.receive(0, T + ms + 40, S, H, 0x10001, prio=10)
    g.wait_receive(0, T + ms + 100, S, 10)
    if during is not None:
        g.state(0, T + ms + 200, RUNNING, during[0], during[1])
    g.state(0, T + 3 * ms, RUNNING, S, 10)
    g.reply(0, T + 3 * ms + 400_000, S, H, 0x10001, 30)
    g.send_exit(0, T + 3 * ms + 400_100, H, 30)
    return model(g)[1]


def test_parked_request_is_insufficient_evidence_when_a_lower_thread_runs():
    for during in (None, (M, 20)):
        r = suspected_inversions(parked_scene(during))
        assert r["checked"] == 1 and r["suspected"] == [], during
        (row,) = r["insufficient_evidence"]
        assert any("RECEIVE-blocked, this request still open" in k for k in row["reasons"]), row


def test_parked_request_is_clean_while_the_cpu_is_idle_or_runs_higher_priority_work():
    for during in (((1, 1), 0), (M, 40)):
        r = suspected_inversions(parked_scene(during))
        assert r["checked"] == 1 and r["suspected"] == [] and r["insufficient_evidence"] == [], (during, r)


def test_request_open_while_the_receiver_waits_in_receive_is_insufficient_evidence():
    # s1 takes hi's request, goes back to RECEIVE for 2 ms with the request still open, then replies itself. The
    # critical path calls that interval a possible handoff; the inversion check must not pass it silently.
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, H, 30)
    g.send(0, T + ms + 10, H, S[0], 0x10001, prio=30)
    g.receive(0, T + ms + 40, S, H, 0x10001, prio=10)
    g.wait_receive(0, T + ms + 100, S, 10)                      # back in RECEIVE, request still open
    g.state(0, T + 3 * ms, RUNNING, S, 10)
    g.reply(0, T + 3 * ms + 400_000, S, H, 0x10001, 30)
    g.send_exit(0, T + 3 * ms + 400_100, H, 30)
    _, m = model(g)
    (tx,) = m.txns
    assert tx.trustworthy and tx.causal and tx.replier == tx.server
    r = suspected_inversions(m)
    assert r["checked"] == 1 and r["suspected"] == []
    (row,) = r["insufficient_evidence"]
    assert any("RECEIVE-blocked, this request still open" in k for k in row["reasons"]), row


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
