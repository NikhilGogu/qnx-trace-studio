# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests on synthetic slot streams (tests/kevgen.py): parser edge cases,
transaction lifecycle, loss handling, inversion inference, and exact checks of
the Perfetto output in TraceProcessor. Run with pytest or directly:
    python3 tests/test_synthetic.py
Set TS_REQUIRE_ALL=1 to turn a missing perfetto package into a failure.
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from kevgen import (C_COMM, C_CONTROL, C_KERCALL, C_PRTH, DEAD, EXIT, INT, K_PULSE, K_RECEIVEV, K_REPLYV,  # noqa: E402
                    K_SEM_POST, K_SENDV, M32, NANOSLEEP, READY, RECEIVE, REPLY, REPLYMSG, RPULSE, RUNNING, S_BEGIN,
                    S_CONT, S_END, SEM, SEND, SPULSE, Gen)
from tracestudio.analyses import (SEVERAL, Timeline, ipc_latency, server_priority_while_handling,  # noqa: E402
                                  suspected_inversions)
from tracestudio.critical import Segment, _clip, critical_path  # noqa: E402
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import UNKNOWN, Model  # noqa: E402

T = 5 << 32
C, B, S, S2, M, H, IDLE1 = (100, 1), (101, 1), (200, 1), (200, 2), (300, 1), (400, 1), (1, 2)
NAMES = {1: ("procnto-smp-instr", {1: "idle_cpu_0", 2: "idle_cpu_1"}), 100: ("/bin/client", {1: "c"}),
         101: ("/bin/other", {1: "b"}), 200: ("/bin/server", {1: "s1", 2: "s2"}), 300: ("/bin/mid", {1: "m"}),
         400: ("/bin/high", {1: "h"}), 500: ("/bin/work", {1: "w", 2: "w"})}
W1, W2 = (500, 1), (500, 2)
TMP = tempfile.mkdtemp(prefix="ts-synth-")


def kev(g, **kw):
    return KevFile("synthetic.kev", g.bytes(**kw))


def model(g, **kw):
    k = kev(g, **kw)
    return k, Model(k)


def tp_of(m, name):
    """TraceProcessor over the converted model, or None (skip) when perfetto is missing."""
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        if os.environ.get("TS_REQUIRE_ALL"):
            raise
        print("skip: perfetto package not installed")
        return None
    path = os.path.join(TMP, name + ".perfetto-trace")
    write(m, path)
    return TraceProcessor(trace=path)


def rows(tp, q):
    return list(tp.query(q))


def no_import_errors(tp):
    errs = [(r.name, r.value) for r in rows(tp, "SELECT name, value FROM stats WHERE value > 0 AND severity IN "
                                                "('error', 'data_loss')")]
    assert not errs, errs
    assert rows(tp, "SELECT COUNT(*) n FROM slice WHERE dur < 0")[0].n == 0


def exchange(g, cpu, t, client, server, rcvid, cprio=10, sprio=10, work=1000, msg=(0x7101, 1, 0),
             coid=0x40000001):
    """Client running at t, server RECEIVE-blocked: one full exchange on one CPU. Returns the end time."""
    g.state(cpu, t, RUNNING, client, cprio)
    g.send(cpu, t + 10, client, server[0], rcvid, prio=cprio, msg=msg, coid=coid)
    g.receive(cpu, t + 40, server, client, rcvid, prio=sprio, msg=msg)
    g.reply(cpu, t + 60 + work, server, client, rcvid, client_prio=cprio)
    g.wait_receive(cpu, t + 100 + work, server, prio=sprio)
    g.send_exit(cpu, t + 120 + work, client, prio=cprio)
    return t + 140 + work


# ---- timestamps --------------------------------------------------------------
def test_time_anchor_is_authoritative_after_long_silence():
    g = Gen(ncpu=1)
    t1 = T + 0x100
    g.buffer(0, t1)
    g.state(0, t1 + 10, RUNNING, C)
    t2 = t1 + 3_500_000_000            # 3.5 s of silence, more than 2^31 cycles, same MSB
    g.buffer(0, t2)
    g.state(0, t2 + 10, READY, C)
    k = kev(g)
    evs = k.events()
    assert [e.ts for e in evs if e.cls == C_CONTROL and e.code == 1] == [t1, t2]
    assert [e.ts for e in evs if e.cls == C_PRTH] == [t1 + 10, t2 + 10]
    assert not k.losses and k.stats.ts_adjusted == 0


def test_interlaced_slots_around_a_rollover():
    g = Gen(ncpu=1)
    g.buffer(0, T + 0xFFFFF000)
    g.state(0, T + 0xFFFFF100, RUNNING, C, 10)
    g.time(0, (6 << 32) + 0x10)                        # rollover anchor
    g.state(0, T + 0xFFFFFF00, READY, C, 10)           # stamped before the rollover, written after its TIME
    g.state(0, (6 << 32) + 0x20, RUNNING, C, 10)
    k = kev(g)
    assert [e.ts for e in k.events() if e.cls == C_PRTH] == [T + 0xFFFFF100, T + 0xFFFFFF00, (6 << 32) + 0x20]
    assert k.stats.rollovers == 1 and k.stats.ts_adjusted == 1 and k.stats.per_cpu_regressions == 1


def test_interlaced_combine_takes_the_begin_timestamp():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    lsb = (T + 100) & M32
    g.raw(S_BEGIN, 0, C_PRTH, RUNNING, lsb, C[0], C[1])
    g.raw(0, 0, C_COMM, 0, (T + 150) & M32, 0x10001, 200)     # another event written in between
    g.raw(S_END, 0, C_PRTH, RUNNING, lsb, 30, 2)
    k = kev(g)
    ev = [e for e in k.events() if e.cls == C_PRTH]
    assert len(ev) == 1 and ev[0].ts == T + 100 and ev[0].words == (C[0], C[1], 30, 2) and not ev[0].partial


def test_combine_across_consecutive_buffers():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.raw(S_BEGIN, 0, C_PRTH, RUNNING, (T + 100) & M32, C[0], C[1])
    g.buffer(0, T + 200)                                       # next buffer of the same CPU, sequence + 1
    g.raw(S_END, 0, C_PRTH, RUNNING, (T + 100) & M32, 30, 2)
    k = kev(g)
    ev = [e for e in k.events() if e.cls == C_PRTH]
    assert len(ev) == 1 and ev[0].ts == T + 100 and not ev[0].partial
    assert k.stats.partial_combines == 0 and k.stats.orphan_slots == 0 and not k.losses


