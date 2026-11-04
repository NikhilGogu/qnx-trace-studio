# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Event naming and argument decoding for QNX 8.0 kernel events.

Payload layouts follow the "Current Trace Events and Data" appendix of the
System Analysis Toolkit guide (the "#Args" letters: H hex, D signed decimal,
E signed 64-bit, X hex 64-bit). Fast-mode events carry a two-word summary in
one slot; wide-mode events carry the full argument list in a combine event.
The layout is chosen from the payload length, so traces with a per-class mix
of fast and wide events decode correctly. A kernel call that fails exits with
its return code (-1) and errno as its arguments, and an interrupted call
(INT_CALL) has no arguments; traceprinter prints both that way.
"""
from __future__ import annotations

from .kev import (C_COMM, C_CONTROL, C_INT, C_KERCALL, C_PRTH, C_SYSTEM, C_USER, CLASS_NAMES)
from .qnx_tables import IO_CONNECT_SUBTYPES, KERCALLS, MSG_TYPES, STATES

KER_EXIT_BIT = 0x80
KER_INT_BIT = 0x100       # interrupted kernel call (KERCALLINT): traceprinter labels it INT_CALL; not seen in a capture
KER_64_BIT = 0x200        # 64-bit argument variant
STATE_MAX = 63

PR_PROCCREATE, PR_PROCDESTROY, PR_PROCCREATE_NAME, PR_PROCDESTROY_NAME, PR_THREAD_NAME = 64, 128, 192, 256, 320
PROCESS_EVENTS = {PR_PROCCREATE: "PROCCREATE", PR_PROCDESTROY: "PROCDESTROY", PR_PROCCREATE_NAME: "PROCCREATE_NAME",
                  PR_PROCDESTROY_NAME: "PROCDESTROY_NAME", PR_THREAD_NAME: "PROCTHREAD_NAME"}

COMM_SMSG, COMM_SPULSE, COMM_RMSG, COMM_RPULSE, COMM_SPULSE_EXE, COMM_SPULSE_DIS, COMM_SPULSE_DEA, \
    COMM_SPULSE_UN, COMM_SPULSE_QUN, COMM_SIGNAL, COMM_REPLY, COMM_ERROR = range(12)
COMM_NAMES = {COMM_SMSG: "SND_MESSAGE", COMM_SPULSE: "SND_PULSE", COMM_RMSG: "REC_MESSAGE", COMM_RPULSE: "REC_PULSE",
              COMM_SPULSE_EXE: "SND_PULSE_EXE", COMM_SPULSE_DIS: "SND_PULSE_DIS", COMM_SPULSE_DEA: "SND_PULSE_DEA",
              COMM_SPULSE_UN: "SND_PULSE_UN", COMM_SPULSE_QUN: "SND_PULSE_QUN", COMM_SIGNAL: "SIGNAL",
              COMM_REPLY: "REPLY_MESSAGE", COMM_ERROR: "MSG_ERROR"}

CONTROL_NAMES = {1: "TIME", 2: "BUFFER", 4: "BUFFER_END", 8: "DROPPED"}
INT_DELIVER = 7
SYSTEM_NAMES = {1: "RESERVED", 2: "PATHMGR", 3: "APS_NAME", 4: "APS_BUDGETS", 5: "APS_BNKR", 6: "MMAP", 7: "MUNMAP",
                8: "MAPNAME", 9: "ADDRESS", 10: "FUNC_ENTER", 11: "FUNC_EXIT", 12: "SLOG", 13: "DEFRAG_START",
                14: "RUNSTATE", 15: "POWER", 16: "IPI", 17: "PAGEWAIT", 18: "TIMER", 19: "DEFRAG_END", 20: "PROFILE",
                21: "MAPNAME_64", 22: "APS_PSTATS", 23: "APS_OSTATS", 24: "APS_INFO", 25: "APS_JOIN",
                26: "APS_THREAD", 27: "APS_PROCESS", 28: "SCHED_CONF", 29: "IST_ATTACH", 30: "IST_DETACH",
                31: "PCTRACE", 32: "SEND_IPI"}

# Field kinds: x = u32 (hex), d = s32, q = u64 (lo, hi words), e = s64 (lo, hi words),
# s = NUL-terminated string (rest), _ = skip
# struct _msg_info32/_msg_info64 in <sys/neutrino.h>: scoid and coid _Int32t, lengths _Ssize32t/_Ssize64t,
# priority and flags _Int16t (one trace word each): all signed (a pulse's info is all ones, -1)
_MSGINFO32 = "nd:_ srcnd:_ pid:d tid:d chid:d scoid:d coid:d msglen:d srcmsglen:d dstmsglen:d priority:d flags:d"
_MSGINFO64 = "nd:_ srcnd:_ pid:d tid:d chid:d scoid:d coid:d msglen:e srcmsglen:e dstmsglen:e priority:d flags:d"
# (name, exit, is64) -> (fast, wide)
KER_ARGS = {
    ("MSG_SENDV", 0, 0): ("coid:x msg0:x", "coid:x sparts:d rparts:d msg0:x msg1:x msg2:x"),
    ("MSG_SENDV", 0, 1): ("coid:x msg0:x", "coid:x sparts:e rparts:e msg0:x msg1:x msg2:x"),
    ("MSG_SENDV", 1, 0): ("status:d rmsg0:x", "status:d rmsg0:x rmsg1:x rmsg2:x"),
    ("MSG_SENDV", 1, 1): ("status:e", "status:e rmsg0:x rmsg1:x rmsg2:x"),
    ("MSG_RECEIVEV", 0, 0): ("chid:x rparts:d", "chid:x rparts:d"),
    ("MSG_RECEIVEV", 0, 1): ("chid:x rparts:d", "chid:x rparts:e"),
    ("MSG_RECEIVEV", 1, 0): ("rcvid:x rmsg0:x", "rcvid:x rmsg0:x rmsg1:x rmsg2:x " + _MSGINFO32),
    ("MSG_RECEIVEV", 1, 1): ("rcvid:x rmsg0:x", "rcvid:x rmsg0:x rmsg1:x rmsg2:x " + _MSGINFO64),
    ("MSG_REPLYV", 0, 0): ("rcvid:x status:x", "rcvid:x sparts:d status:x smsg0:x smsg1:x smsg2:x"),
    ("MSG_REPLYV", 0, 1): ("rcvid:x status:x", "rcvid:x sparts:e status:q smsg0:x smsg1:x smsg2:x"),
    ("MSG_REPLYV", 1, 0): ("ret_val:d", "ret_val:d"),
    ("MSG_ERROR", 0, 0): ("rcvid:x err:d", "rcvid:x err:d"),
    ("MSG_ERROR", 1, 0): ("ret_val:d", "ret_val:d"),
    ("MSG_SEND_PULSE", 0, 0): ("coid:x code:x", "coid:x priority:d code:x value:x"),
    ("MSG_SEND_PULSE", 1, 0): ("status:d empty:x", "status:d empty:x"),
    ("MSG_SEND_PULSEPTR", 0, 0): ("coid:x code:x", "coid:x priority:d code:x value:x"),
    ("MSG_SEND_PULSEPTR", 0, 1): ("coid:x code:x", "coid:x priority:d code:x value:q"),
    ("MSG_SEND_PULSEPTR", 1, 0): ("status:d empty:x", "status:d empty:x"),
    ("CHANNEL_CREATE", 0, 0): ("flags:x", "flags:x"),
    ("CHANNEL_CREATE", 1, 0): ("chid:d", "chid:d"),
    ("CONNECT_ATTACH", 0, 0): ("nd:_ pid:d", "nd:_ pid:d chid:d index:x flags:x"),
    ("CONNECT_ATTACH", 1, 0): ("coid:x", "coid:x"),
    ("SCHED_SET", 0, 0): ("pid:d tid:d", "pid:d tid:d policy:d sched_priority:d sched_curpriority:d"),
    ("SCHED_SET", 0, 1): ("pid:d tid:d", "pid:d tid:d policy:d sched_priority:d sched_curpriority:d"),
    ("THREAD_CTL", 0, 0): ("cmd:d data:x", "cmd:d data:x"),
    ("THREAD_CTL", 0, 1): ("cmd:d data:x", "cmd:d data:q"),
    ("THREAD_CTL", 1, 0): ("ret_val:d", "ret_val:d"),
    ("THREAD_CTLEXT", 0, 0): ("pid:d tid:d", "pid:d tid:d cmd:d data:x"),
    ("THREAD_CTLEXT", 0, 1): ("pid:d tid:d", "pid:d tid:d cmd:d data:q"),
    ("THREAD_CTLEXT", 1, 0): ("ret_val:d", "ret_val:d"),
    ("CONNECT_DETACH", 0, 0): ("coid:x", "coid:x"),
    # SyncMutexLock/Unlock: the mutex address and its count and owner words as the kernel read them
    ("SYNC_MUTEX_LOCK", 0, 0): ("sync_p:x owner:d", "sync_p:x count:d owner:d"),
    ("SYNC_MUTEX_LOCK", 0, 1): ("sync_p:q", "sync_p:q count:d owner:d"),
    ("SYNC_MUTEX_LOCK", 1, 0): ("ret_val:d empty:x", "ret_val:d empty:x"),
    ("SYNC_MUTEX_UNLOCK", 0, 0): ("sync_p:x owner:d", "sync_p:x count:d owner:d"),
    ("SYNC_MUTEX_UNLOCK", 0, 1): ("sync_p:q owner:d", "sync_p:q count:d owner:d"),
    ("SYNC_MUTEX_UNLOCK", 1, 0): ("ret_val:d empty:x", "ret_val:d empty:x"),
    ("SYNC_DESTROY", 0, 0): ("sync_p:x owner:d", "sync_p:x count:d owner:d"),
    ("SYNC_DESTROY", 0, 1): ("sync_p:q", "sync_p:q count:d owner:d"),
    ("SYNC_CREATE", 0, 0): ("type:d sync_p:x", "type:d sync_p:x count:d owner:d protocol:d flags:x prioceiling:d clockid:d"),
    ("SYNC_CREATE", 0, 1): ("type:d sync_p:q", "type:d sync_p:q count:d owner:d protocol:d flags:x prioceiling:d clockid:d"),
}
for _n in ("MSG_SENDVNC",):
    for _k in [k for k in KER_ARGS if k[0] == "MSG_SENDV"]:
        KER_ARGS[(_n,) + _k[1:]] = KER_ARGS[_k]
for _k in [k for k in KER_ARGS if k[0] == "MSG_RECEIVEV"]:
    KER_ARGS[("MSG_RECEIVEPULSEV",) + _k[1:]] = KER_ARGS[_k]

_SPEC_CACHE = {}


def _spec(text):
    s = _SPEC_CACHE.get(text)
    if s is None:
        s = _SPEC_CACHE[text] = [tuple(f.split(":")) for f in text.split()]
    return s


def _s32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def unpack(words, spec_text):
    """Decode payload words with a field spec; missing words are left out."""
    args = {}
    i, n = 0, len(words)
    for name, kind in _spec(spec_text):
        if kind == "s":
            raw = b"".join(w.to_bytes(4, "little") for w in words[i:])
            args[name] = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            i = n
            break
        if kind in ("q", "e"):
            if i + 1 >= n:
                break
            v = words[i] | (words[i + 1] << 32)
            args[name] = v - (1 << 64) if kind == "e" and v >> 63 else v
            i += 2
            continue
        if i >= n:
            break
        if kind == "d":
            args[name] = _s32(words[i])
        elif kind == "x":
            args[name] = words[i]
        i += 1
    return args


THREAD_WIDE = "pid:d tid:d priority:d policy:d"


def is_thread_state(ev):
    return ev.cls == C_PRTH and ev.code < STATE_MAX


def state_name(code):
    if code == -1:
        return "UNKNOWN"     # trace-studio's own marker: events were lost, state not known
    return STATES.get(code, "STATE%d" % code)


def msg_type_name(msg0, to_procnto):
    """Name of a message from its first 32-bit word: _IO_* types (and the _IO_CONNECT subtype)
    for any server, system manager types (< 0x100) only for messages to procnto."""
    t = msg0 & 0xFFFF
    if t == 0x100:
        return IO_CONNECT_SUBTYPES.get(msg0 >> 16, "_IO_CONNECT")
    if 0x100 < t <= 0x1FF or (t < 0x100 and to_procnto):
        return MSG_TYPES.get(t)
    return None


def kercall(ev):
    """(call name, is_exit, is64, interrupted) for a KERCALL-class event."""
    num = ev.code & 0x7F
    return (KERCALLS.get(num, "KER%d" % num), bool(ev.code & KER_EXIT_BIT), bool(ev.code & KER_64_BIT),
            bool(ev.code & KER_INT_BIT))


def describe(ev):
    """Return (class_label, event_label, args) with traceprinter-style labels."""
    c = ev.cls
    w = ev.words
    if c == C_PRTH:
        if ev.code < STATE_MAX:
            return "THREAD", "TH" + state_name(ev.code), unpack(w, THREAD_WIDE)
        if ev.code not in PROCESS_EVENTS and STATE_MAX <= ev.code < 2 * STATE_MAX:
            return "VTHREAD", "VTH" + state_name(ev.code - STATE_MAX), unpack(w, THREAD_WIDE)
        name = PROCESS_EVENTS.get(ev.code, "PROC%d" % ev.code)
        if ev.code == PR_THREAD_NAME:
            return "PROCESS", name, unpack(w, "pid:d tid:d name:s")
        if ev.code in (PR_PROCCREATE_NAME, PR_PROCDESTROY_NAME):
            return "PROCESS", name, unpack(w, "ppid:d pid:d name:s")
        return "PROCESS", name, unpack(w, "ppid:d pid:d")
    if c == C_KERCALL:
        name, is_exit, is64, intr = kercall(ev)
        label = "INT_CALL" if intr else ("KER_EXIT" if is_exit else "KER_CALL")
        args = {}
        if intr:
            pass                       # logged with no parameters
        elif is_exit and w and w[0] == 0xFFFFFFFF:
            args = unpack(w, "ret_val:d errno:d")      # failed call: return code and errno, any variant
        else:
            spec = KER_ARGS.get((name, int(is_exit), int(is64))) or KER_ARGS.get((name, int(is_exit), 0))
            if spec:
                args = unpack(w, spec[0] if len(w) <= 2 else spec[1])
        return label, "%s/%02d" % (name, ev.code & 0x7F), args
    if c == C_COMM:
        name = COMM_NAMES.get(ev.code, "COMM%d" % ev.code)
        if ev.code in (COMM_SMSG, COMM_RMSG):
            return "COMM", name, unpack(w, "rcvid:x pid:d")
        if ev.code in (COMM_REPLY, COMM_ERROR):
            return "COMM", name, unpack(w, "tid:d pid:d")
        if ev.code == COMM_SIGNAL:
            return "COMM", name, unpack(w, "signo:d code:d")
        return "COMM", name, unpack(w, "scoid:x pid:d")
    if c == C_CONTROL:
        name = CONTROL_NAMES.get(ev.code, "CONTROL%d" % ev.code)
        if ev.code == 1:
            return "CONTROL", name, unpack(w, "msb:x lsb:x")
        if ev.code == 2:
            return "CONTROL", name, unpack(w, "sequence:d num_events:d")
        return "CONTROL", name, {}
    if c == C_INT:
        if ev.code == INT_DELIVER:
            return "INTERRUPT", "INT_DELIVER", unpack(w, "intr_id:x")
        return "INTERRUPT", "INT%d" % ev.code, {}
    if c == C_SYSTEM:
        base = ev.code & 0x1FF
        name = SYSTEM_NAMES.get(base, "SYS%d" % base) + ("_64" if ev.code & KER_64_BIT else "")
        if base == 18:
            return "SYSTEM", name, unpack(w, "pid:d tid:d timer_id:d flags:x")
        if base == 2:
            return "SYSTEM", name, unpack(w, "pid:d tid:d path:s")
        return "SYSTEM", name, {}
    if c == C_USER:
        return "USREVENT", "EVENT:%d" % ev.code, {}
    return CLASS_NAMES.get(c, "CLASS%d" % c), "EV%d" % ev.code, {}
