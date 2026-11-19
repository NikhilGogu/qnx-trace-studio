#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Seeded scheduling regressions: score trace-studio against a deadline-only gate.

Each workload variant below is a healthy parent scenario with one scheduling bug planted in it (priority
inheritance off on a channel, a PTHREAD_PRIO_NONE mutex, a run mask narrowed to one CPU, a hog raised above the
client, a thread pool cut to one receiver, ...), or a healthy parent itself (a control). The variants are
scenarios of workload/run_scenarios.sh. Capture each several times on a QNX target, one directory per round:

    sh run_scenarios.sh /tmp/ts/bin /tmp/ts/out VARIANT...        # on the target, once per round
    python3 tools/seeded_regressions.py ROUND_DIR... --md table.md --json rows.json

For every capture it records two answers:

- baseline: what a deadline-only gate gives today: tracelogger wrote the trace, traceprinter decodes it, and the
  workload's own log (its MsgSend latencies) says how many of hi's requests missed the deadline. It sees a miss.
  It names no cause.
- trace-studio: `tracestudio check --deadline ... --no-inversions --client CLIENT` (exit status, and which
  assertion failed), plus the cause it names for every request that missed: the suspected priority inversion with
  its evidence, or, when there is none, the longest kind of the request's critical path and who held the CPU.