# ---- loss ------------------------------------------------------------------------
def test_lost_buffer_never_joins_combine_slots():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T + 50, RUNNING, C, 30)
    g.raw(S_BEGIN, 0, C_PRTH, READY, (T + 100) & M32, C[0], C[1])
    g.buffer(0, T + 300, seq=3)                                # buffer 2 missing
    g.raw(S_END, 0, C_PRTH, READY, (T + 100) & M32, 30, 2)     # same key: must not complete the begin
    k = kev(g)
    ready = [e for e in k.events() if e.cls == C_PRTH and e.code == READY]
    assert len(ready) == 1 and ready[0].partial and ready[0].words == (C[0], C[1])
    assert k.stats.orphan_slots == 1
    (l,) = k.losses
    assert (l.cpu, l.kind, l.count, l.t0, l.t1) == (0, "buffers", 1, T + 100, T + 300)


def test_short_buffer_is_a_loss():
    g = Gen(ncpu=1)
    b = g.buffer(0, T)
    g.state(0, T + 50, RUNNING, C, 30)
    g.raw(S_BEGIN, 0, C_PRTH, READY, (T + 100) & M32, C[0], C[1])
    b[2] = len(b[3]) + 3                                       # announces 3 slots the file does not have
    g.buffer(0, T + 300)
    g.raw(S_END, 0, C_PRTH, READY, (T + 100) & M32, 30, 2)
    k = kev(g)
    (l,) = k.losses
    assert (l.kind, l.count, l.t0, l.t1) == ("short buffer", 3, T, T + 300)      # the whole buffer is uncertain
    assert k.stats.short_buffers == 1
    # every combine that touches the short buffer is partial, even the RUNNING one that looked complete
    assert [e.partial for e in k.events() if e.cls == C_PRTH] == [True, True]


def test_sequence_wrap_reset_and_huge_gap():
    g = Gen(ncpu=1)
    for i, seq in enumerate([0xFFFFFFFE, 0xFFFFFFFF, 0, 1, 1 << 30, 7]):
        g.buffer(0, T + i * 1000, seq=seq)
        g.state(0, T + i * 1000 + 10, RUNNING, C)
    t0 = time.time()
    k = kev(g)
    k.events()
    assert time.time() - t0 < 1.0                              # nothing proportional to the gap
    assert [(l.kind, l.count, l.t0) for l in k.losses] == [
        ("buffers", 0xFFFFFFFD, None),                         # first buffer in the file is not number 1
        ("buffers", (1 << 30) - 2, T + 3010),                  # 2 .. 2^30-1
        ("sequence reset", None, T + 4010)]
    assert k.stats.lost_buffers == {0: 0xFFFFFFFD + (1 << 30) - 2} and k.stats.sequence_resets == 1


def test_dropped_events_make_state_unknown_and_exchanges_untrustworthy():
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 100, RUNNING, C, 30)
    g.send(0, T + 200, C, S[0], 0x10001, prio=30)
    g.receive(1, T + 300, S, C, 0x10001, prio=30)
    g.event(1, T + 400, C_CONTROL, 8, [17, 0])                 # DROPPED: 17 events lost on CPU 1
    g.reply(1, T + 5000, S, C, 0x10001, 30)                    # nobody known to run on CPU 1 now
    g.state(1, T + 5100, RUNNING, S, 10)
    g.send_exit(0, T + 6000, C, 30)
    k, m = model(g)
    assert k.stats.dropped_events == 17
    (l,) = k.losses
    assert (l.cpu, l.kind, l.count, l.t0, l.t1) == (1, "dropped", 17, T + 320, T + 400)
    assert any(sw[:2] == (T + 320, 1) and sw[2] == S and sw[5] == UNKNOWN for sw in m.switches)
    assert (T + 320, -1) in [(s[0], s[1]) for s in m.threads[S].states]       # UNKNOWN until its next event
    assert m.counters["events_without_actor"] >= 1                           # the reply is not attributed
    tx = m.txns[0]
    assert not tx.trustworthy and "spans lost events on cpu 1" in tx.warnings
    r = suspected_inversions(m)
    assert r["excluded_untrustworthy"] == 1 and r["checked"] == 0


def test_truncated_file():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T + 10, RUNNING, C, 30)
    g.state(0, T + 20, READY, C, 30)
    data = g.bytes()
    k = KevFile("cut.kev", data[:-8])                          # cut in the middle of the last slot
    k.events()
    assert k.stats.trailing_bytes == 8
    assert [(l.kind, l.t0) for l in k.losses] == [("truncated file", T + 20)]
    assert [e.partial for e in k.events() if e.cls == C_PRTH] == [False, True]


# ---- decoding and model --------------------------------------------------------------
def test_mixed_fast_and_wide_events():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T + 10, RUNNING, C)                             # fast thread events: no priority
    g.state(0, T + 11, RECEIVE, S)
    g.send(0, T + 20, C, S[0], 0x10001)                        # wide MsgSendv: coid, parts, msg[0..2]
    g.receive(0, T + 60, S, C, 0x10001)
    g.reply(0, T + 200, S, C, 0x10001)
    g.wait_receive(0, T + 240, S)
    g.send_exit(0, T + 260, C)
    g.state(0, T + 300, RUNNING, C)
    g.send(0, T + 310, C, S[0], 0x20001, wide=False)            # fast MsgSendv: coid and msg[0] only
    _, m = model(g)
    assert m.wide is False
    a, b = m.txns
    assert a.trustworthy and a.msg == (0x7101, 1, 0) and a.info.get("chid") == 1
    assert b.msg == (0x7101,) and b.t_call == T + 310


def test_injected_only_start():
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 1, RUNNING, C, 10, injected=True)
    g.state(0, T + 2, RECEIVE, S, 10, injected=True)
    g.kcall(0, T + 100, K_SENDV, [1, 1, 1, 0x7101, 1, 0])     # no traced THRUNNING yet: no actor
    _, m = model(g)
    assert m.switches == [] and m.txns == [] and m.counters["events_without_actor"] == 1
    assert m.threads[C].states[0][4] is True
    tp = tp_of(m, "injected")
    if tp:
        no_import_errors(tp)
        assert rows(tp, "SELECT COUNT(*) n FROM sched")[0].n == 0
        tp.close()


