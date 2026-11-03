# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Self-contained reader for QNX kernel event trace files (.kev).

File layout (tracelogger, TRACE_VER 2.01, as observed on QNX SDP 8.0):
  text header   "TRACE_HEADER_BEGIN::" then "TRACE_<KEY>::<value>" pairs up to
                "TRACE_HEADER_END::"
  system page   TRACE_SYSPAGE_LEN bytes, copied from the target
  event slots   16-byte traceevent_t records: header, data[0] (timestamp LSB),
                data[1], data[2]. Slots arrive in kernel-buffer order: one
                buffer (~1011 slots) belongs to one CPU, buffers from different
                CPUs follow each other as they fill.

Header word (see <sys/trace.h>): bits 31-30 slot structure (simple, combine
begin, continuation, end), bits 29-24 CPU, bit 16 "injected" (state emitted at
trace start), bits 14-10 internal class, bits 9-0 event code.

This module reassembles combine events (all slots of one event carry the same
CPU, class, code and timestamp LSB; other events may interlace), extends the
32-bit timestamps per CPU using CONTROL/TIME events plus a nearest-neighbour
rule for interlaced slots around a rollover, tracks per-CPU buffer sequence
numbers to report lost buffers, and returns events merged in time order.
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
class KevStats:
    slots: int = 0
    events: int = 0
    combine_events: int = 0
    partial_combines: int = 0
    orphan_slots: int = 0          # continuation/end slots without a begin
    buffers: dict = field(default_factory=dict)        # cpu -> buffers seen
    lost_buffers: dict = field(default_factory=dict)   # cpu -> missing sequence numbers
    dropped_events: int = 0        # from CONTROL/DROPPED events
    rollovers: int = 0
    ts_adjusted: int = 0           # slots whose MSB was taken from a neighbour (interlaced at a rollover)
    per_cpu_regressions: int = 0   # events earlier than their predecessor on the same CPU (interlacing)
    bad_cpu: int = 0


class KevError(Exception):
    pass


class KevFile:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.attrs, self.syspage, self.events_offset = self._parse_header(self.data)
        if self.attrs.get("LITTLE_ENDIAN", "TRUE") != "TRUE":
            raise KevError("only little-endian traces are supported")
        if not self.attrs.get("ENCODING", "16 byte events").startswith("16 byte"):
            raise KevError("unsupported encoding %r" % self.attrs.get("ENCODING"))
        self.cycles_per_sec = int(self.attrs.get("CYCLES_PER_SEC", "0"))
        self.ncpu = int(self.attrs.get("CPU_NUM", "1"))
        self.version = (int(self.attrs.get("VER_MAJOR", "0")), int(self.attrs.get("VER_MINOR", "0")))
        self.stats = KevStats()
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
        splen = int(attrs.get("SYSPAGE_LEN", "0"))
        syspage = data[off:off + splen]
        return attrs, syspage, off + splen

    def cycles_to_ns(self, cyc: int) -> int:
        return cyc * 1_000_000_000 // self.cycles_per_sec

    # ---- slots --------------------------------------------------------------
    def slots(self):
        """Yield (index, header, d0, d1, d2) for every 16-byte slot."""
        body = memoryview(self.data)[self.events_offset:]
        n = len(body) // 16
        self.stats.slots = n
        for i, (h, d0, d1, d2) in enumerate(_SLOT.iter_unpack(body[:n * 16])):
            yield i, h, d0, d1, d2

    # ---- events -------------------------------------------------------------
    def events(self):
        if self._events is None:
            self._events = self._build()
        return self._events

    def _build(self):
        st = self.stats
        msb = {}       # cpu -> current MSB
        last = {}      # cpu -> last extended timestamp
        lastseq = {}   # cpu -> last buffer sequence number
        pending = {}   # (cpu, cls, code, lsb) -> [first_index, ts, words, injected]
        out = []

        def extend(cpu, lsb):
            m = msb.get(cpu)
            if m is None:
                return lsb
            prev = last.get(cpu)
            ts = (m << 32) | lsb
            if prev is not None and abs(ts - prev) > (1 << 31):
                best = ts
                for mm in (m - 1, m + 1):
                    if mm < 0:
                        continue
                    cand = (mm << 32) | lsb
                    if abs(cand - prev) < abs(best - prev):
                        best = cand
                if best != ts:
                    st.ts_adjusted += 1
                ts = best
            return ts

        for i, h, d0, d1, d2 in self.slots():
            struct_ = h >> 30
            cpu = (h >> 24) & 0x3F
            cls = (h >> 10) & 0x1F
            code = h & 0x3FF
            injected = bool(h & INJECTED_FLAG)
            if cpu >= max(self.ncpu, 1):
                st.bad_cpu += 1
            if cls == C_CONTROL and code == CTRL_TIME and struct_ == S_SIMPLE:
                if cpu in msb and d1 != msb[cpu]:
                    st.rollovers += 1
                msb[cpu] = d1
            ts = extend(cpu, d0)
            if struct_ == S_SIMPLE:
                ev = Event(ts, cpu, cls, code, (d1, d2), i, injected)
                out.append(ev)
                self._control(ev, lastseq)
            else:
                key = (cpu, cls, code, d0)
                if struct_ == S_BEGIN:
                    if key in pending:   # previous one never ended
                        p = pending.pop(key)
                        out.append(Event(p[1], cpu, cls, code, tuple(p[2]), p[0], p[3], True))
                        st.partial_combines += 1
                    pending[key] = [i, ts, [d1, d2], injected]
                else:
                    p = pending.get(key)
                    if p is None:
                        st.orphan_slots += 1
                        continue
                    p[2].extend((d1, d2))
                    if struct_ == S_END:
                        del pending[key]
                        out.append(Event(p[1], cpu, cls, code, tuple(p[2]), p[0], p[3]))
                        st.combine_events += 1
            if cpu in msb:
                prev = last.get(cpu)
                if prev is not None and ts < prev:
                    st.per_cpu_regressions += 1
                if prev is None or ts > prev:
                    last[cpu] = ts
        for key, p in pending.items():
            out.append(Event(p[1], key[0], key[1], key[2], tuple(p[2]), p[0], p[3], True))
            st.partial_combines += 1
        out.sort(key=lambda e: (e.ts, e.seq))
        st.events = len(out)
        return out

    def _control(self, ev, lastseq):
        if ev.cls != C_CONTROL:
            return
        st = self.stats
        if ev.code == CTRL_BUFFER:
            seq = ev.words[0]
            st.buffers[ev.cpu] = st.buffers.get(ev.cpu, 0) + 1
            prev = lastseq.get(ev.cpu)
            if prev is not None and seq != prev + 1:
                st.lost_buffers.setdefault(ev.cpu, []).extend(range(prev + 1, seq))
            lastseq[ev.cpu] = seq
        elif ev.code == CTRL_DROPPED:
            st.dropped_events += ev.words[0]
