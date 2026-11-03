# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Self-contained reader for QNX kernel event trace files (.kev).

File layout (tracelogger, TRACE_VER 2.01, as observed on QNX SDP 8.0):
  text header   "TRACE_HEADER_BEGIN::" then "TRACE_<KEY>::<value>" pairs up to
                "TRACE_HEADER_END::"
  system page   TRACE_SYSPAGE_LEN bytes, copied from the target
  event slots   16-byte traceevent_t records: header, data[0] (timestamp LSB),
                data[1], data[2]. Slots arrive in kernel-buffer order: one
                buffer belongs to one CPU and is written to the file whole;
                buffers from different CPUs follow each other as they fill.

Header word (see <sys/trace.h>): bits 31-30 slot structure (simple, combine
begin, continuation, end), bits 29-24 CPU, bit 16 "injected" (state emitted at
trace start), bits 14-10 internal class, bits 9-0 event code.

Each buffer starts with CONTROL/TIME (full 64-bit clock) and CONTROL/BUFFER
(per-CPU sequence number, number of slots in the buffer including these two).
The kernel also emits TIME whenever the 32-bit clock LSB rolls over.

Timestamps: TIME events are authoritative anchors. Every other slot is placed
in the 2^32-cycle window that starts TOLERANCE cycles before the latest anchor
of its CPU, which resolves slots that were stamped just before an anchor but
written after it (interlacing) without ever overriding an anchor. Combine
continuations take the full timestamp of their begin slot.

Loss: per-CPU buffer sequence numbers are checked in file order (gaps, wrap,
reset), every buffer's announced slot count is checked against the slots that
follow it, CONTROL/DROPPED is counted, and a file that ends inside a slot or
buffer is flagged. Each loss becomes a Loss record with the time span it
covers on its CPU, and any combine event still being assembled on that CPU is
closed as partial, so slots from both sides of a gap are never joined.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

# internal classes (_TRACE_*_C >> 10)
C_EMPTY, C_CONTROL, C_KERCALL, C_INT, C_PRTH, C_SYSTEM, C_USER, C_COMM, C_QUIP, C_SEC, C_QVM, C_PMU = range(12)
CLASS_NAMES = {C_EMPTY: "EMPTY", C_CONTROL: "CONTROL", C_KERCALL: "KERCALL", C_INT: "INT", C_PRTH: "PR_TH",
               C_SYSTEM: "SYSTEM", C_USER: "USER", C_COMM: "COMM", C_QUIP: "QUIP", C_SEC: "SEC", C_QVM: "QVM",
               C_PMU: "PMU"}

S_SIMPLE, S_BEGIN, S_CONT, S_END = 0, 1, 2, 3

CTRL_TIME, CTRL_BUFFER, CTRL_BUFFER_END, CTRL_DROPPED = 1, 2, 4, 8
INJECTED_FLAG = 0x00010000
_SLOT = struct.Struct("<4I")
_M32 = 0xFFFFFFFF
# How far before the preceding TIME anchor of its CPU a slot's timestamp may lie.
# Interlacing delays are microseconds; 2^28 cycles is about 0.1 s at 2.7 GHz.
TOLERANCE = 1 << 28


@dataclass(slots=True)
class Event:
    ts: int            # 64-bit ClockCycles() value
    cpu: int
    cls: int           # internal class
    code: int          # 10-bit event code
    words: tuple       # payload as 32-bit words (data[1], data[2] of every slot)
    seq: int           # file index of the first slot
    injected: bool = False
    partial: bool = False   # combine event with missing slots


@dataclass
class Loss:
    """Events missing on one CPU between t0 and t1 (cycles)."""
    cpu: int
    t0: int | None      # latest timestamp seen on this CPU before the loss (None: before its first event)
    t1: int | None      # first timestamp after the loss (None: to the end of the trace)
    kind: str           # "buffers", "sequence reset", "dropped", "short buffer", "truncated file"
    count: int | None   # buffers, slots or events lost; None if unknown
    detail: str = ""
    index: int = 0      # file slot index where the loss was detected


@dataclass
class KevStats:
    slots: int = 0
    events: int = 0
    combine_events: int = 0
    partial_combines: int = 0
    orphan_slots: int = 0          # continuation/end slots without a (valid) begin
    stale_combines: int = 0        # begins still waiting for their end long after they started
    buffers: dict = field(default_factory=dict)        # cpu -> buffers seen
    lost_buffers: dict = field(default_factory=dict)   # cpu -> buffers missing from the sequence
    sequence_resets: int = 0
    short_buffers: int = 0         # buffers with fewer slots than announced
    long_buffers: int = 0          # buffers with more slots than announced (counted only)
    dropped_events: int = 0        # from CONTROL/DROPPED events
    trailing_bytes: int = 0        # bytes after the last whole slot
    rollovers: int = 0
    ts_adjusted: int = 0           # slots placed in the epoch before or after their anchor's
    unanchored_slots: int = 0      # slots seen before any TIME event of their CPU
    per_cpu_regressions: int = 0   # events earlier than their predecessor on the same CPU (interlacing)
    max_regression_cycles: int = 0
    bad_cpu: int = 0


