# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Regression checks on real QNX 8.0 traces (tests/data/run1, captured with
workload/run_scenarios.sh). Run with pytest or directly:  python3 tests/test_tracestudio.py
Optional parts run when available: host traceprinter ($QNX_HOST) and the perfetto package.
"""
import gzip
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402
from tracestudio.validate import validate  # noqa: E402
from tracestudio.analyses import deadline_misses, suspected_inversions  # noqa: E402

DATA = os.path.join(HERE, "data", "run1")
TMP = tempfile.mkdtemp(prefix="ts-test-")
SCEN = ["ipc_wide", "ipc_fast", "prio_inherit", "prio_fixed"]


def kev(name):
    out = os.path.join(TMP, name + ".kev")
    if not os.path.exists(out):
        with gzip.open(os.path.join(DATA, name + ".kev.gz")) as f, open(out, "wb") as g:
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
        print("skip: traceprinter not on PATH")
        return
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
        print("skip: perfetto package not installed")
        return
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
        tp.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
