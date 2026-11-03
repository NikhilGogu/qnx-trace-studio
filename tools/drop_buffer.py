#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Fault injection: write a copy of a .kev with kernel buffers removed, as if
tracelogger had lost them, to test that the decoder reports the loss and does
not invent message-passing links across the gap.

    python3 tools/drop_buffer.py in.kev out.kev N [N...]   (N = buffer ordinal in file order)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tracestudio.kev import C_CONTROL, CTRL_BUFFER, CTRL_TIME, KevFile  # noqa: E402


def buffers(kf):
    """[(first slot, end slot, cpu, sequence)] of every kernel buffer in file order."""
    slots = list(kf.slots())
    starts = []
    for i, h, d0, d1, d2 in slots:
        if (h >> 10) & 0x1F == C_CONTROL and h & 0x3FF == CTRL_BUFFER and h >> 30 == 0:
            prev = slots[i - 1][1] if i else 0
            time_first = i and (prev >> 10) & 0x1F == C_CONTROL and prev & 0x3FF == CTRL_TIME
            starts.append((i - 1 if time_first else i, (h >> 24) & 0x3F, d1))
    ends = [s for s, _, _ in starts[1:]] + [len(slots)]
    return [(s, e, cpu, seq) for (s, cpu, seq), e in zip(starts, ends)], slots


def drop_buffers(kf, drop):
    bufs, slots = buffers(kf)
    out = bytearray(kf.data[:kf.events_offset])
    removed = 0
    for n, (a, b, _, _) in enumerate(bufs):
        if n in drop:
            removed += b - a
            continue
        for _, h, d0, d1, d2 in slots[a:b]:
            out += struct.pack("<4I", h, d0, d1, d2)
    return bytes(out), len(bufs), removed


if __name__ == "__main__":
    src, dst, drop = sys.argv[1], sys.argv[2], {int(x) for x in sys.argv[3:]}
    data, n, removed = drop_buffers(KevFile(src), drop)
    with open(dst, "wb") as f:
        f.write(data)
    print("buffers %d, dropped %s, slots removed %d" % (n, sorted(drop), removed))