def test_migration_marks_old_cpu_unknown_not_idle():
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, C, 10)
    g.state(1, T + 100, RUNNING, C, 10)                        # runs on CPU 1 before CPU 0 switched
    g.state(0, T + 500, RUNNING, B, 10)
    _, m = model(g)
    assert [(sw[0], sw[2], sw[5]) for sw in m.switches if sw[1] == 0] == [
        (T + 10, None, C), (T + 100, C, UNKNOWN), (T + 500, UNKNOWN, B)]
    assert m.counters["migrations_without_switch_out"] == 1
    tp = tp_of(m, "migration")
    if tp:
        no_import_errors(tp)
        r = rows(tp, "SELECT s.ts, s.dur, t.name FROM sched s JOIN thread t USING (utid) WHERE s.cpu = 0 ORDER BY s.ts")
        assert [(x.ts - T, x.dur, x.name) for x in r][:2] == [(10, 90, "c"), (100, 400, "UNKNOWN")]
        tp.close()


def test_interrupted_send_and_late_reply():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T, RUNNING, C, 10)
    g.send(0, T + 10, C, S[0], 0x10001, prio=10)
    g.receive(0, T + 50, S, C, 0x10001, prio=10)
    g.state(0, T + 190, READY, C, 10)                          # a signal unblocks the client
    g.state(0, T + 200, RUNNING, C, 10)
    g.kcall(0, T + 210, K_SENDV, [], INT)                      # INT_CALL: interrupted, no parameters
    g.state(0, T + 300, RUNNING, S, 10)
    g.reply(0, T + 310, S, C, 0x10001, 10)                     # the server replies late
    _, m = model(g)
    (tx,) = m.txns
    assert tx.outcome == "interrupted" and tx.t_exit == T + 210 and not tx.trustworthy
    assert m.counters["interrupted_calls"] == 1
    assert m.counters["reply_without_txn"] == 1 and m.counters["reply_msg_without_txn"] == 1


def test_cancelled_send_is_complete_but_untrustworthy():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T, RUNNING, C, 10)
    g.send(0, T + 10, C, S[0], 0x10001, prio=10)
    g.receive(0, T + 50, S, C, 0x10001, prio=10)
    g.kcall(0, T + 80, 15, [0x10001, 1, 0, 0, 0, 0])           # server starts its reply...
    g.state(0, T + 90, READY, C, 10)                           # ...but the client is unblocked by a timeout
    g.kcall(0, T + 95, 15, [0, 0], EXIT)
    g.send_exit(0, T + 100, C, 10, status=0xFFFFFFFF)          # returns -1 without REPLY_MESSAGE
    _, m = model(g)
    (tx,) = m.txns
    assert tx.complete and not tx.trustworthy and tx.outcome == "cancelled"


def test_rcvid_reuse_and_late_completion_keep_the_new_exchange():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T, RUNNING, C, 10)
    g.send(0, T + 10, C, S[0], 0x10001)                        # X: C -> S, rcvid R
    g.state(0, T + 100, RUNNING, B, 10)
    g.send(0, T + 110, B, S[0], 0x10001)                       # Y reuses R while X looks open
    g.state(0, T + 200, RUNNING, S, 10)
    g.kcall(0, T + 205, 13, [0x20002, 3])                      # some MsgError call of the server ...
    g.comm(0, T + 210, 10, C[1], C[0])                         # ... carrying a late REPLY_MESSAGE for X's client
    g.kcall(0, T + 215, 13, [0, 0], EXIT)
    g.state(0, T + 220, RUNNING, C, 10)
    g.kcall(0, T + 230, K_SENDV, [0, 0, 0, 0], EXIT)           # X's late MsgSendv exit
    g.receive(0, T + 300, S, B, 0x10001)
    g.reply(0, T + 500, S, B, 0x10001)
    g.wait_receive(0, T + 540, S)
    g.send_exit(0, T + 600, B)
    _, m = model(g)
    x, y = m.txns
    assert "rcvid reused while still open" in x.warnings and not x.trustworthy
    assert y.trustworthy and y.client == B and y.t_exit == T + 610
    assert m.counters["reply_msg_without_txn"] == 1 and m.counters["send_exit_without_txn"] == 1
    assert not m._by_rcvid and not m._by_client


def test_thread_death_and_id_reuse():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T, RUNNING, C, 10)
    g.send(0, T + 10, C, S[0], 0x10001)
    g.state(0, T + 50, DEAD, C, 10)                            # the client dies while REPLY-blocked
    end = exchange(g, 0, T + 100, C, S, 0x20001)               # a new thread with the same pid/tid
    g.state(0, end, RUNNING, S, 10)
    g.reply(0, end + 10, S, C, 0x10001)                        # late reply to the dead client's rcvid
    _, m = model(g)
    x, y = m.txns
    assert not x.trustworthy and any("died" in w for w in x.warnings)
    assert y.trustworthy and y.rcvid == 0x20001
    assert m.counters["reply_without_txn"] == 1


def test_cross_thread_and_deferred_replies():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001)
    g.receive(0, T + 100, S, C, 0x10001)                       # s1 receives X
    g.wait_receive(0, T + 150, S)
    g.state(0, T + 200, RUNNING, B, 10)
    g.send(0, T + 210, B, S[0], 0x20001)
    g.receive(0, T + 300, S, B, 0x20001)                       # s1 receives Y, X still unreplied
    g.reply(0, T + 400, S, C, 0x10001)                         # s1 replies X: handle X [100, 400] crosses Y
    g.wait_receive(0, T + 450, S)
    g.state(0, T + 500, RUNNING, S2, 10)
    g.reply(0, T + 510, S2, B, 0x20001)                        # s2 replies Y (cross-thread)
    g.send_exit(0, T + 600, C)
    g.send_exit(0, T + 700, B)
    _, m = model(g)
    x, y = m.txns
    assert x.trustworthy and y.trustworthy
    assert (y.server, y.replier) == (S, S2) and m.counters["cross_thread_replies"] == 1
    assert y.parent is None and x.parent is None
    tp = tp_of(m, "deferred")
    if tp:
        no_import_errors(tp)
        h = rows(tp, "SELECT s.name, s.ts, s.dur, tr.name AS track FROM slice s JOIN track tr ON s.track_id = tr.id "
                     "WHERE s.name LIKE 'handle %' ORDER BY s.ts")
        assert [(r.name, r.ts - T, r.dur) for r in h] == [("handle client/c", 120, 280), ("handle other/b", 320, 190)]
        assert h[0].track != h[1].track                          # crossing lifetimes on separate tracks
        f = rows(tp, "SELECT a.name AS a, b.name AS b, extract_arg(a.arg_set_id, 'debug.txn') AS ta, "
                     "extract_arg(b.arg_set_id, 'debug.txn') AS tb FROM flow JOIN slice a ON a.id = slice_out "
                     "JOIN slice b ON b.id = slice_in ORDER BY a.ts")
        assert len(f) == 4 and all(r.ta == r.tb for r in f)
        rep = rows(tp, "SELECT t.name FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t "
                       "USING (utid) WHERE s.name = 'MsgReply' ORDER BY s.ts")
        assert [r.name for r in rep] == ["s1", "s2"]
        tp.close()


