#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Event-by-event check of the trace-studio .kev parser against QNX traceprinter.

    traceprinter -f trace.kev -o trace.txt
    python3 tools/compare_traceprinter.py trace.kev trace.txt

Compares timestamp (64-bit cycles), CPU, class and event label of every event,
the per-CPU event order, and every decoded argument that both sides print.
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.events import describe  # noqa: E402

LINE = re.compile(r"^t:0x([0-9a-f]+) CPU:(\d+) (\S+?)\s*:(\S*)\s*(.*)$")
ARG = re.compile(r"([A-Za-z_][\w\[\]\->().]*?):(\"[^\"]*\" \([^)]*\)|\S+)")
KEYMAP = {"msg[0]": "msg", "rmsg[0]": "rmsg", "smsg[0]": "smsg", "lsb(offset)": "lsb"}


def num(v):
    m = re.match(r"^(-?)(0x[0-9a-fA-F]+|\d+)", v)
    if not m:
        return v
    x = int(m.group(2), 16 if m.group(2).startswith("0x") else 10)
    return -x if m.group(1) else x


def same(a, b):
    """Equal, or equal as 32/64-bit two's complement (traceprinter prints some fields signed)."""
    if a == b:
        return True
    if isinstance(a, int) and isinstance(b, int):
        return (a - b) % (1 << 32) == 0 if max(abs(a), abs(b)) < (1 << 32) else (a - b) % (1 << 64) == 0
    return False


def parse_tp(path):
    evs, cur = [], None
    for ln in open(path, errors="replace"):
        m = LINE.match(ln)
        if m:
            cur = [int(m.group(1), 16), int(m.group(2)), m.group(3), m.group(4), {}]
            evs.append(cur)
            rest = m.group(5)
        elif cur is not None and ln.startswith(" "):
            rest = ln
        else:
            continue
        for k, v in ARG.findall(rest):
            k = k.replace("info->", "")
            k = KEYMAP.get(k, k)
            if v.startswith('"'):
                words = [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]+", v)]
                for j, x in enumerate(words):
                    cur[4]["%s%d" % (k, j)] = x
            else:
                cur[4][k] = num(v)
    return evs


def main(kev_path, tp_path):
    kf = KevFile(kev_path)
    mine = []
    for e in kf.events():
        cl, name, args = describe(e)
        mine.append([e.ts, e.cpu, cl, name, args])
    tp = parse_tp(tp_path)
    def key(r):
        ev = r[3]
        if r[2] in ("KER_CALL", "KER_EXIT", "INT_CALL"):
            ev = ev.rsplit("/", 1)[-1].lstrip("0")
        elif r[2] == "SYSTEM":
            ev = ""
        return (r[0], r[1], r[2], ev)
    cm, ct = collections.Counter(map(key, mine)), collections.Counter(map(key, tp))
    only_mine, only_tp = cm - ct, ct - cm
    print(f"file: {os.path.basename(kev_path)}  slots: {kf.stats.slots}  mode args: {kf.attrs.get('TRACELOGGER_ARGS')}")
    print(f"events: trace-studio {len(mine)}, traceprinter {len(tp)}, "
          f"identical (ts, cpu, class, event): {sum((cm & ct).values())}")
    print(f"only in trace-studio: {sum(only_mine.values())}, only in traceprinter: {sum(only_tp.values())}")
    for k, n in list(only_mine.items())[:5]:
        print("  mine-only", hex(k[0]), k[1:], n)
    for k, n in list(only_tp.items())[:5]:
        print("  tp-only  ", hex(k[0]), k[1:], n)
    # per-CPU order
    order_bad = 0
    for cpu in sorted({r[1] for r in tp}):
        a = [key(r) for r in mine if r[1] == cpu]
        b = [key(r) for r in tp if r[1] == cpu]
        order_bad += sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    print(f"per-CPU order differences: {order_bad}")
    # arguments
    by_key = collections.defaultdict(list)
    for r in tp:
        by_key[key(r)].append(r[4])
    checked = bad = 0
    per_field = collections.Counter()
    labels = set()
    examples = []
    for r in mine:
        lst = by_key.get(key(r))
        if not lst:
            continue
        targs = lst.pop(0)
        for k, v in r[4].items():
            if k not in targs or isinstance(v, str) and k != "name":
                continue
            tv = targs[k]
            ok = same(tv, v)
            if k == "name":
                ok = str(tv) == v.split()[0] if v else True
            checked += 1
            per_field[(r[2], r[3].split("/")[0], k)] += 1
            labels.add(r[3])
            if not ok:
                bad += 1
                if len(examples) < 8:
                    examples.append((hex(r[0]), r[2], r[3], k, v, tv))
    print(f"argument values compared: {checked}, mismatches: {bad}, distinct (class, event, field): {len(per_field)}, "
          f"event types with args: {len(labels)}")
    for ex in examples:
        print("  mismatch", ex)
    st = kf.stats
    print(f"combine events: {st.combine_events}, partial: {st.partial_combines}, orphan slots: {st.orphan_slots}, "
          f"rollovers: {st.rollovers}, ts adjusted: {st.ts_adjusted}, per-CPU regressions: {st.per_cpu_regressions}, "
          f"buffers: {st.buffers}, lost: {st.lost_buffers}, dropped events: {st.dropped_events}")
    return 0 if not only_mine and not only_tp and not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
