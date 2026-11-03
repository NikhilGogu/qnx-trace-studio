# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Check decoded message-passing transactions against the workload's own logs.

The workload (workload/*.c) logs every request with ClockCycles() taken just
before MsgSend() and just after it returns, plus the server-side rcvid,
receiving thread and timestamps. ClockCycles() and trace timestamps share one
time base, so each logged request must contain exactly one decoded
transaction from the same client thread, no other logged request may claim
that transaction, and it must name the same server thread, rcvid and coid and
end with the logged outcome. Validation fails (ok = False) when there are no
logged requests, when a SEND or DONE record has no partner, when any request
is not matched, when any check (including the parent link of a forwarded
request) is wrong or missing, or when the pulse counts differ from the log.
The workloads it understands are ts_clients/ts_front/ts_back and ts_pload/
ts_psrv; the cancellation scenario is checked by its own regression test.
"""
from __future__ import annotations

import csv
import glob
import os
from collections import defaultdict


INT_CELLS = ("cycles", "pid", "tid", "id", "a", "b", "c", "d")
CELLS = INT_CELLS + ("role", "ev")


def load_logs(logdir):
    """All rows of the workload's CSV logs; ValueError for a short or non-numeric row."""
    rows = []
    for path in sorted(glob.glob(os.path.join(logdir, "*.csv"))):
        with open(path, newline="") as f:
            for n, r in enumerate(csv.DictReader(line for line in f if not line.startswith("#")), 1):
                missing = [k for k in CELLS if r.get(k) in (None, "")]
                if missing:
                    raise ValueError("%s: record %d lacks %s" % (os.path.basename(path), n, ", ".join(missing)))
                for k in INT_CELLS:
                    r[k] = int(r[k])
                rows.append(r)
    return rows


def ground_truth(rows, unpaired=None):
    """Requests as seen by the workload: list of dicts. SEND or DONE records without a partner are appended
    to `unpaired` (when given) as (problem, record)."""
    gt = []
    sends = {}
    lost = [] if unpaired is None else unpaired
    for r in rows:
        key = (r["pid"], r["tid"], r["id"])
        if r["ev"] in ("SEND", "FWD_SEND"):
            if key in sends:
                lost.append(("SEND without DONE", sends[key]))
            sends[key] = r
        elif r["ev"] in ("SEND_DONE", "FWD_DONE"):
            s = sends.pop(key, None)
            if s is None or (s["ev"] == "SEND") != (r["ev"] == "SEND_DONE"):
                lost.append(("DONE without SEND", r))
                continue
            gt.append({"id": r["id"], "client": (r["pid"], r["tid"]), "t0": min(s["cycles"], r["cycles"]),
                       "t1": max(s["cycles"], r["cycles"]), "coid": s["a"] if r["ev"] == "SEND_DONE" else r["a"],
                       "status": r["b"], "forwarded": r["ev"] == "FWD_DONE", "role": r["role"], "row": r})
    lost.extend(("SEND without DONE", s) for s in sends.values())
    rx = defaultdict(list)
    reply = defaultdict(list)
    for r in rows:
        if r["ev"] == "RX" and r["id"]:
            rx[(r["role"], r["id"])].append(r)
        elif r["ev"] == "REPLY":
            reply[(r["role"], r["id"])].append(r)
    for g in gt:
        srole = {"clients": "front", "front": "back"}.get(g["role"])
        if g["role"] == "pload":
            srole = next((k[0] for k in rx if k[0].startswith("psrv") and k[1] == g["id"]), None)
        g["server_role"] = srole
        g["rx"] = rx.get((srole, g["id"]), [None])[0]
        g["reply"] = reply.get((srole, g["id"]), [None])[0]
    return gt


def validate(model, logdir, wide=None):
    rows = load_logs(logdir)
    unpaired = []
    gt = ground_truth(rows, unpaired)
    wide = model.wide if wide is None else wide
    by_client = defaultdict(list)
    for tx in model.txns:
        by_client[tx.client].append(tx)
    res = {"requests": len(gt), "matched": 0, "problems": defaultdict(int), "examples": [],
           "latency_err_us": [], "checks": defaultdict(int)}
    gt_by_id_client = {}
    tx_for_gt = {}
    cps = model.kev.cycles_per_sec
    cands_of, claims = {}, defaultdict(int)
    for g in gt:
        cands = cands_of[id(g)] = [t for t in by_client.get(g["client"], [])
                                   if t.t_call is not None and g["t0"] <= t.t_call <= g["t1"]
                                   and (t.t_exit is None or t.t_exit <= g["t1"])]
        if len(cands) == 1:
            claims[id(cands[0])] += 1
    for g in gt:
        cands = cands_of[id(g)]
        ok = True
        why = []
        if len(cands) == 1 and claims[id(cands[0])] > 1:   # two logged requests cannot be one exchange
            res["problems"]["shared_with_another_request"] += 1
            why.append("transaction claimed by %d logged requests" % claims[id(cands[0])])
            ok = False
        elif len(cands) != 1:
            res["problems"]["candidates=%d" % len(cands)] += 1
            why.append("candidates=%d" % len(cands))
            ok = False
        else:
            t = cands[0]
            tx_for_gt[id(g)] = t
            rx = g["rx"]
            if rx is None:
                why.append("no server log row")
                ok = False
            else:
                # each check is True (agrees), None (decoder has no value: missing events) or False (contradicts)
                def cmp(val, want):
                    return None if val is None else val == want
                st = g["status"]
                checks = {
                    "server_pid": cmp(t.server_pid, rx["pid"]),
                    "server_tid": cmp(t.server, (rx["pid"], rx["tid"])),
                    "rcvid": cmp(t.rcvid, rx["a"] & 0xFFFFFFFF),
                    "coid": cmp(t.coid, g["coid"] & 0xFFFFFFFF),
                    # MsgSend's return value: the reply status, or -1 when the call failed
                    "outcome": None if t.outcome is None or t.status is None else (
                        t.outcome == "replied" and t.status == st if st >= 0
                        else t.outcome in ("cancelled", "error", "interrupted") and t.status == st),
                    "rx_before_log": None if t.t_rx_exit is None else t.t_rx_exit <= rx["cycles"],
                    "complete": True if t.complete else None,
                    "trustworthy": True if t.trustworthy else None,
                }
                if g["reply"] is not None:
                    checks["reply_after_log"] = None if t.t_reply_call is None else t.t_reply_call >= g["reply"]["cycles"]
                if wide:
                    checks["payload_id"] = None if len(t.msg) < 2 else t.msg[1] == g["id"]
                for k, v in checks.items():
                    tag = {True: ":ok", None: ":missing", False: ":WRONG"}[v]
                    res["checks"][k + tag] += 1
                    if v is not True:
                        why.append(k + tag)
                        ok = False
                        if v is False:
                            res["checks"]["WRONG_flagged" if t.warnings else "WRONG_unflagged"] += 1
            if t.t_exit is not None:
                res["latency_err_us"].append(((g["t1"] - g["t0"]) - (t.t_exit - t.t_call)) * 1e6 / cps)
        gt_by_id_client[(g["id"], g["role"])] = g
        if ok:
            res["matched"] += 1
        elif len(res["examples"]) < 10:
            res["examples"].append((g["role"], g["id"], g["client"], why))
    # parent links: a forwarded request must be a child of the client's request
    for g in gt:
        if not g["forwarded"]:
            continue
        parent_g = gt_by_id_client.get((g["id"], "clients"))
        t, pt = tx_for_gt.get(id(g)), tx_for_gt.get(id(parent_g)) if parent_g else None
        if t is None or pt is None:
            tag = ":missing"
        elif t.parent is None:
            tag = ":missing"
        else:
            tag = ":ok" if t.parent == pt.idx else ":WRONG"
        res["checks"]["parent_link" + tag] += 1
    # decoded transactions of workload clients that the logs do not explain
    wl_clients = {g["client"] for g in gt}
    explained = {id(t) for t in tx_for_gt.values()}
    extra = [t for t in model.txns if t.client in wl_clients and id(t) not in explained]
    res["unexplained_decoded"] = len(extra)
    res["unexplained_examples"] = [(t.idx, model.label(t.client), "->", model.processes[t.server_pid].name
                                    if t.server_pid in model.processes else t.server_pid,
                                    ["0x%x" % w for w in t.msg]) for t in extra[:6]]
    # rcvid reuse across requests (ambiguity the key must survive)
    seen = defaultdict(int)
    for t in model.txns:
        seen[(t.server_pid, t.rcvid)] += 1
    res["rcvid_reused_keys"] = sum(1 for v in seen.values() if v > 1)
    res["rcvid_max_reuse"] = max(seen.values()) if seen else 0
    # concurrent receivers: server threads per server process in the workload
    srv = defaultdict(set)
    for g in gt:
        if g["rx"] is not None:
            srv[g["server_role"]].add(g["rx"]["tid"])
    res["receiver_threads"] = {k: len(v) for k, v in srv.items()}
    wc = defaultdict(int)
    for t in model.txns:
        for w in t.warnings:
            wc[w.split(" (")[0].split(" on ")[0]] += 1
    res["txn_warnings"] = dict(wc)
    res["decoder_counters"] = {k: v for k, v in model.counters.items() if v}
    # pulses: client MsgSendPulse (value = request id) and the back-end's timer pulses
    sent = [r for r in rows if r["ev"] == "PULSE_SEND"]
    dec = [p for p in model.pulses if p.kind == "pulse" and p.sender is not None]
    matched = 0
    for r in sent:
        if any(p.sender == (r["pid"], r["tid"]) and p.value == r["id"] and p.receiver is not None
               and p.receiver[0] == next((x["pid"] for x in rows if x["role"] == "front"), -1) for p in dec):
            matched += 1
    back = next((x["pid"] for x in rows if x["role"] == "back"), None)
    ticks_log = sum(1 for r in rows if r["role"] == "back" and r["ev"] == "PULSE_RX")
    ticks_dec = sum(1 for p in model.pulses if p.server_pid == back and p.t_recv is not None and p.receiver is not None)
    res["pulses"] = {"client_pulses_logged": len(sent), "client_pulses_matched_with_value": matched,
                     "back_pulses_logged": ticks_log, "back_pulses_received_in_trace": ticks_dec}
    # pulse evidence that applies: values only in wide mode, receive counts always
    if sent and wide:
        res["checks"]["client_pulses" + (":ok" if matched == len(sent) else ":WRONG")] += 1
    if ticks_log:
        res["checks"]["back_pulses" + (":ok" if ticks_dec == ticks_log else ":WRONG")] += 1
    res["unpaired_log_records"] = [(why, r["role"], r["ev"], r["id"], r["pid"], r["tid"]) for why, r in unpaired]
    res["checks"]["log_records_paired" + (":WRONG" if unpaired else ":ok")] += 1
    res["problems"] = dict(res["problems"])
    res["checks"] = dict(sorted(res["checks"].items()))
    failed = [k for k in res["checks"] if k.endswith((":WRONG", ":missing"))]
    res["ok"] = bool(res["requests"]) and res["matched"] == res["requests"] and not failed
    res["failed_checks"] = failed
    le = sorted(res.pop("latency_err_us"))
    if le:
        res["log_minus_trace_latency_us"] = {"min": round(le[0], 2), "median": round(le[len(le) // 2], 2),
                                             "max": round(le[-1], 2)}
    return res, gt, tx_for_gt