def test_ambiguous_parent():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T, RUNNING, C, 10)
    g.send(0, T + 10, C, S[0], 0x10001)
    g.state(0, T + 50, RUNNING, B, 10)
    g.send(0, T + 60, B, S[0], 0x20001)
    g.receive(0, T + 100, S, C, 0x10001)
    g.wait_receive(0, T + 130, S)
    g.receive(0, T + 150, S, B, 0x20001)                       # s1 holds two received requests
    g.send(0, T + 200, S, M[0], 0x30001)                       # a nested send: whose request is it for?
    _, m = model(g)
    x, y, z = m.txns
    assert z.parent is None and z.parent_candidates == [x.idx, y.idx]
    assert m.counters["ambiguous_parents"] == 1


def receive_pulse(g, cpu, ts, server, scoid, block=True):
    """The server takes one pulse in MsgReceivev(chid 1) and, with block, waits in RECEIVE again."""
    g.state(cpu, ts, RUNNING, server, 10)
    g.comm(cpu, ts + 10, RPULSE, scoid, server[0])
    g.kcall(cpu, ts + 20, K_RECEIVEV, [0, 0], EXIT)
    if block:
        g.kcall(cpu, ts + 30, K_RECEIVEV, [1, 1])
        g.state(cpu, ts + 40, RECEIVE, server, 10)


def test_pulse_matching_needs_a_synchronised_queue():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T, RUNNING, S, 10)
    g.kcall(0, T + 1, K_RECEIVEV, [1, 1])
    g.state(0, T + 2, RUNNING, C, 10)
    g.pulse(0, T + 10, C, S[0], 0x10001, 0)                    # two pulses queued before the first receive
    g.pulse(0, T + 30, C, S[0], 0x10001, 1)
    receive_pulse(g, 0, T + 100, S, 0x10001, block=False)       # 2 candidates
    g.kcall(0, T + 130, K_RECEIVEV, [1, 1])
    receive_pulse(g, 0, T + 150, S, 0x10001)                    # 1 candidate, but the queue is not in sync
    g.state(0, T + 300, RUNNING, C, 10)
    g.pulse(0, T + 310, C, S[0], 0x10001, 9)                    # after S blocked in RECEIVE: in sync
    receive_pulse(g, 0, T + 400, S, 0x10001)
    g.comm(0, T + 500, SPULSE, 0x10002, S[0])                  # no MsgSendPulse in progress: kernel or timer
    _, m = model(g)
    p0, p1, p2, p3 = m.pulses
    assert (p0.match, p0.candidates) == ("ambiguous", [0, 1])
    assert (p1.match, p1.candidates) == ("unsynced", [1])
    assert (p2.match, p2.sender, p2.value) == ("unique", C, 9)
    assert p3.sender is None and p3.match is None
    tp = tp_of(m, "pulses")
    if tp:
        no_import_errors(tp)
        assert rows(tp, "SELECT COUNT(*) n FROM flow")[0].n == 1   # an arrow only for the unique match
        tp.close()


def test_pulse_queue_loses_sync_at_a_loss():
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T, RUNNING, S, 10)
    g.kcall(0, T + 1, K_RECEIVEV, [1, 1])
    g.state(0, T + 2, RECEIVE, S, 10)
    g.state(1, T + 10, RUNNING, C, 10)
    g.pulse(1, T + 20, C, S[0], 0x10001, 0)
    receive_pulse(g, 0, T + 100, S, 0x10001)                    # learns the channel, then S blocks: in sync
    g.pulse(1, T + 200, C, S[0], 0x10001, 1)
    receive_pulse(g, 0, T + 300, S, 0x10001)                    # unique
    g.state(1, T + 400, RUNNING, C, 10)
    g.buffer(1, T + 5000, seq=g.seq[1] + 2)                    # CPU 1 loses a buffer: a pulse may be unseen
    g.pulse(1, T + 5010, C, S[0], 0x10001, 2)
    receive_pulse(g, 0, T + 5100, S, 0x10001)
    _, m = model(g)
    assert [p.match for p in m.pulses] == ["unsynced", "unique", "unsynced"]


# ---- inversion inference ---------------------------------------------------------------
def prio_scene(server_mask, preemptor, preemptor_prio, other_cpu_thread=None, other_prio=None, after_receive=True,
               chan_flags=0x1, sibling=False, gen=False):
    """hi (30) asks server s1 (10, fixed priority) for 0.5 ms of work; preemptor takes CPU 0 for 2 ms."""
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, S, 10)
    g.channel(0, T + 12, chan_flags)
    if sibling:
        g.state(0, T + 15, RECEIVE, S2, 10)                    # a second thread of the server process exists
    if server_mask is not None:
        g.runmask(0, T + 20, server_mask)
    g.wait_receive(0, T + 40, S, 10)
    if other_cpu_thread:
        g.state(1, T + 50, RUNNING, other_cpu_thread, other_prio)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, H, 30)
    g.send(0, T + ms + 10, H, S[0], 0x10001, prio=30)           # hi REPLY-blocked
    if after_receive:
        g.receive(0, T + ms + 40, S, H, 0x10001, prio=10)       # s1 receives at its own priority
    g.state(0, T + ms + 100_000, READY, S, 10)                   # preempted while handling hi's request
    g.state(0, T + ms + 100_010, RUNNING, preemptor, preemptor_prio)
    g.state(0, T + 3 * ms, NANOSLEEP, preemptor, preemptor_prio)
    if after_receive:
        g.state(0, T + 3 * ms + 10, RUNNING, S, 10)
    else:
        g.receive(0, T + 3 * ms + 10, S, H, 0x10001, prio=10)
    g.reply(0, T + 3 * ms + 400_000, S, H, 0x10001, 30)
    g.send_exit(0, T + 3 * ms + 400_100, H, 30)
    return g if gen else model(g)


