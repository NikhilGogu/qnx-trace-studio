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
    for x in inv_f["insufficient_evidence"] + inv_i["insufficient_evidence"]:   # only requests to procnto
        assert list(x["reasons"]) == [k for k in x["reasons"] if k.startswith("request handed off")], x
        assert x["server"].startswith("procnto-smp-instr/"), x
    assert inv_i["suspected"] == []
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
            assert tx.errno == r["c"]                  # EINTR or ETIMEDOUT, from the failed exit's errno word
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
    r = list(tp.query("SELECT COUNT(*) n, SUM(extract_arg(arg_set_id, 'debug.path_candidate') = "
                      "'/system/lib/libxo.so.0') p FROM slice WHERE name = 'MsgSend _IO_READ (candidate) -> devb-eide'"))[0]
    assert r.n >= 30 and r.p >= 30, (r.n, r.p)
    tp.close()


def test_cli_exit_codes():
    from tracestudio.cli import main
    empty = tempfile.mkdtemp(prefix="ts-empty-")
    assert main(["validate", kev("prio_fixed"), "--logs", empty]) == 1             # no logged requests
    assert main(["validate", kev("prio_fixed"), "--logs", os.path.join(DATA, "prio_fixed.logs")]) == 0
    junk = os.path.join(TMP, "not-a-trace.kev")
    with open(junk, "w") as f:
        f.write("not a trace")
    assert main(["info", junk]) == 1 and main(["analyze", junk]) == 1


def test_fault_injection_never_links_wrong():
    k = KevFile(kev("ipc_wide"))
    data, nbuf, removed = drop_buffers(k, {79, 80, 83, 84})   # two buffers per CPU inside the workload window
    k2 = KevFile("dropped.kev", data)
    assert [(l.kind, l.count) for l in k2.losses] == [("buffers", 1)] * 4
    res, _, _ = validate(Model(k2), os.path.join(DATA, "ipc_wide.logs"))
    links = ("server_pid", "server_tid", "rcvid", "coid", "payload_id", "parent_link")
    assert not any(c.startswith(links) and c.endswith(":WRONG") for c in res["checks"]), res["checks"]
    assert "WRONG_unflagged" not in res["checks"]          # outcomes contradict the log only on flagged exchanges
    assert res["matched"] < res["requests"] == 200 and not res["ok"]



def test_check_fails_prio_fixed_and_passes_prio_inherit():
    import contextlib
    import io
    import xml.etree.ElementTree as ET
    from tracestudio.cli import main
    opts = ["--deadline", "hi=5000", "--no-inversions", "--max-insufficient", "2"]
    for name, code in (("prio_fixed", 1), ("prio_inherit", 0)):
        xml = os.path.join(TMP, name + ".junit.xml")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(["check", kev(name)] + opts + ["--junit", xml]) == code
        text = out.getvalue()
        root = ET.parse(xml).getroot()
        (suite,) = root.findall("testsuite")
        cases = suite.findall("testcase")
        assert root.tag == "testsuites" and suite.get("tests") == "3" and suite.get("errors") == "0"
        assert [c.get("name") for c in cases] == ["trace integrity", "deadline hi=5000 us",
                                                  "no suspected priority inversions"]
        failures = [c.find("failure") for c in cases]
        assert "matched ts_pload/hi@30 -> procnto-smp-instr (1), ts_pload/hi@30 -> ts_psrv (20)" in text
        assert "2 with insufficient evidence (at most 2 allowed)" in text
        if code:
            assert "FAIL deadline hi=5000 us: 20 of 21 trustworthy requests over 5000 us, max 11429.5 us" in text
            assert "FAIL no suspected priority inversions: 20 suspected; checked 672 exchanges" in text
            assert "txn 520: ts_pload/hi (prio 30)" in text
            assert suite.get("failures") == "2" and failures[0] is None
            assert failures[1].get("type") == "DeadlineMissed" and "txn 524: ts_pload/hi -> ts_psrv" in failures[1].text
            assert failures[2].get("type") == "PriorityInversion" and "ts_pload/hog (prio 20)" in failures[2].text
        else:
            assert "check: 3 assertions, 0 failed" in text and "FAIL" not in text
            assert suite.get("failures") == "0" and failures == [None, None, None]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):                       # inconclusive requests count against --no-inversions
        assert main(["check", kev("prio_inherit"), "--no-inversions"]) == 1
    assert "2 with insufficient evidence (at most 0 allowed)" in out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):                       # both are requests to procnto from other clients
        assert main(["check", kev("prio_inherit"), "--no-inversions", "--client", "ts_pload/"]) == 0
    assert "checked 118 exchanges of clients matching 'ts_pload/'" in out.getvalue(), out.getvalue()


def test_open_converts_once_and_pins_the_slowest_clients():
    import contextlib
    import io
    import shutil
    from tracestudio import __version__
    from tracestudio.cli import main
    from tracestudio.serve import cache_path, default_pins
    pins, labels = default_pins(Model(KevFile(kev("prio_fixed"))))
    assert pins == ["^lo (critical path|QNX state)", "^hi (critical path|QNX state)"]
    assert labels == ["ts_pload/lo", "ts_pload/hi"]
    assert default_pins(Model(KevFile(kev("prio_fixed"))), [("hi@ts_psrv", 5000.0)])[1] == ["ts_pload/hi"]
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="ts-cache-")
    try:
        path = os.path.join(TMP, "open_me.kev")
        shutil.copy(kev("prio_fixed"), path)
        cached = cache_path(path, __version__)
        for step in ("converted ", "using ", "incomplete; converting again"):
            if step.startswith("incomplete"):
                with open(cached, "r+b") as f:                # an interrupted write must not be reused
                    f.truncate(1000)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                assert main(["open", path, "--no-browser", "--port", "0", "--timeout", "1", "--pin", "^hi "]) == 1
            assert step in out.getvalue() and "pinning tracks matching ^hi " in out.getvalue()
        assert os.path.getsize(cached) > 1000 and not os.path.exists(os.path.join(TMP, "open_me.perfetto-trace"))
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old


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
