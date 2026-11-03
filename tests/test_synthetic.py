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
from kevgen import (C_COMM, C_CONTROL, C_PRTH, DEAD, EXIT, INT, K_PULSE, K_SENDV, M32, NANOSLEEP, READY,  # noqa: E402
                    RECEIVE, REPLY, RPULSE, RUNNING, S_BEGIN, S_END, SEND, SPULSE, Gen)
from tracestudio.analyses import Timeline, suspected_inversions  # noqa: E402
from tracestudio.critical import critical_path  # noqa: E402
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import UNKNOWN, Model  # noqa: E402

T = 5 << 32
C, B, S, S2, M, H, IDLE1 = (100, 1), (101, 1), (200, 1), (200, 2), (300, 1), (400, 1), (1, 2)
NAMES = {1: ("procnto-smp-instr", {1: "idle_cpu_0", 2: "idle_cpu_1"}), 100: ("/bin/client", {1: "c"}),
         101: ("/bin/other", {1: "b"}), 200: ("/bin/server", {1: "s1", 2: "s2"}), 300: ("/bin/mid", {1: "m"}),
         400: ("/bin/high", {1: "h"})}
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


def exchange(g, cpu, t, client, server, rcvid, cprio=10, sprio=10, work=1000):
    """Client running at t, server RECEIVE-blocked: one full exchange on one CPU. Returns the end time."""
    g.state(cpu, t, RUNNING, client, cprio)
    g.send(cpu, t + 10, client, server[0], rcvid, prio=cprio)
    g.receive(cpu, t + 40, server, client, rcvid, prio=sprio)
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
    assert (l.kind, l.count, l.t0, l.t1) == ("short buffer", 3, T + 100, T + 300)
    assert k.stats.short_buffers == 1 and k.stats.orphan_slots == 1
    assert [e.partial for e in k.events() if e.cls == C_PRTH and e.code == READY] == [True]


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


def test_pulse_matching_confidence():
    g = Gen(ncpu=1)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T, RUNNING, C, 10)
    for i in range(2):                                         # two pulses queued before the receive
        g.kcall(0, T + 10 + 100 * i, K_PULSE, [0x40000001, 10, 5, i])
        g.comm(0, T + 20 + 100 * i, SPULSE, 0x10001, S[0])
        g.kcall(0, T + 30 + 100 * i, K_PULSE, [0], EXIT)
    g.state(0, T + 300, RUNNING, S, 10)
    g.comm(0, T + 310, RPULSE, 0x10001, S[0])
    g.comm(0, T + 320, RPULSE, 0x10001, S[0])
    g.kcall(0, T + 400, K_PULSE, [0x40000001, 10, 5, 9])       # a pulse from S itself, received alone
    g.comm(0, T + 410, SPULSE, 0x10001, S[0])
    g.kcall(0, T + 420, K_PULSE, [0], EXIT)
    g.comm(0, T + 430, RPULSE, 0x10001, S[0])
    g.comm(0, T + 500, SPULSE, 0x10002, S[0])                  # no MsgSendPulse in progress: kernel or timer
    _, m = model(g)
    p0, p1, p2, p3 = m.pulses
    assert (p0.match, p0.candidates, p0.confidence) == ("ambiguous", [0, 1], 0.5)
    assert (p1.match, p1.confidence) == ("ambiguous", None)
    assert (p2.match, p2.confidence, p2.sender, p2.value) == ("unique", 1.0, S, 9)
    assert p3.sender is None and p3.context == S and p3.match is None
    tp = tp_of(m, "pulses")
    if tp:
        no_import_errors(tp)
        assert rows(tp, "SELECT COUNT(*) n FROM flow")[0].n == 1   # an arrow only for the unique match
        tp.close()


# ---- inversion inference ---------------------------------------------------------------
def prio_scene(server_mask, preemptor, preemptor_prio, other_cpu_thread=None, other_prio=None, after_receive=True):
    """hi (30) asks server s1 (10, fixed priority) for 0.5 ms of work; preemptor takes CPU 0 for 2 ms."""
    g = Gen(ncpu=2)
    g.buffer(0, T)
    g.names(0, T, NAMES)
    g.state(0, T + 10, RUNNING, S, 10)
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
    return model(g)


def test_inversion_positive_after_receive():
    _, m = prio_scene(0x1, M, 20)
    r = suspected_inversions(m)
    (x,) = r["suspected"]
    assert x["client"] == "high/h" and x["server"] == "server/s1" and x["server_run_mask"] == "0x1"
    assert set(x["while"]) == {"REPLY"} and 1899 < x["inversion_us"] < 1901
    assert x["preempted_by"][0]["thread"] == "mid/m" and x["preempted_by"][0]["prio"] == 20
    assert "high/h (prio 30)" in x["evidence"] and "mid/m (prio 20) on CPU 0" in x["evidence"]
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


def test_no_run_mask_and_an_idle_cpu_is_insufficient_evidence():
    _, m = prio_scene(None, M, 20, other_cpu_thread=IDLE1, other_prio=0)
    r = suspected_inversions(m)
    assert r["suspected"] == []
    (x,) = r["insufficient_evidence"]
    assert any("CPU 1 ran procnto-smp-instr/idle_cpu_1 at prio 0" in k and "no run mask" in k for k in x["reasons"])


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