def test_inversion_positive_after_receive():
    _, m = prio_scene(0x1, M, 20)
    r = suspected_inversions(m)
    (x,) = r["suspected"]
    assert x["client"] == "high/h" and x["server"] == "server/s1" and x["server_run_mask"] == "0x1"
    assert set(x["while"]) == {"REPLY"} and 1899 < x["inversion_us"] < 1901
    assert x["preempted_by"][0]["thread"] == "mid/m" and x["preempted_by"][0]["prio"] == 20
    assert "high/h (prio 30)" in x["evidence"] and "mid/m (prio 20) on CPU 0" in x["evidence"]
    assert "ran a thread above prio 10, at least one of them below the client's 30" in x["evidence"]
    assert r["insufficient_evidence"] == []
    tl = Timeline(m)
    segs = critical_path(m, m.txns[0], tl)
    assert sum(s.t1 - s.t0 for s in segs) == m.txns[0].t_exit - m.txns[0].t_call
    pre = [s for s in segs if s.kind == "server preempted"]
    assert len(pre) == 1 and pre[0].t1 - pre[0].t0 == 1_900_010 and "mid/m (prio 20)" in pre[0].label


def test_inversion_before_receive_is_found_too():
    _, m = prio_scene(0x1, M, 20, after_receive=False)
    (x,) = suspected_inversions(m)["suspected"]
    assert x["preempted_by"][0]["thread"] == "mid/m"


def test_no_inversion_from_an_unrelated_cpu():
    # CPU 0 is taken by a thread above the client; the medium thread runs on CPU 1, which s1 may not use
    _, m = prio_scene(0x1, (400, 2), 40, other_cpu_thread=M, other_prio=20)
    r = suspected_inversions(m)
    assert r["suspected"] == [] and r["insufficient_evidence"] == []


def test_unknown_run_mask_is_not_all_cpus():
    # the server is in fact pinned behind (400, 2) at prio 40 on CPU 1; CPU 0 runs mid (20); client 30.
    # Assuming "all CPUs" for the untraced mask would call this an inversion.
    _, m = prio_scene(None, M, 20, other_cpu_thread=(400, 2), other_prio=40)
    r = suspected_inversions(m)
    assert r["suspected"] == []
    (x,) = r["insufficient_evidence"]
    assert list(x["reasons"]) == ["run mask of server/s1 not traced"]


def test_run_mask_dropped_after_loss_and_inheriting_channel():
    _, m = prio_scene(0x1, M, 20, chan_flags=0x4001)            # _NTO_CHF_INHERIT_RUNMASK | FIXED_PRIORITY
    r = suspected_inversions(m)
    assert r["suspected"] == [] and "run mask of server/s1 not traced" in r["insufficient_evidence"][0]["reasons"]
    g = prio_scene(0x1, M, 20, gen=True)
    g.state(1, T + 400_000, RUNNING, C, 10)
    g.buffer(1, T + 600_000, seq=g.seq[1] + 2)                  # CPU 1 loses a buffer before hi sends
    _, m = model(g)
    assert Timeline(m).runmask_at(S, T + 1_000_000) is None
    r = suspected_inversions(m)
    assert r["suspected"] == []


def send_phase_scene(sibling):
    """s1 (10, CPU 0) handles b's request and is preempted by mid (20); hi (30) sends and stays SEND-blocked."""
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, S, 10)
    g.channel(0, T + 12, 0x1)
    if sibling:
        g.state(0, T + 15, RECEIVE, S2, 10)
    g.runmask(0, T + 20, 0x1)
    g.wait_receive(0, T + 40, S, 10)
    ms = 1_000_000
    g.state(0, T + ms, RUNNING, B, 10)
    g.send(0, T + ms + 10, B, S[0], 0x10001, prio=10)
    g.receive(0, T + ms + 40, S, B, 0x10001, prio=10)
    g.state(0, T + ms + 100_000, READY, S, 10)
    g.state(0, T + ms + 100_010, RUNNING, M, 20)
    g.state(0, T + ms + 200_000, READY, M, 20)
    g.state(0, T + ms + 200_010, RUNNING, H, 30)
    g.send(0, T + ms + 200_020, H, S[0], 0x20002, prio=30, block=SEND)
    g.state(0, T + ms + 200_100, RUNNING, M, 20)
    g.state(0, T + 3 * ms, NANOSLEEP, M, 20)
    g.state(0, T + 3 * ms + 10, RUNNING, S, 10)
    g.reply(0, T + 3 * ms + 100, S, B, 0x10001, 10)
    g.receive(0, T + 3 * ms + 200, S, H, 0x20002, prio=10)
    g.reply(0, T + 3 * ms + 600_000, S, H, 0x20002, 30)
    g.send_exit(0, T + 3 * ms + 600_100, H, 30)
    return model(g)


def test_send_phase_needs_a_sole_receiver():
    _, m = send_phase_scene(sibling=False)
    (x,) = suspected_inversions(m)["suspected"]
    assert set(x["while"]) == {"SEND"} and x["preempted_by"][0]["thread"] == "mid/m"
    _, m = send_phase_scene(sibling=True)                       # s2 could have received hi's message
    r = suspected_inversions(m)
    assert r["suspected"] == []
    assert "other threads of server may receive on the channel" in r["insufficient_evidence"][0]["reasons"]


def test_no_inversion_for_plain_blocking():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, B, 10)
    g.send(0, T + 20, B, S[0], 0x10001, prio=10)
    g.receive(0, T + 50, S, B, 0x10001, prio=10)                # s1 busy with b's request
    g.state(0, T + 1000, RUNNING, H, 30)
    g.send(0, T + 1010, H, S[0], 0x20001, prio=30, block=SEND)  # hi waits; s1 keeps running (inheritance)
    g.state(0, T + 1100, RUNNING, S, 30)
    g.reply(0, T + 2_000_000, S, B, 0x10001, 10)
    g.receive(0, T + 2_000_100, S, H, 0x20001, prio=30)
    g.reply(0, T + 2_500_000, S, H, 0x20001, 30)
    g.send_exit(0, T + 2_500_100, H, 30)
    _, m = model(g)
    r = suspected_inversions(m)
    assert r["checked"] == 1 and r["suspected"] == [] and r["insufficient_evidence"] == []


