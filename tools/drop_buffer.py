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

src, dst, drop = sys.argv[1], sys.argv[2], {int(x) for x in sys.argv[3:]}
kf = KevFile(src)
slots = list(kf.slots())
starts = []
for i, h, d0, d1, d2 in slots:
    cls, code = (h >> 10) & 0x1F, h & 0x3FF
    if cls == C_CONTROL and code == CTRL_BUFFER and h >> 30 == 0:
        s = i - 1 if i and ((slots[i - 1][1] >> 10) & 0x1F, slots[i - 1][1] & 0x3FF) == (C_CONTROL, CTRL_TIME) else i
        starts.append(s)
bounds = list(zip(starts, starts[1:] + [len(slots)]))
keep = bytearray(kf.data[:kf.events_offset])
removed = 0
for n, (a, b) in enumerate(bounds):
    if n in drop:
        removed += b - a
        continue
    for i, h, d0, d1, d2 in slots[a:b]:
        keep += struct.pack("<4I", h, d0, d1, d2)
open(dst, "wb").write(keep)
print("buffers %d, dropped %s, slots removed %d" % (len(bounds), sorted(drop), removed))