class KevError(Exception):
    pass


class KevFile:
    def __init__(self, path: str, data: bytes | None = None):
        self.path = path
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        self.data = data
        self.attrs, self.syspage, self.events_offset = self._parse_header(self.data)
        if self.attrs.get("LITTLE_ENDIAN", "TRUE") != "TRUE":
            raise KevError("only little-endian traces are supported")
        if not self.attrs.get("ENCODING", "16 byte events").startswith("16 byte"):
            raise KevError("unsupported encoding %r" % self.attrs.get("ENCODING"))
        self.cycles_per_sec = int(self.attrs.get("CYCLES_PER_SEC", "0") or 0)
        if self.cycles_per_sec <= 0:
            raise KevError("header has no TRACE_CYCLES_PER_SEC")
        self.ncpu = int(self.attrs.get("CPU_NUM", "1"))
        self.version = (int(self.attrs.get("VER_MAJOR", "0")), int(self.attrs.get("VER_MINOR", "0")))
        self.stats = KevStats()
        self._losses: list[Loss] = []
        self._events = None

    # ---- header -----------------------------------------------------------
    @staticmethod
    def _parse_header(data: bytes):
        begin = b"TRACE_HEADER_BEGIN::"
        end_tag = b"TRACE_HEADER_END::"
        if not data.startswith(begin):
            raise KevError("not a .kev file (missing TRACE_HEADER_BEGIN)")
        end = data.find(end_tag)
        if end < 0:
            raise KevError("truncated header")
        text = data[len(begin):end].decode("latin-1")
        attrs = {}
        for m in re.finditer(r"TRACE_([A-Z0-9_]+)::(.*?)(?=TRACE_[A-Z0-9_]+::|$)", text, re.S):
            attrs[m.group(1)] = m.group(2)
        off = end + len(end_tag)
        splen = int(attrs.get("SYSPAGE_LEN", "0") or 0)
        syspage = data[off:off + splen]
        return attrs, syspage, off + splen

    def cycles_to_ns(self, cyc: int) -> int:
        return cyc * 1_000_000_000 // self.cycles_per_sec

    # ---- slots --------------------------------------------------------------
    def slots(self):
        """Yield (index, header, d0, d1, d2) for every whole 16-byte slot."""
        body = memoryview(self.data)[self.events_offset:]
        n = len(body) // 16
        self.stats.slots = n
        self.stats.trailing_bytes = len(body) - n * 16
        for i, (h, d0, d1, d2) in enumerate(_SLOT.iter_unpack(body[:n * 16])):
            yield i, h, d0, d1, d2

    @property
    def losses(self) -> list[Loss]:
        self.events()
        return self._losses

    # ---- events -------------------------------------------------------------
    def events(self):
        if self._events is None:
            self._events = self._build()
        return self._events

    def _build(self):
        st = self.stats
        anchor = {}      # cpu -> full timestamp of the latest TIME event on that cpu
        last_any = None  # latest anchor of any cpu, for slots before their own cpu's first TIME
        latest = {}      # cpu -> latest timestamp seen on that cpu
        seqs = {}        # cpu -> last BUFFER sequence number
        pending = {}     # (cpu, cls, code, lsb) -> [first index, ts, words, injected]
        open_loss = {}   # cpu -> Loss whose end is the next TIME anchor of that cpu
        cur = None       # [cpu, first slot index, announced slots] of the buffer being read
        out = []
        prev_time = None  # (cpu, index, latest timestamp of that cpu before it) if the previous slot was a TIME

        def close_partial(key):
            p = pending.pop(key)
            out.append(Event(p[1], key[0], key[1], key[2], tuple(p[2]), p[0], p[3], True))
            st.partial_combines += 1

        def lose(cpu, t0, kind, count, detail, idx, t1):
            for key in [k for k in pending if k[0] == cpu]:
                close_partial(key)
            loss = Loss(cpu, t0, t1, kind, count, detail, idx)
            self._losses.append(loss)
            return loss

        def close_buffer(end_index, t0, t1):
            if cur is None:
                return
            cpu, start, want = cur
            have = end_index - start
            if have < want:
                st.short_buffers += 1
                kind = "truncated file" if end_index >= st.slots else "short buffer"
                loss = lose(cpu, t0, kind, want - have, "buffer announced %d slots, file has %d" % (want, have),
                            end_index, t1)
                if t1 is None:
                    open_loss[cpu] = loss
            elif have > want:
                st.long_buffers += 1

        def place(cpu, lsb):
            a = anchor.get(cpu)
            if a is None:
                st.unanchored_slots += 1
                a = last_any
                if a is None:
                    return lsb
            base = a - TOLERANCE
            ts = base + ((lsb - base) & _M32)
            if ts >> 32 != a >> 32:
                st.ts_adjusted += 1
            return ts

        for i, h, d0, d1, d2 in self.slots():
            struct_ = h >> 30
            cpu = (h >> 24) & 0x3F
            cls = (h >> 10) & 0x1F
            code = h & 0x3FF
            injected = bool(h & INJECTED_FLAG)
            if cpu >= max(self.ncpu, 1):
                st.bad_cpu += 1
            is_time = struct_ == S_SIMPLE and cls == C_CONTROL and code == CTRL_TIME
            if is_time:
                ts = (d1 << 32) | d0
                a = anchor.get(cpu)
                if a is not None and a >> 32 != d1:
                    st.rollovers += 1
                anchor[cpu] = last_any = ts
                for key in [k for k, p in pending.items() if k[0] == cpu and p[1] < ts - TOLERANCE]:
                    close_partial(key)
                    st.stale_combines += 1
                if cpu in open_loss:
                    open_loss.pop(cpu).t1 = ts
            elif struct_ in (S_SIMPLE, S_BEGIN):
                ts = place(cpu, d0)
            else:
                ts = None
            prev_latest = latest.get(cpu)
            if ts is not None:
                prev = prev_latest
                if prev is not None and ts < prev:
                    st.per_cpu_regressions += 1
                    st.max_regression_cycles = max(st.max_regression_cycles, prev - ts)

            if struct_ == S_SIMPLE:
                ev = Event(ts, cpu, cls, code, (d1, d2), i, injected)
                out.append(ev)
                if cls == C_CONTROL and code == CTRL_BUFFER:
                    # the TIME slot just before a BUFFER slot opens the new buffer; losses end at it and
                    # start at the latest timestamp the cpu had before it
                    same = prev_time is not None and prev_time[0] == cpu and prev_time[1] == i - 1
                    start = i - 1 if same else i
                    t_new = anchor[cpu] if same else ts
                    t_before = prev_time[2] if same else latest.get(cpu)
                    if cur is not None:
                        old = cur[0]
                        close_buffer(start, t_before if old == cpu else latest.get(old), t_new if old == cpu else None)
                    cur = [cpu, start, d2]
                    st.buffers[cpu] = st.buffers.get(cpu, 0) + 1
                    p = seqs.get(cpu)
                    if p is None:
                        if d1 > 1:
                            lose(cpu, None, "buffers", d1 - 1,
                                 "sequence 1..%d before the first buffer in the file" % (d1 - 1), i, t_new)
                            st.lost_buffers[cpu] = st.lost_buffers.get(cpu, 0) + d1 - 1
                    else:
                        gap = (d1 - p - 1) & _M32
                        if gap and gap < 1 << 31:
                            lose(cpu, t_before, "buffers", gap, "sequence %d..%d" % ((p + 1) & _M32, (d1 - 1) & _M32),
                                 i, t_new)
                            st.lost_buffers[cpu] = st.lost_buffers.get(cpu, 0) + gap
                        elif gap:
                            st.sequence_resets += 1
                            lose(cpu, t_before, "sequence reset", None, "sequence %d after %d" % (d1, p), i, t_new)
                    seqs[cpu] = d1
                elif cls == C_CONTROL and code == CTRL_DROPPED:
                    st.dropped_events += d1
                    lose(cpu, latest.get(cpu), "dropped", d1, "kernel dropped %d events" % d1, i, ts)
            else:
                key = (cpu, cls, code, d0)
                if struct_ == S_BEGIN:
                    if key in pending:   # previous one never ended
                        close_partial(key)
                    pending[key] = [i, ts, [d1, d2], injected]
                else:
                    p = pending.get(key)
                    if p is None:
                        st.orphan_slots += 1
                    else:
                        p[2].extend((d1, d2))
                        if struct_ == S_END:
                            del pending[key]
                            out.append(Event(p[1], cpu, cls, code, tuple(p[2]), p[0], p[3]))
                            st.combine_events += 1
            if ts is not None and (cpu not in latest or ts > latest[cpu]):
                latest[cpu] = ts
            prev_time = (cpu, i, prev_latest) if is_time else None

        if cur is not None:
            close_buffer(st.slots, latest.get(cur[0]), None)
        if st.trailing_bytes and not any(l.kind == "truncated file" for l in self._losses):
            c = cur[0] if cur else 0
            lose(c, latest.get(c), "truncated file", None, "%d bytes after the last whole slot" % st.trailing_bytes,
                 st.slots, None)
        for key in list(pending):
            close_partial(key)
        out.sort(key=lambda e: (e.ts, e.seq))
        st.events = len(out)
        return out