# ---- exact Perfetto output ------------------------------------------------------------------
def test_perfetto_exact_slices_threads_and_flows():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    end = exchange(g, 0, T + 1000, C, S, 0x10001, work=5000)
    _, m = model(g)
    (tx,) = m.txns
    assert tx.trustworthy and (tx.t_call, tx.t_rx_exit, tx.t_reply_call, tx.t_exit) == (
        T + 1010, T + 1060, T + 6060, T + 6130) and end == T + 6140
    tp = tp_of(m, "exact")
    if not tp:
        return
    no_import_errors(tp)
    q = ("SELECT s.id, s.name, s.ts, s.dur, s.depth, s.parent_id, t.tid, t.name AS tname, p.pid, p.name AS pname "
         "FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t USING (utid) "
         "LEFT JOIN process p USING (upid) ORDER BY s.ts, s.depth")
    sl = {r.name: r for r in rows(tp, q)}
    send, back, handle, rep = sl["MsgSend -> server"], sl["reply received"], sl["handle client/c"], sl["MsgReply"]
    assert (send.ts - T, send.dur, send.depth, send.tid, send.tname, send.pid, send.pname) == (
        1010, 5120, 0, 10001, "c", 100, "client")
    assert (back.ts - T, back.dur, back.depth, back.parent_id) == (6080, 50, 1, send.id)
    assert (handle.ts - T, handle.dur, handle.tid, handle.pid) == (1060, 5000, 20001, 200)
    assert (rep.ts - T, rep.dur, rep.tid) == (6060, 30, 20001)
    flows = {(r.slice_out, r.slice_in) for r in rows(tp, "SELECT slice_out, slice_in FROM flow")}
    assert flows == {(send.id, handle.id), (rep.id, back.id)}
    st = rows(tp, "SELECT s.name, s.ts, s.dur, extract_arg(s.arg_set_id, 'debug.qnx_prio') AS prio FROM slice s "
                  "JOIN track tr ON s.track_id = tr.id WHERE tr.name = 'c QNX state' ORDER BY s.ts")
    assert [(r.name, r.ts - T, r.dur, r.prio) for r in st] == [
        ("RUNNING", 1000, 30, 10), ("REPLY", 1030, 5050, 10), ("READY", 6080, 40, 10), ("RUNNING", 6120, 10, 10)]
    cp = rows(tp, "SELECT s.name, s.ts, s.dur, s.depth FROM slice s JOIN track tr ON s.track_id = tr.id "
                  "WHERE tr.name = 'c critical path' ORDER BY s.depth, s.ts")
    assert cp[0].name.startswith("txn 0 -> server") and (cp[0].ts - T, cp[0].dur) == (1010, 5120)
    assert sum(r.dur for r in cp if r.depth == 1) == 5120
    sched = rows(tp, "SELECT s.ts, s.dur, s.priority, t.tid FROM sched s JOIN thread t USING (utid) ORDER BY s.ts")
    assert [(r.ts - T, r.priority, r.tid) for r in sched][:3] == [(1000, 10, 10001), (1040, 10, 20001),
                                                                  (6120, 10, 10001)]
    waker = rows(tp, "SELECT t.tid, w.tid AS wtid FROM thread_state ts JOIN thread t USING (utid) "
                     "JOIN thread w ON w.utid = ts.waker_utid WHERE ts.state = 'R'")
    assert [(r.tid, r.wtid) for r in waker] == [(10001, 20001)]   # the reply's READY, proven by MsgReplyv
    tp.close()


def test_zero_length_and_rounded_slices_nest():
    g = Gen(ncpu=1, cps=3_000_000_000)                         # 3 cycles per ns: rounding merges timestamps
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 3, RECEIVE, S, 10)
    g.state(0, T + 4, RUNNING, C, 10)
    g.state(0, T + 4, READY, C, 10)                            # zero-length RUNNING
    g.state(0, T + 5, RUNNING, C, 10)
    exchange(g, 0, T + 7, C, S, 0x10001, work=1)
    _, m = model(g)
    tp = tp_of(m, "rounding")
    if tp:
        no_import_errors(tp)
        bad = rows(tp, "SELECT COUNT(*) n FROM slice c JOIN slice p ON c.parent_id = p.id "
                       "WHERE c.ts < p.ts OR c.ts + c.dur > p.ts + p.dur")
        assert bad[0].n == 0
        tp.close()



# ---- loss counterexamples (second review) ---------------------------------------------------------
def test_deleted_middle_slot_taints_combine_and_exchange():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    g.state(0, T + 1000, RUNNING, C, 10)
    g.send(0, T + 1010, C, S[0], 0x10001, prio=10)
    b = g.buffer(0, T + 1030)
    g.receive(0, T + 1040, S, C, 0x10001, prio=10)             # MsgReceivev exit: an 8-slot combine
    g.reply(0, T + 6060, S, C, 0x10001, 10)
    conts = [i for i, sl in enumerate(b[3]) if sl[0] >> 30 == S_CONT and (sl[0] >> 10) & 0x1F == C_KERCALL]
    b[2] = len(b[3])                                            # the header keeps the original count
    del b[3][conts[1]]                                         # one middle slot is missing from the file
    g.buffer(0, T + 6100)
    g.send_exit(0, T + 6120, C, 10)
    k, m = model(g)
    assert k.stats.short_buffers == 1 and k.stats.tainted_combines >= 1
    assert all(e.partial for e in k.events() if e.cls == C_KERCALL and e.code == K_RECEIVEV | EXIT)
    (tx,) = m.txns
    assert not tx.trustworthy and "spans lost events on cpu 0" in tx.warnings


def test_loss_at_trace_start_flags_overlapping_exchange():
    g = Gen(ncpu=2)
    g.buffer(1, T)
    g.names(1, T, NAMES)
    g.state(1, T + 10, RUNNING, C, 10)
    g.send(1, T + 20, C, S[0], 0x10001, prio=10)
    g.buffer(0, T + 500, seq=3)                                 # buffers 1 and 2 of CPU 0 are missing
    g.state(0, T + 510, RUNNING, S, 10)
    g.reply(0, T + 600, S, C, 0x10001, 10)
    g.send_exit(1, T + 700, C, 10)
    k, m = model(g)
    (l,) = k.losses
    assert (l.cpu, l.t0, l.t1) == (0, None, T + 500)
    (tx,) = m.txns
    assert not tx.trustworthy and "spans lost events on cpu 0" in tx.warnings


def test_loss_drops_run_mask_path_and_calls():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 5, RECEIVE, S, 10)
    g.state(0, T + 10, RUNNING, C, 10)
    g.runmask(0, T + 20, 0x1)
    opened(g, 0, T + 100, C, S, "/data/f", 3, 0x10001)
    g.state(0, T + 800, RUNNING, C, 10)
    g.kcall(0, T + 810, K_PULSE, [0x40000001, 10, 5, 1])       # C enters MsgSendPulse ...
    g.buffer(0, T + 2000, seq=g.seq[0] + 2)                    # ... and the next buffer is lost
    g.state(0, T + 2010, RUNNING, C, 10)
    g.comm(0, T + 2020, SPULSE, 0x10001, S[0])                 # its call is not known any more
    exchange(g, 0, T + 3000, C, S, 0x20002, msg=(0x0101, 4096, 0), coid=3)
    _, m = model(g)
    tl = Timeline(m)
    assert tl.runmask_at(C, T + 30) == 0x1 and tl.runmask_at(C, T + 2500) is None
    assert m.pulses[0].sender is None
    first, after = m.txns[0], m.txns[-1]
    assert first.path == "/data/f" and after.path is None


