# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Regression checks on real QNX 8.0 traces (tests/data/run1, captured with
workload/run_scenarios.sh). Run with pytest or directly:  python3 tests/test_tracestudio.py
The traceprinter comparison needs the QNX SDP host tools on PATH and the Perfetto checks
need the perfetto package; both are skipped when absent unless TS_REQUIRE_ALL=1, which
makes a missing tool a failure (use it for release checks).
"""
import gzip
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from drop_buffer import drop_buffers  # noqa: E402
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402
from tracestudio.validate import load_logs, validate  # noqa: E402
from tracestudio.analyses import deadline_misses, suspected_inversions  # noqa: E402

DATA = os.path.join(HERE, "data", "run1")
DATA3 = os.path.join(HERE, "data", "run3")
TMP = tempfile.mkdtemp(prefix="ts-test-")
SCEN = ["ipc_wide", "ipc_fast", "prio_inherit", "prio_fixed"]


def skip(why):
    if os.environ.get("TS_REQUIRE_ALL"):
        raise AssertionError("required for release checks: " + why)
    print("skip:", why)


def kev(name, data=DATA):
    out = os.path.join(TMP, name + ".kev")
    if not os.path.exists(out):
        with gzip.open(os.path.join(data, name + ".kev.gz")) as f, open(out, "wb") as g:
            shutil.copyfileobj(f, g)
    return out


def test_parser_invariants():
    for s in SCEN:
        k = KevFile(kev(s))
        evs = k.events()
        assert k.stats.orphan_slots == 0 and k.stats.partial_combines == 0, s
        assert not k.stats.lost_buffers and not k.losses and not k.stats.short_buffers, s
        assert all(a.ts <= b.ts for a, b in zip(evs, evs[1:])), s
        assert k.stats.rollovers >= 2, s    # 2.7 GHz TSC wraps 32 bits every 1.6 s


def test_traceprinter_event_by_event():
    tp = shutil.which("traceprinter")
    if not tp:
        return skip("traceprinter not on PATH (source qnxsdp-env.sh)")
    for s in SCEN:
        txt = os.path.join(TMP, s + ".txt")
        subprocess.run([tp, "-f", kev(s), "-o", txt], check=True)
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "compare_traceprinter.py"), kev(s), txt],
                           capture_output=True, text=True)
        assert r.returncode == 0, s + "\n" + r.stdout


def test_ground_truth_links():
    for s in SCEN:
        res, gt, _ = validate(Model(KevFile(kev(s))), os.path.join(DATA, s + ".logs"))
        assert res["requests"] == (200 if s.startswith("ipc") else 40), s
        assert res["matched"] == res["requests"], (s, res["examples"])
        assert not any(":WRONG" in k or ":missing" in k for k in res["checks"]), (s, res["checks"])


def test_priority_story():
    fixed = Model(KevFile(kev("prio_fixed")))
    inherit = Model(KevFile(kev("prio_inherit")))
    f = deadline_misses(fixed, "hi", 5000)["ts_psrv"]
    i = deadline_misses(inherit, "hi", 5000)["ts_psrv"]
    assert (f["requests"], f["missed"], f["excluded"]) == (20, 20, 0), f   # all miss with a fixed-priority channel
    assert (i["requests"], i["missed"], i["excluded"]) == (20, 0, 0), i    # none with priority inheritance
    inv_f, inv_i = suspected_inversions(fixed), suspected_inversions(inherit)
    sus = inv_f["suspected"]
    assert len(sus) == 20 and all(x["client"].endswith("/hi") for x in sus), sus
    assert all(x["preempted_by"][0]["thread"].endswith("/hog") for x in sus)
    assert all(x["server_run_mask"] == "0x1" and x["server_prio_while_ready"] == 10 for x in sus)
    assert all(x["while"].get("SEND", 0) > 7000 for x in sus)       # hog runs while hi waits for the receive
    assert not inv_f["insufficient_evidence"]
    assert inv_i["suspected"] == [] and inv_i["insufficient_evidence"] == []
    assert any(v["flags"] & 1 for v in fixed.channels.values())         # _NTO_CHF_FIXED_PRIORITY seen
    assert not any(v["flags"] & 1 for v in inherit.channels.values())


def test_perfetto_loads():
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        return skip("perfetto package not installed")
    for s in ("ipc_wide", "prio_fixed"):
        out = os.path.join(TMP, s + ".perfetto-trace")
        write(Model(KevFile(kev(s))), out)
        tp = TraceProcessor(trace=out)
        n = {r.t: r.n for r in tp.query(
            "SELECT 'sched' t, COUNT(*) n FROM sched UNION ALL SELECT 'thread_state', COUNT(*) FROM thread_state "
            "UNION ALL SELECT 'flow', COUNT(*) FROM flow UNION ALL SELECT 'msgsend', COUNT(*) FROM slice "
            "WHERE name LIKE 'MsgSend -> ts_%'")}
        assert n["sched"] > 1000 and n["thread_state"] > 1000 and n["flow"] > 500, n
        assert n["msgsend"] == (202 if s == "ipc_wide" else 41), n   # requests + forwards + QUIT
        errs = [(r.name, r.value) for r in tp.query(
            "SELECT name, value FROM stats WHERE value > 0 AND severity IN ('error', 'data_loss')")]
        assert not errs, errs
        bad_flows = list(tp.query(
            "SELECT COUNT(*) n FROM flow f JOIN slice a ON a.id = f.slice_out JOIN slice b ON b.id = f.slice_in "
            "WHERE extract_arg(a.arg_set_id, 'debug.txn') IS NOT extract_arg(b.arg_set_id, 'debug.txn')"))[0].n
        assert bad_flows == 0
        assert list(tp.query("SELECT COUNT(*) n FROM slice WHERE dur < 0"))[0].n == 0
        if s == "prio_fixed":
            m = Model(KevFile(kev(s)))
            want = sorted(m.kev.cycles_to_ns(t.t_exit) - m.kev.cycles_to_ns(t.t_call) for t in m.txns
                          if m.threads[t.client].name == "hi" and m.server_name(t) == "ts_psrv")
            got = sorted(r.dur for r in tp.query(
                "SELECT s.dur FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t USING (utid) "
                "WHERE t.name = 'hi' AND s.name = 'MsgSend -> ts_psrv'"))
            assert len(want) == 20 and got == want, (got, want)
            cp = list(tp.query(
                "SELECT COUNT(*) n FROM slice s JOIN track tr ON s.track_id = tr.id "
                "WHERE tr.name = 'hi critical path' AND s.depth = 0 AND s.name LIKE 'txn % -> ts_psrv: %'"))[0].n
            assert cp == 20, cp
        tp.close()


def test_inversion_after_receive_on_a_real_capture():
    # run 3: lo sends 0.5 ms of work at t+0, hi at t+4 ms, the hog wakes at t+4.2 ms, while
    # the server already handles hi's request
    for name, n_inv, missed in (("prio_fixed_rx", 20, 20), ("prio_inherit_rx", 0, 0)):
        m = Model(KevFile(kev(name, DATA3)))
        res, _, _ = validate(m, os.path.join(DATA3, name + ".logs"))
        assert res["ok"] and res["matched"] == res["requests"] == 40, (name, res["checks"])
        d = deadline_misses(m, "hi", 5000)["ts_psrv"]
        assert (d["requests"], d["missed"]) == (20, missed), (name, d)
        sus = suspected_inversions(m)["suspected"]
        assert len(sus) == n_inv, (name, len(sus))
        assert all(set(x["while"]) == {"REPLY"} and x["preempted_by"][0]["thread"] == "ts_pload/hog" for x in sus)


def test_cancellation_matches_the_workload_log():
    m = Model(KevFile(kev("cancel", DATA3)))
    rows = load_logs(os.path.join(DATA3, "cancel.logs"))
    sends = {(r["tid"], r["id"]): r for r in rows if r["ev"] == "SEND"}
    seen = 0
    for r in rows:
        if r["ev"] != "SEND_DONE":
            continue
        s = sends[(r["tid"], r["id"])]
        (tx,) = [t for t in m.txns if t.client == (r["pid"], r["tid"]) and t.t_call is not None
                 and s["cycles"] <= t.t_call <= r["cycles"]]
        if r["b"] == -1:      # ETIMEDOUT (tmo) or EINTR (sig): MsgSendv exits with -1, no INT_CALL is traced
            assert r["c"] in (4, 260) and tx.outcome == "cancelled" and tx.status == -1 and not tx.trustworthy
        else:
            assert tx.outcome == "replied" and tx.trustworthy, tx
        seen += 1
    assert seen == 15
    late = sum(1 for r in rows if r["ev"] == "REPLY" and r["b"] == -1)
    assert late == 10 and m.counters["reply_without_txn"] == late     # late replies never touch other exchanges


def test_resource_manager_names_and_paths():
    m = Model(KevFile(kev("rm_small", DATA3)))
    read = {}
    for t in m.txns:
        if t.msg_type == "_IO_READ" and t.path and m.label(t.client).startswith("cat/") and t.status and t.status > 0:
            read[t.path] = read.get(t.path, 0) + t.status
    assert read == {"/system/lib/libxo.so.0": 132400, "/data/ts_rm.bin": 65536}   # sizes from wc -c on the target
    to_disk = {t.msg_type for t in m.txns if m.server_name(t) == "devb-eide"}
    assert {"_IO_CONNECT_OPEN", "_IO_READ", "_IO_WRITE", "_IO_CLOSE", "_IO_CONNECT_UNLINK", "_IO_LSEEK"} <= to_disk
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        return skip("perfetto package not installed")
    out = os.path.join(TMP, "rm_small.perfetto-trace")
    write(m, out)
    tp = TraceProcessor(trace=out)
    r = list(tp.query("SELECT COUNT(*) n, SUM(extract_arg(arg_set_id, 'debug.path') = '/system/lib/libxo.so.0') p "
                      "FROM slice WHERE name = 'MsgSend _IO_READ -> devb-eide'"))[0]
    assert r.n >= 30 and r.p >= 30, (r.n, r.p)
    tp.close()


def test_fault_injection_never_links_wrong():
    k = KevFile(kev("ipc_wide"))
    data, nbuf, removed = drop_buffers(k, {79, 80, 83, 84})   # two buffers per CPU inside the workload window
    k2 = KevFile("dropped.kev", data)
    assert [(l.kind, l.count) for l in k2.losses] == [("buffers", 1)] * 4
    res, _, _ = validate(Model(k2), os.path.join(DATA, "ipc_wide.logs"))
    assert not any(":WRONG" in c for c in res["checks"]), res["checks"]
    assert res["matched"] < res["requests"] == 200 and not res["ok"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    import test_synthetic   # noqa: E402  (runs the unit tests too when started directly)
    for name, fn in list(vars(test_synthetic).items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