The expected cause of every bug is fixed below before any capture is scored. A cause is correct only when every
request that missed is explained by it and no request of the client is called a suspected inversion that the
variant does not have. "insufficient evidence" is counted on its own: it is neither a correct nor a wrong cause.
Controls must pass both gates; any failure there is a false alarm.
"""
import argparse
import contextlib
import glob
import io
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tracestudio.analyses import Timeline, suspected_inversions, us  # noqa: E402
from tracestudio.cli import main as tracestudio_main  # noqa: E402
from tracestudio.critical import critical_path, to_json  # noqa: E402
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402

PLOAD = {"client": "ts_pload/hi", "deadline_us": 5000}
MUTEX = {"client": "ts_mutex/hi", "server": "ts_mutex", "deadline_us": 4000}


def inversion(servers, preemptor, phase=None, via=None):
    return {"inversion": True, "servers": servers, "preemptor": preemptor, "phase": phase, "via": via}


def no_inversion(kind, thread, prio=None):
    return {"inversion": False, "kind": kind, "thread": thread, "prio": prio}


# name: (parent, the change, gate, expected cause or None for a control)
VARIANTS = {
    "prio_inherit": (None, "control: ts_psrv inherits hi's priority", dict(PLOAD, server="ts_psrv"), None),
    "prio_inherit_rx": (None, "control: as prio_inherit, hog wakes with hi", dict(PLOAD, server="ts_psrv"), None),
    "prio_inherit_late": (None, "control: lo asks for 6 ms, hog wakes 1 ms after hi sends",
                          dict(PLOAD, server="ts_psrv"), None),
    "mutex_chain_hog": (None, "control: mutex with priority inheritance, hog at 20", MUTEX, None),
    "pool_inherit": (None, "control: two inheriting receivers, lo and lo2 fill them", dict(PLOAD, server="ts_pool"),
                     None),
    "pool_wide": (None, "control: one fixed-priority receiver that may use CPU 0 and 1",
                  dict(PLOAD, server="ts_pool"), None),
    "pool_long": (None, "control: two inheriting receivers, lo asks for 9 ms", dict(PLOAD, server="ts_pool"), None),
    "prio_fixed": ("prio_inherit", "channel created with _NTO_CHF_FIXED_PRIORITY (inheritance off)",
                   dict(PLOAD, server="ts_psrv"), inversion(["ts_psrv/psrv-rx"], "ts_pload/hog", "SEND")),
    "prio_fixed_rx": ("prio_inherit_rx", "inheritance off; the hog strikes after the receive",
                      dict(PLOAD, server="ts_psrv"), inversion(["ts_psrv/psrv-rx"], "ts_pload/hog", "REPLY")),
    "prio_hog_high": ("prio_inherit_late", "hog raised from 20 to 35, above hi",
                      dict(PLOAD, server="ts_psrv"), no_inversion("server preempted", "ts_pload/hog", 35)),
    "prio_hi_low": ("prio_inherit_late", "hi lowered from 30 to 15, below the hog",
                    dict(PLOAD, server="ts_psrv", hi_prio=15), no_inversion("server preempted", "ts_pload/hog", 20)),
    "mutex_chain_nopi": ("mutex_chain_hog", "mutex initialised with PTHREAD_PRIO_NONE", MUTEX,
                         inversion(["ts_mslow/slow-rx"], "ts_mutex/hog", via="ts_mutex/holder")),
    "mutex_chain_slowfixed": ("mutex_chain_hog", "the nested server ts_mslow's channel fixed-priority", MUTEX,
                              inversion(["ts_mslow/slow-rx"], "ts_mutex/hog", via="ts_mutex/holder")),
    "pool_fixed": ("pool_inherit", "the pool's channel fixed-priority", dict(PLOAD, server="ts_pool"),
                   inversion(["ts_pool/pool-rx"], "ts_pload/hog", "SEND")),
    "pool_narrow": ("pool_wide", "the receiver's run mask narrowed from 0x3 to 0x1", dict(PLOAD, server="ts_pool"),
                    inversion(["ts_pool/pool-rx0"], "ts_pload/hog", "SEND")),
    "pool_long_one": ("pool_long", "the pool cut from two receivers to one", dict(PLOAD, server="ts_pool"),
                      no_inversion("server busy", "ts_pload/lo")),
    "pool_long_fixed": ("pool_long", "the pool's channel fixed-priority", dict(PLOAD, server="ts_pool"),
                        inversion(["ts_pool/pool-rx"], "ts_pload/hog", "REPLY")),
}


# ---- baseline: the deadline-only gate ----------------------------------------------------------------------
def _csv(path):
    with open(path) as f:
        head = f.readline()
        cps = int(head.split("cycles_per_sec=")[1].split()[0])
        f.readline()
        rows = [ln.rstrip("\n").split(",") for ln in f if ln.strip()]
    return cps, rows


def baseline(kev_path, gate):
    """The workload's own log: how many of hi's MsgSend calls took longer than the deadline. traceprinter only
    confirms the trace decodes. Columns: cycles,role,pid,tid,ev,id,a,b,c,d."""
    logs = kev_path[:-len(".kev")] + ".logs"
    missed = n = 0
    for path in glob.glob(os.path.join(logs, "*.csv")):
        cps, rows = _csv(path)
        role = rows[0][1] if rows else ""
        if role == "pload":                # SEND: b = prio; SEND_DONE: c = latency in cycles
            hi_tids = {r[3] for r in rows if r[4] == "SEND" and int(r[7]) == gate.get("hi_prio", 30)}
            for r in rows:
                if r[4] == "SEND_DONE" and r[3] in hi_tids:
                    n += 1
                    missed += int(r[8]) * 1e6 / cps > gate["deadline_us"]
        elif role == "mutex":              # hi is the main thread: SEND and SEND_DONE with the same id
            send = {r[5]: int(r[0]) for r in rows if r[4] == "SEND"}
            for r in rows:
                if r[4] == "SEND_DONE" and r[5] in send:
                    n += 1
                    missed += (int(r[0]) - send[r[5]]) * 1e6 / cps > gate["deadline_us"]
    tp = shutil.which("traceprinter")
    tp_ok = None
    if tp:
        r = subprocess.run([tp, "-f", kev_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tp_ok = r.returncode == 0
    return {"requests": n, "missed": int(missed), "detects_miss": missed > 0, "names_cause": False,
            "traceprinter_ok": tp_ok}


# ---- trace-studio -------------------------------------------------------------------------------------------
def run_check(kev_path, gate):
    args = ["check", kev_path, "--deadline", "hi@%s=%d" % (gate["server"], gate["deadline_us"]),
            "--no-inversions", "--client", gate["client"]]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = tracestudio_main(args)
    lines = out.getvalue().splitlines()
    fails = [ln[5:].split(":")[0] for ln in lines if ln.startswith("FAIL ")]
    return {"exit": code, "failed": fails, "summary": [ln for ln in lines if ln.startswith(("PASS ", "FAIL "))],
            "command": "tracestudio " + " ".join(args)}


def _dominant(cp):
    """(kind, thread named by the longest segment of that kind)"""
    kind = max(cp["by_kind_us"].items(), key=lambda kv: kv[1])[0]
    segs = []

    def walk(ss):
        for s in ss:
            segs.append(s)
            walk(s.get("nested") or [])
    walk(cp["segments"])
    seg = max((s for s in segs if s["kind"] == kind), key=lambda s: s["us"])
    held = seg.get("cpu_held_by_us") or {}
    top = max(held.items(), key=lambda kv: kv[1])[0] if held else None
    return kind, seg, top


def causes(kev_path, gate):
    """The cause trace-studio names for every request of the client over the deadline."""
    m = Model(KevFile(kev_path))
    tl = Timeline(m)
    client = gate["client"]
    only = (lambda key: client in m.label(key))
    inv = suspected_inversions(m, tl=tl, only=only)
    by_txn = {r["txn"]: r for r in inv["suspected"]}
    unclear = {r["txn"]: r for r in inv["insufficient_evidence"]}
    late = []
    for t in m.txns:
        if not (t.trustworthy and only(t.client) and m.server_name(t) == gate["server"]):
            continue
        if us(m, t.t_exit - t.t_call) <= gate["deadline_us"]:
            continue
        row = {"txn": t.idx, "latency_us": round(us(m, t.t_exit - t.t_call), 1)}
        if t.idx in by_txn:
            r = by_txn[t.idx]
            row.update({"cause": "suspected priority inversion", "phase": sorted(r["while"]),
                        "held_up": [(h["thread"], h["prio"], h["chain"]) for h in r["held_up"]],
                        "preemptor": (r["preempted_by"][0]["thread"], r["preempted_by"][0]["prio"])})
        elif t.idx in unclear:
            row.update({"cause": "insufficient evidence", "reasons": unclear[t.idx]["reasons"]})
        elif t.causal:
            cp = to_json(m, t, critical_path(m, t, tl))
            kind, seg, top = _dominant(cp)
            row.update({"cause": kind, "segment": seg["what"], "thread": seg.get("thread"), "cpu_held_by": top,
                        "by_kind_us": cp["by_kind_us"]})
        else:
            row.update({"cause": "not analysed (ambiguous parent)"})
        late.append(row)
    return {"late": late, "suspected": [r["txn"] for r in inv["suspected"]],
            "insufficient": [r["txn"] for r in inv["insufficient_evidence"]], "checked": inv["checked"]}


def _held_label(top):
    """'ts_pload/hog (prio 35) on CPU 0' -> ('ts_pload/hog', 35)"""
    if not top or " (prio " not in top:
        return top, None
    name, rest = top.split(" (prio ", 1)
    return name, int(rest.split(")")[0])


def judge(expect, c):
    """'correct', 'wrong', 'insufficient evidence' or 'none' (no request missed) for one capture."""
    if not c["late"]:
        return "none", "no request over the deadline"
    wrong, unclear = [], 0
    for r in c["late"]:
        cause = r["cause"]
        if cause == "insufficient evidence":
            unclear += 1
            continue
        if expect["inversion"]:
            ok = cause == "suspected priority inversion" and r["preemptor"][0] == expect["preemptor"] and all(
                any(h[0].startswith(s) for s in expect["servers"]) for h in r["held_up"])
            if ok and expect["phase"]:
                ok = expect["phase"] in r["phase"]
            if ok and expect["via"]:
                ok = all(h[2] and expect["via"] in h[2] for h in r["held_up"])
        else:
            ok = cause == expect["kind"]
            if ok and expect["kind"] == "server preempted":
                ok = _held_label(r["cpu_held_by"]) == (expect["thread"], expect["prio"])
            elif ok and expect["kind"] == "server busy":
                ok = "running %s's request" % expect["thread"] in r["segment"]
        if not ok:
            wrong.append(r["txn"])
    late = {r["txn"] for r in c["late"]}
    extra = [t for t in c["suspected"] if t not in late] if expect["inversion"] else c["suspected"]
    if not expect["inversion"] and extra:
        wrong += extra                       # an inversion the variant does not have
    if wrong:
        return "wrong", "txns %s" % wrong[:5]
    if unclear:
        return "insufficient evidence", "%d of %d late requests" % (unclear, len(c["late"]))
    return "correct", "%d of %d late requests" % (len(c["late"]), len(c["late"]))


def score(dirs, tp=True):
    rows = []
    for d in dirs:
        for name, (parent, change, gate, expect) in VARIANTS.items():
            path = os.path.join(d, name + ".kev")
            if not os.path.exists(path):
                continue
            b = baseline(path, gate) if tp else dict(baseline(path, gate), traceprinter_ok=None)
            ch = run_check(path, gate)
            c = causes(path, gate)
            row = {"round": os.path.basename(os.path.normpath(d)), "variant": name, "parent": parent,
                   "change": change, "control": expect is None, "baseline": b, "check": ch,
                   "late": len(c["late"]), "suspected": len(c["suspected"]), "insufficient": len(c["insufficient"])}
            if expect is None:
                row["ours_alarm"] = ch["exit"] != 0
                row["baseline_alarm"] = b["detects_miss"]
            else:
                row["ours_detects_miss"] = "deadline hi@%s=%d us" % (gate["server"], gate["deadline_us"]) in ch["failed"]
                row["verdict"], row["verdict_detail"] = judge(expect, c)
                row["ours_names_cause"] = row["verdict"] in ("correct", "wrong")
            row["causes"] = c["late"]
            rows.append(row)
    return rows


def table(rows):
    out = ["| variant | change | captures | baseline: detects miss | baseline: names cause | trace-studio: check "
           "fails | trace-studio: names cause | trace-studio: correct cause | insufficient evidence | wrong cause |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    ctl = ["| control | what | captures | baseline: false alarm | trace-studio: false alarm (check exit 1) |",
           "|---|---|---|---|---|"]
    for name, (parent, change, gate, expect) in VARIANTS.items():
        rs = [r for r in rows if r["variant"] == name]
        if not rs:
            continue
        n = len(rs)
        if expect is None:
            ctl.append("| %s | %s | %d | %d of %d | %d of %d |" % (
                name, change[len("control: "):], n, sum(r["baseline_alarm"] for r in rs), n,
                sum(r["ours_alarm"] for r in rs), n))
            continue
        cnt = {v: sum(r["verdict"] == v for r in rs) for v in ("correct", "wrong", "insufficient evidence")}
        out.append("| %s | %s | %d | %d of %d | 0 of %d | %d of %d | %d of %d | %d of %d | %d | %d |" % (
            name, change, n, sum(r["baseline"]["detects_miss"] for r in rs), n, n,
            sum(r["check"]["exit"] != 0 for r in rs), n, sum(r["ours_names_cause"] for r in rs), n,
            cnt["correct"], n, cnt["insufficient evidence"], cnt["wrong"]))
    return "\n".join(out + [""] + ctl) + "\n"


def totals(rows):
    bugs = [r for r in rows if not r["control"]]
    ctls = [r for r in rows if r["control"]]
    variants_caught = sum(
        1 for v in {r["variant"] for r in bugs}
        if all(r["verdict"] == "correct" for r in bugs if r["variant"] == v))
    return {
        "bug_variants": len({r["variant"] for r in bugs}), "bug_captures": len(bugs),
        "control_variants": len({r["variant"] for r in ctls}), "control_captures": len(ctls),
        "baseline_detects_miss": sum(r["baseline"]["detects_miss"] for r in bugs),
        "baseline_names_cause": 0,
        "ours_check_fails": sum(r["check"]["exit"] != 0 for r in bugs),
        "ours_detects_miss": sum(r["ours_detects_miss"] for r in bugs),
        "ours_correct_cause": sum(r["verdict"] == "correct" for r in bugs),
        "ours_insufficient": sum(r["verdict"] == "insufficient evidence" for r in bugs),
        "ours_wrong_cause": sum(r["verdict"] == "wrong" for r in bugs),
        "variants_correct_in_every_capture": variants_caught,
        "baseline_false_alarms": sum(r["baseline_alarm"] for r in ctls),
        "ours_false_alarms": sum(r["ours_alarm"] for r in ctls),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+", help="one directory per round, with VARIANT.kev and VARIANT.logs/")
    ap.add_argument("--md", help="write the comparison table (Markdown)")
    ap.add_argument("--json", help="write every row with its evidence (JSON)")
    ap.add_argument("--skip-traceprinter", action="store_true")
    a = ap.parse_args(argv)
    rows = score(a.dirs, tp=not a.skip_traceprinter)
    t = totals(rows)
    text = table(rows) + "\n" + "\n".join("%s: %s" % kv for kv in t.items()) + "\n"
    print(text, end="")
    if a.md:
        with open(a.md, "w") as f:
            f.write(text)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"totals": t, "rows": rows}, f, indent=1, default=str)
    ok = t["ours_wrong_cause"] == 0 and t["ours_false_alarms"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