def test_ready_inside_a_loss_window_does_not_end_unknown():
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, B, 10)
    g.state(1, T + 20, RECEIVE, S, 10)
    g.state(1, T + 1000, READY, S, 10)                         # an intact CPU sees S READY inside the window
    g.state(0, T + 50, RUNNING, M, 10)
    g.buffer(0, T + 5000, seq=g.seq[0] + 2)                    # CPU 0 loses a buffer: window T+50 .. T+5000
    g.state(1, T + 7000, RUNNING, S, 10)
    _, m = model(g)
    tl = Timeline(m)
    assert tl.state_at(S, T + 500)[0] == -1
    assert tl.state_at(S, T + 1500)[0] == -1                   # the READY at T+1000 holds only at its instant
    assert tl.state_at(S, T + 7500)[0] == RUNNING


def test_partial_thread_event_does_not_restore_actor():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, B, 10)
    g.raw(S_BEGIN, 0, C_PRTH, RUNNING, (T + 100) & M32, C[0], C[1])    # its end slot is lost with buffer 2
    g.buffer(0, T + 300, seq=g.seq[0] + 2)
    g.kcall(0, T + 310, K_SENDV, [0x40000001, 1, 1, 0x7101, 1, 0])
    g.comm(0, T + 320, 0, 0x10001, S[0])
    _, m = model(g)
    assert all(sw[5] != C for sw in m.switches)
    assert m.txns == [] and m.counters["partial_state_events"] == 1


# ---- critical path counterexamples ----------------------------------------------------------------
def test_clip_recomputes_attribution():
    _, m = prio_scene(0x1, M, 20)
    a, b = ("mid/m (prio 20) on CPU 0", "m (20)"), ("high/h (prio 30) on CPU 0", "h (30)")
    seg = Segment(0, 100, "server preempted", "", S, 10, 0, pieces=[(0, 50, b), (50, 100, a)])
    (c,) = _clip(m, [seg], 80, 100)
    assert c.held_by == {a: 20} and "mid/m (prio 20) on CPU 0" in c.label and "high/h" not in c.label


def test_untrustworthy_child_is_not_followed():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 1, RECEIVE, M, 10)
    g.state(0, T + 2, RECEIVE, S, 10)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001)
    g.receive(0, T + 50, S, C, 0x10001)
    g.send(0, T + 100, S, M[0], 0x20002)                      # nested request of s1 to mid ...
    g.receive(0, T + 140, M, S, 0x20002, info_client=(999, 9))   # ... whose _msg_info contradicts the trace
    g.reply(0, T + 300, M, S, 0x20002)
    g.wait_receive(0, T + 340, M)
    g.send_exit(0, T + 360, S)
    g.reply(0, T + 400, S, C, 0x10001)
    g.wait_receive(0, T + 440, S)
    g.send_exit(0, T + 460, C)
    _, m = model(g)
    x, z = m.txns
    assert x.trustworthy and not z.trustworthy and z.parent == x.idx
    segs = critical_path(m, x, Timeline(m))
    assert not any(sg.kind == "nested request" for sg in segs)
    assert any(sg.kind == "server blocked" and "not followed" in sg.label for sg in segs)


def test_unreplied_request_keeps_the_server_ambiguous():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 1, RECEIVE, S, 10)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001)
    g.receive(0, T + 50, S, C, 0x10001)                        # never replied in the trace
    g.wait_receive(0, T + 80, S)
    g.state(0, T + 100, RUNNING, B, 10)
    g.send(0, T + 110, B, S[0], 0x20002)
    g.receive(0, T + 140, S, B, 0x20002)
    g.reply(0, T + 400, S, B, 0x20002)
    g.send_exit(0, T + 460, B)
    _, m = model(g)
    y = m.txns[1]
    segs = critical_path(m, y, Timeline(m))
    assert any("2 requests open" in sg.label for sg in segs)
    assert not any(sg.label.endswith("running this request") for sg in segs)


def test_ready_beside_an_idle_cpu_is_unexplained():
    _, m = prio_scene(None, M, 20, other_cpu_thread=IDLE1, other_prio=0)
    segs = critical_path(m, m.txns[0], Timeline(m))
    assert any(sg.kind == "unexplained ready" and "CPU 1 idle" in sg.label for sg in segs)
    assert not any(sg.kind == "server preempted" for sg in segs)


def test_empty_run_mask_does_not_crash():
    _, m = prio_scene(0x4, M, 20)                               # CPU 2 does not exist in a 2-CPU trace
    segs = critical_path(m, m.txns[0], Timeline(m))
    assert any(sg.kind == "unexplained ready" and "names no CPU" in sg.label for sg in segs)
    assert any("names no CPU" in k for x in suspected_inversions(m)["insufficient_evidence"] for k in x["reasons"])


# ---- transactions ----------------------------------------------------------------------------------
def test_stale_reply_is_quarantined():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 1, RECEIVE, S, 10)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001)
    g.receive(0, T + 50, S, C, 0x10001)
    g.wait_receive(0, T + 80, S)
    g.send_exit(0, T + 100, C, 10, status=0xFFFFFFFF)          # X is cancelled
    g.send(0, T + 120, C, S[0], 0x20002, block=SEND)           # Y, still waiting for the server
    g.state(0, T + 200, RUNNING, S, 10)
    g.kcall(0, T + 210, K_REPLYV, [0x10001, 1, 0, 0, 0, 0])    # the server's late reply to X ...
    g.comm(0, T + 220, REPLYMSG, C[1], C[0])                   # ... names C, whose open exchange is Y
    g.kcall(0, T + 230, K_REPLYV, [0, 0], EXIT)
    g.receive(0, T + 300, S, C, 0x20002)
    g.reply(0, T + 500, S, C, 0x20002)
    g.send_exit(0, T + 600, C)
    _, m = model(g)
    x, y = m.txns
    assert x.outcome == "cancelled" and m.counters["reply_without_txn"] == 1
    assert (y.t_rx_exit, y.t_reply_msg, y.outcome, y.t_exit) == (T + 320, T + 510, "replied", T + 610)
    assert m.counters["reply_msg_quarantined"] == 1 and not y.trustworthy


