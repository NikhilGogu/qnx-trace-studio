# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Build synthetic .kev files slot by slot, for unit tests.

Slots go into per-CPU kernel buffers the way tracelogger writes them: each
buffer starts with CONTROL/TIME and CONTROL/BUFFER (sequence number, slot
count), and an event on another CPU starts a new buffer. Payloads of more than
two words become combine events (begin, continuations, end), as in wide mode.
"""
import struct

M32 = 0xFFFFFFFF
C_CONTROL, C_KERCALL, C_INT, C_PRTH, C_SYSTEM, C_COMM = 1, 2, 3, 4, 5, 7
S_SIMPLE, S_BEGIN, S_CONT, S_END = 0, 1, 2, 3
RUNNING, READY, SEND, RECEIVE, REPLY, DEAD, NANOSLEEP = 1, 2, 4, 5, 6, 0, 12
K_SENDV, K_ERROR, K_RECEIVEV, K_REPLYV, K_PULSE, K_THREAD_CTL, K_CHANNEL_CREATE = 11, 13, 14, 15, 20, 52, 35
SMSG, SPULSE, RMSG, RPULSE, REPLYMSG, ERRMSG = 0, 1, 2, 3, 10, 11
PROCCREATE_NAME, THREAD_NAME = 192, 320
EXIT, INT = 0x80, 0x100


def words_of(text):
    raw = text.encode() + b"\0"
    raw += b"\0" * (-len(raw) % 4)
    return [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]


class Gen:
    def __init__(self, ncpu=2, cps=1_000_000_000):
        self.ncpu, self.cps = ncpu, cps
        self.buffers = []          # [cpu, seq, count or None, [slots]]
        self.seq = {}
        self.cur = None

    # ---- buffers and raw slots ------------------------------------------------
    def buffer(self, cpu, ts, seq=None, count=None):
        s = self.seq.get(cpu, 0) + 1 if seq is None else seq
        self.seq[cpu] = s
        b = [cpu, s & M32, count, []]
        self.buffers.append(b)
        self.cur = b
        self.raw(S_SIMPLE, cpu, C_CONTROL, 1, ts & M32, ts >> 32, ts & M32)   # TIME
        self.raw(S_SIMPLE, cpu, C_CONTROL, 2, ts & M32, s & M32, 0)            # BUFFER, count patched later
        return b

    def raw(self, st, cpu, cls, code, d0, d1, d2, injected=False):
        if self.cur is None or self.cur[0] != cpu:
            raise ValueError("no open buffer for cpu %d" % cpu)
        h = (st << 30) | (cpu << 24) | (cls << 10) | code | (0x10000 if injected else 0)
        self.cur[3].append([h, d0 & M32, d1 & M32, d2 & M32])

    def on(self, cpu, ts):
        if self.cur is None or self.cur[0] != cpu:
            self.buffer(cpu, ts)

    def time(self, cpu, ts):
        self.on(cpu, ts)
        self.raw(S_SIMPLE, cpu, C_CONTROL, 1, ts & M32, ts >> 32, ts & M32)

    def event(self, cpu, ts, cls, code, words, injected=False, lsb=None):
        self.on(cpu, ts)
        w = list(words)
        l = ts & M32 if lsb is None else lsb
        if len(w) <= 2:
            w += [0] * (2 - len(w))
            self.raw(S_SIMPLE, cpu, cls, code, l, w[0], w[1], injected)
            return
        w += [0] * (len(w) % 2)
        pairs = [w[i:i + 2] for i in range(0, len(w), 2)]
        for i, (a, b) in enumerate(pairs):
            st = S_BEGIN if i == 0 else (S_END if i == len(pairs) - 1 else S_CONT)
            self.raw(st, cpu, cls, code, l, a, b, injected)

    def bytes(self, cut=None):
        attrs = [("DATE", "synthetic"), ("VER_MAJOR", "2"), ("VER_MINOR", "01"), ("LITTLE_ENDIAN", "TRUE"),
                 ("ENCODING", "16 byte events"), ("CYCLES_PER_SEC", str(self.cps)), ("CPU_NUM", str(self.ncpu)),
                 ("SYSNAME", "QNX"), ("SYSPAGE_LEN", "0")]
        head = b"TRACE_HEADER_BEGIN::" + b"".join(b"TRACE_%s::%s" % (k.encode(), v.encode()) for k, v in attrs)
        head += b"TRACE_HEADER_END::"
        body = bytearray()
        for cpu, seq, count, slots in self.buffers:
            slots[1][3] = len(slots) if count is None else count
            for s in slots:
                body += struct.pack("<4I", *s)
        data = head + bytes(body)
        return data if cut is None else data[:len(head) + cut]

    # ---- typed events ---------------------------------------------------------
    def state(self, cpu, ts, code, key, prio=None, injected=False):
        w = [key[0], key[1]] if prio is None else [key[0], key[1], prio, 2]
        self.event(cpu, ts, C_PRTH, code, w, injected)

    def kcall(self, cpu, ts, num, words, flags=0):
        self.event(cpu, ts, C_KERCALL, num | flags, words)

    def comm(self, cpu, ts, code, w0, w1):
        self.event(cpu, ts, C_COMM, code, [w0, w1])

    def names(self, cpu, ts, procs):
        """procs: {pid: (path, {tid: name})}"""
        for pid, (path, threads) in procs.items():
            self.event(cpu, ts, C_PRTH, PROCCREATE_NAME, [1, pid] + words_of(path))
            for tid, name in threads.items():
                self.event(cpu, ts, C_PRTH, THREAD_NAME, [pid, tid] + words_of(name))

    def runmask(self, cpu, ts, mask):
        self.kcall(cpu, ts, K_THREAD_CTL, [4, mask])
        self.kcall(cpu, ts + 1, K_THREAD_CTL, [0, 0], EXIT)

    # ---- one message exchange on one CPU, the way QNX 8.0 traces it ------------
    def send(self, cpu, ts, client, spid, rcvid, msg=(0x7101, 1, 0), prio=None, block=REPLY, wide=True):
        self.kcall(cpu, ts, K_SENDV, [0x40000001, 1, 1] + list(msg) if wide else [0x40000001, msg[0]])
        self.comm(cpu, ts + 10, SMSG, rcvid, spid)
        self.state(cpu, ts + 20, block, client, prio)

    def receive(self, cpu, ts, server, client, rcvid, prio=None, msg=(0x7101, 1, 0), chid=1, wide=True,
                info_client=None):
        ic = info_client or client
        self.state(cpu, ts, RUNNING, server, prio)
        self.comm(cpu, ts + 10, RMSG, rcvid, server[0])
        if wide:
            info = [0, 0, ic[0], ic[1], chid, 0x10001, 0x40000001, 16, 16, 16, prio or 10, 0]
            self.kcall(cpu, ts + 20, K_RECEIVEV, [rcvid] + list(msg) + info, EXIT)
        else:
            self.kcall(cpu, ts + 20, K_RECEIVEV, [rcvid, msg[0]], EXIT)

    def reply(self, cpu, ts, server, client, rcvid, client_prio=None, error=None):
        if error is None:
            self.kcall(cpu, ts, K_REPLYV, [rcvid, 1, 0, 0, 0, 0])
            self.comm(cpu, ts + 10, REPLYMSG, client[1], client[0])
        else:
            self.kcall(cpu, ts, K_ERROR, [rcvid, error])
            self.comm(cpu, ts + 10, ERRMSG, client[1], client[0])
        self.state(cpu, ts + 20, READY, client, client_prio)
        self.kcall(cpu, ts + 30, K_REPLYV if error is None else K_ERROR, [0, 0], EXIT)

    def wait_receive(self, cpu, ts, server, prio=None):
        self.kcall(cpu, ts, K_RECEIVEV, [1, 1])
        self.state(cpu, ts + 10, RECEIVE, server, prio)

    def send_exit(self, cpu, ts, client, prio=None, status=0):
        self.state(cpu, ts, RUNNING, client, prio)
        self.kcall(cpu, ts + 10, K_SENDV, [status, 0, 0, 0], EXIT)