def test_ambiguous_parent_is_not_trustworthy():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.state(0, T, RUNNING, C, 10)
    g.send(0, T + 10, C, S[0], 0x10001)
    g.state(0, T + 50, RUNNING, B, 10)
    g.send(0, T + 60, B, S[0], 0x20001)
    g.receive(0, T + 100, S, C, 0x10001)
    g.wait_receive(0, T + 130, S)
    g.receive(0, T + 150, S, B, 0x20001)
    g.send(0, T + 200, S, M[0], 0x30001)
    _, m = model(g)
    z = m.txns[2]
    assert z.parent is None and any("parent ambiguous" in w for w in z.warnings) and not z.trustworthy


# ---- wakeups ---------------------------------------------------------------------------------------
def test_wakeup_needs_object_evidence():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T, RUNNING, C, 10)
    g.state(0, T + 5, SEM, B, 10)
    g.kcall(0, T + 10, K_SEM_POST, [0x1234, 0])                # C posts some semaphore ...
    g.state(0, T + 20, READY, B, 10)                           # ... and B becomes READY: maybe an interrupt
    g.kcall(0, T + 30, K_SEM_POST, [0], EXIT)
    g.state(0, T + 40, RECEIVE, S, 10)
    g.send(0, T + 100, C, S[0], 0x10001)
    g.state(0, T + 115, READY, S, 10)                          # inside C's MsgSendv, after SND_MESSAGE
    g.receive(0, T + 200, S, C, 0x10001)                       # S receives that very message
    g.state(0, T + 300, RECEIVE, S2, 10)
    g.state(0, T + 310, RUNNING, C, 10)
    g.send(0, T + 320, C, S[0], 0x20002)
    g.state(0, T + 335, READY, S2, 10)
    g.receive(0, T + 400, S, C, 0x20002)                       # but s1 receives it, not s2
    _, m = model(g)
    assert [(w[2], w[3], w[5]) for w in m.wakeups] == [(C, S, "received the message")]
    tp = tp_of(m, "wakeup")
    if tp:
        no_import_errors(tp)
        r = rows(tp, "SELECT COUNT(*) n FROM thread_state WHERE waker_utid IS NOT NULL")
        assert r[0].n == 1
        tp.close()


# ---- paths and names -------------------------------------------------------------------------------
def opened(g, cpu, ts, client, server, path, coid, rcvid, first=(0x00020100, 0, 0)):
    """open(): PATHMGR of the opening thread, ConnectAttach to the server, then a first message on the coid."""
    g.state(cpu, ts, RUNNING, client, 10)
    g.pathmgr(cpu, ts + 5, client, path)
    g.connect_attach(cpu, ts + 10, server[0], coid)
    g.send(cpu, ts + 20, client, server[0], rcvid, msg=first, coid=coid)
    g.receive(cpu, ts + 50, server, client, rcvid, msg=first)
    g.reply(cpu, ts + 200, server, client, rcvid)
    g.wait_receive(cpu, ts + 240, server)
    g.send_exit(cpu, ts + 260, client)
    return ts + 300


def test_paths_need_a_confirmed_open_and_names_a_resource_manager():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 1, RECEIVE, S, 10)
    g.state(0, T + 2, RECEIVE, M, 10)
    t = opened(g, 0, T + 10, C, M, "/data/a", 3, 0x10001, first=(0x7101, 1, 0))   # unrelated ConnectAttach
    t = exchange(g, 0, t, C, M, 0x20002, msg=(0x0101, 4096, 0), coid=3)         # mid never answered _IO_CONNECT
    t = opened(g, 0, t, B, S, "/data/b", 4, 0x30003)                             # a real open
    exchange(g, 0, t, B, S, 0x40004, msg=(0x0101, 4096, 0), coid=4)
    _, m = model(g)
    a_open, a_read, b_open, b_read = m.txns
    assert a_open.path is None and a_read.path is None and a_read.msg_type is None
    assert a_read.msg_desc.startswith("type 0x0101")
    assert (b_open.msg_type, b_open.path) == ("_IO_CONNECT_OPEN", "/data/b")
    assert (b_read.msg_type, b_read.path) == ("_IO_READ", "/data/b")


# ---- diff inputs -----------------------------------------------------------------------------------
def test_latency_rows_are_roles_with_unfinished_and_excluded():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 1, RECEIVE, S, 10)
    t = exchange(g, 0, T + 10, W1, S, 0x10001, cprio=10)
    t = exchange(g, 0, t, W2, S, 0x20002, cprio=20)            # same name, other priority: another role
    g.state(0, t, RUNNING, W2, 20)
    g.send(0, t + 10, W2, S[0], 0x30003, prio=20)              # never returns in the trace
    _, m = model(g)
    rows_ = {r["client"]: r for r in ipc_latency(m)}
    assert set(rows_) == {"work/w@10", "work/w@20"}
    assert (rows_["work/w@10"]["n"], rows_["work/w@20"]["n"], rows_["work/w@20"]["unfinished"]) == (1, 1, 1)


def test_server_time_is_credited_once():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, C, 10)
    g.send(0, T + 20, C, S[0], 0x10001, prio=10)
    g.receive(0, T + 100, S, C, 0x10001, prio=10)              # X: received at T+120
    g.wait_receive(0, T + 150, S, 10)                          # s1 RECEIVE-blocked from T+160
    g.state(0, T + 200, RUNNING, B, 10)
    g.send(0, T + 210, B, S[0], 0x20001, prio=10)
    g.receive(0, T + 300, S, B, 0x20001, prio=10)              # Y: received at T+320, s1 now holds both
    g.reply(0, T + 400, S, C, 0x10001, 10)
    g.reply(0, T + 500, S, B, 0x20001, 10)
    g.send_exit(0, T + 600, C, 10)
    g.send_exit(0, T + 700, B, 10)
    _, m = model(g)
    sp = server_priority_while_handling(m)
    got = {k[1]: round(v[10], 3) for k, v in sp.items()}
    # cps 1e9: s1 runs 60 ns while it holds only X (T+120..160 and T+300..320), 80 ns holding both
    # (credited once), 100 ns while it holds only Y
    assert got == {"client/c": 0.06, SEVERAL: 0.08, "other/b": 0.1}

if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok", name)
            except Exception as e:   # report every failure, then exit nonzero
                failed += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, e)
    sys.exit(1 if failed else 0)
