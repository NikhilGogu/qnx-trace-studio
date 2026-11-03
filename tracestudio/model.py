# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct system state from decoded QNX kernel events.

Produces processes and threads (names, state intervals, priorities), per-CPU
context switches and wakeups, interrupts, pulses, and message-passing
transactions (MsgSend -> MsgReceive -> MsgReply -> client unblock).

Linking rules, all from kernel events, none from message payloads:
  * A kernel call or COMM event belongs to the thread running on its CPU
    (the last non-injected THRUNNING on that CPU).
  * SND_MESSAGE(rcvid, server pid) opens a transaction for the sending thread,
    keyed by (server pid, rcvid). An rcvid identifies one pending message, so
    the key is unique while the client is blocked and may be reused after.
  * REC_MESSAGE(rcvid, pid) names the receiving server thread, MsgReceivev
    exit adds _msg_info (wide mode), MsgReplyv/MsgError entry with the rcvid
    and REPLY_MESSAGE/MSG_ERROR(tid, pid) close it, MsgSendv exit on the
    client ends it.
  * A transaction sent by a server thread while it handles another one is a
    child of that transaction (nested request across servers).
Every check that fails is recorded on the transaction as a warning.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .events import (COMM_ERROR, COMM_REPLY, COMM_RMSG, COMM_RPULSE, COMM_SIGNAL, COMM_SMSG, COMM_SPULSE,
                     INT_DELIVER, PR_PROCCREATE, PR_PROCCREATE_NAME, PR_PROCDESTROY, PR_THREAD_NAME, STATE_MAX,
                     THREAD_WIDE, describe, kercall, state_name, unpack)
from .kev import C_COMM, C_CONTROL, C_INT, C_KERCALL, C_PRTH, CTRL_BUFFER, KevFile

ST_DEAD, ST_RUNNING, ST_READY = 0, 1, 2
BLOCKED_IPC = {"SEND", "RECEIVE", "REPLY"}
_NTO_CHF_FIXED_PRIORITY = 0x1


@dataclass
class Thread:
    pid: int
    tid: int
    name: str = ""
    states: list = field(default_factory=list)   # (ts, state code, prio or None, cpu, injected)
    prios: list = field(default_factory=list)    # (ts, prio) on change

    @property
    def key(self):
        return (self.pid, self.tid)


@dataclass
class Process:
    pid: int
    ppid: int = 0
    path: str = ""
    destroyed_ts: int | None = None

    @property
    def name(self):
        return self.path.rsplit("/", 1)[-1] if self.path else "pid%d" % self.pid


@dataclass
class Txn:
    idx: int
    client: tuple
    server_pid: int
    rcvid: int
    t_smsg: int
    coid: int | None = None
    msg: tuple = ()
    t_call: int | None = None
    client_block: str | None = None      # SEND or REPLY: first blocked state after the send
    server: tuple | None = None
    t_rmsg: int | None = None
    t_rx_exit: int | None = None
    info: dict = field(default_factory=dict)
    replier: tuple | None = None
    t_reply_call: int | None = None
    t_reply_msg: int | None = None
    t_reply_exit: int | None = None
    t_unblock: int | None = None          # client THREADY after the reply
    t_exit: int | None = None
    status: int | None = None
    error: int | None = None
    parent: int | None = None
    children: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def complete(self):
        return None not in (self.t_call, self.server, self.t_rx_exit, self.t_reply_call, self.t_exit)


@dataclass
class Pulse:
    t_send: int | None
    sender: tuple | None
    kind: str
    server_pid: int
    scoid: int
    code: int | None = None
    value: int | None = None
    t_recv: int | None = None
    receiver: tuple | None = None


class Model:
    def __init__(self, kev: KevFile):
        self.kev = kev
        self.processes: dict[int, Process] = {}
        self.threads: dict[tuple, Thread] = {}
        self.switches = []      # (ts, cpu, prev key|None, prev state|None, prev prio, next key, next prio)
        self.wakeups = []       # (ts, cpu, waker key|None, woken key, prio)
        self.interrupts = []    # (ts, cpu, intr_id)
        self.txns: list[Txn] = []
        self.pulses: list[Pulse] = []
        self.channels = {}      # (pid, chid) -> dict(flags, tid, ts)
        self.connections = {}   # (pid, coid) -> (server pid, chid)
        self.signals = []
        self.gaps = []          # (ts, cpu, first lost seq, last lost seq)
        self.counters = defaultdict(int)
        self.wide = None
        self._run()

    # ---- helpers ----------------------------------------------------------
    def thread(self, pid, tid):
        k = (pid, tid)
        t = self.threads.get(k)
        if t is None:
            t = self.threads[k] = Thread(pid, tid)
            self.process(pid)
        return t

    def process(self, pid):
        p = self.processes.get(pid)
        if p is None:
            p = self.processes[pid] = Process(pid)
        return p

    def state_at(self, key):
        t = self.threads.get(key)
        return t.states[-1][1] if t and t.states else None

    def is_idle(self, key):
        """procnto's per-CPU idle threads: pid 1, running at priority 0."""
        if key is None or key[0] != 1:
            return False
        t = self.threads.get(key)
        if t and any(s[2] is not None for s in t.states):
            return all(s[2] == 0 for s in t.states if s[2] is not None and s[1] == ST_RUNNING)
        return key[1] <= self.kev.ncpu

    def label(self, key):
        if key is None:
            return "?"
        t = self.threads.get(key)
        p = self.processes.get(key[0])
        pn = p.name if p else "pid%d" % key[0]
        return "%s/%s" % (pn, t.name if t and t.name else "tid%d" % key[1])

    # ---- main pass --------------------------------------------------------
    def _run(self):
        running = {}            # cpu -> thread key
        where = {}              # thread key -> cpu it runs on
        pend_send = {}          # client key -> (ts, coid, msg)
        open_by_client = {}     # client key -> Txn
        open_by_rcvid = {}      # (server pid, rcvid) -> Txn
        handling = defaultdict(list)   # server thread key -> [Txn] received, not yet replied
        replying = {}           # server thread key -> Txn (between MsgReplyv entry and exit)
        pend_pulse = {}         # sender key -> (ts, coid, code, value)
        pulse_q = defaultdict(deque)   # (server pid, scoid) -> deque[Pulse]
        pend_conn = {}
        pend_chan = {}
        c = self.counters
        bufseq = {}             # cpu -> last kernel buffer sequence number

        for ev in self.kev.events():
            cls = ev.cls
            if cls == C_CONTROL:
                if ev.code == CTRL_BUFFER:
                    prev_seq = bufseq.get(ev.cpu)
                    if prev_seq is not None and ev.words[0] != prev_seq + 1:
                        # events were lost on this CPU: whoever we thought was running is unknown now,
                        # and every open exchange may be missing its middle
                        c["lost_buffer_gaps"] += 1
                        old = running.get(ev.cpu)
                        if old is not None:   # close the CPU: it ran something unknown during the gap
                            ot = self.threads.get(old)
                            self.switches.append((ev.ts, ev.cpu, old, self.state_at(old),
                                                  ot.prios[-1][1] if ot and ot.prios else None, None, 0))
                        running[ev.cpu] = None
                        for tx in open_by_client.values():
                            tx.warnings.append("spans lost buffer on cpu %d" % ev.cpu)
                        self.gaps.append((ev.ts, ev.cpu, prev_seq + 1, ev.words[0] - 1))
                    bufseq[ev.cpu] = ev.words[0]
                continue
            if cls == C_PRTH:
                if ev.code < STATE_MAX:
                    a = unpack(ev.words, THREAD_WIDE)
                    if self.wide is None:
                        self.wide = "priority" in a
                    key = (a.get("pid"), a.get("tid"))
                    t = self.thread(*key)
                    prio = a.get("priority")
                    prev_state = t.states[-1][1] if t.states else None
                    prio_before = t.prios[-1][1] if t.prios else None
                    t.states.append((ev.ts, ev.code, prio, ev.cpu, ev.injected))
                    if prio is not None and (not t.prios or t.prios[-1][1] != prio):
                        t.prios.append((ev.ts, prio))
                    if ev.injected:
                        continue
                    if ev.code == ST_RUNNING:
                        prev = running.get(ev.cpu)
                        oc = where.get(key)
                        if oc is not None and oc != ev.cpu and running.get(oc) == key:
                            # migrated before its old CPU switched to anything else: that CPU went idle
                            self.switches.append((ev.ts, oc, key, prev_state, prio_before, None, 0))
                            c["migrations_without_switch_out"] += 1
                            running[oc] = None
                        pt = self.threads.get(prev) if prev else None
                        self.switches.append((ev.ts, ev.cpu, prev, self.state_at(prev) if prev else None,
                                              pt.prios[-1][1] if pt and pt.prios else None, key, prio))
                        running[ev.cpu] = key
                        where[key] = ev.cpu
                    else:
                        if ev.code == ST_READY and prev_state not in (None, ST_RUNNING, ST_READY):
                            self.wakeups.append((ev.ts, ev.cpu, running.get(ev.cpu), key, prio))
                        if key in open_by_client:
                            tx = open_by_client[key]
                            sn = state_name(ev.code)
                            if tx.client_block is None and sn in ("SEND", "REPLY"):
                                tx.client_block = sn
                            if ev.code == ST_READY and tx.t_reply_msg is not None and tx.t_unblock is None:
                                tx.t_unblock = ev.ts
                    continue
                if ev.code in (PR_PROCCREATE, PR_PROCCREATE_NAME):
                    a = unpack(ev.words, "ppid:d pid:d name:s")
                    p = self.process(a.get("pid"))
                    p.ppid = a.get("ppid", 0)
                    if a.get("name"):
                        p.path = a["name"]
                elif ev.code == PR_PROCDESTROY:
                    a = unpack(ev.words, "ppid:d pid:d")
                    self.process(a.get("pid")).destroyed_ts = ev.ts
                elif ev.code == PR_THREAD_NAME:
                    a = unpack(ev.words, "pid:d tid:d name:s")
                    self.thread(a.get("pid"), a.get("tid")).name = a.get("name", "")
                continue

            if cls == C_INT:
                if ev.code == INT_DELIVER:
                    self.interrupts.append((ev.ts, ev.cpu, ev.words[0]))
                continue

            if cls not in (C_KERCALL, C_COMM):
                continue
            actor = running.get(ev.cpu)
            if actor is None:
                c["events_without_actor"] += 1

            if cls == C_KERCALL:
                name, is_exit, _, _ = kercall(ev)
                _, _, a = describe(ev)
                if name in ("MSG_SENDV", "MSG_SENDVNC"):
                    if not is_exit:
                        pend_send[actor] = (ev.ts, a.get("coid"), tuple(a.get("msg%d" % i) for i in range(3) if "msg%d" % i in a))
                    else:
                        tx = open_by_client.pop(actor, None)
                        pend_send.pop(actor, None)
                        if tx is None:
                            c["send_exit_without_txn"] += 1
                            continue
                        tx.t_exit = ev.ts
                        tx.status = a.get("status")
                        if tx.t_reply_msg is None:
                            tx.warnings.append("client returned without REPLY_MESSAGE/MSG_ERROR")
                            open_by_rcvid.pop((tx.server_pid, tx.rcvid), None)
                elif name in ("MSG_RECEIVEV", "MSG_RECEIVEPULSEV"):
                    if is_exit:
                        rcvid = a.get("rcvid", 0)
                        if rcvid and rcvid != 0xFFFFFFFF and actor is not None:
                            tx = open_by_rcvid.get((actor[0], rcvid))
                            if tx is None:
                                c["receive_exit_without_txn"] += 1
                                continue
                            if tx.server is None:
                                tx.server = actor
                                tx.warnings.append("REC_MESSAGE missing; server taken from MsgReceivev exit")
                                handling[actor].append(tx)
                            elif tx.server != actor:
                                tx.warnings.append("MsgReceivev exit on %s, REC_MESSAGE on %s" % (actor, tx.server))
                            tx.t_rx_exit = ev.ts
                            if "pid" in a:
                                tx.info = {k: a[k] for k in ("pid", "tid", "chid", "scoid", "coid", "msglen", "priority") if k in a}
                                if (a["pid"], a.get("tid")) != tx.client:
                                    tx.warnings.append("_msg_info sender %s != client %s" % ((a["pid"], a.get("tid")), tx.client))
                                    c["info_sender_mismatch"] += 1
                                else:
                                    c["info_sender_match"] += 1
                elif name in ("MSG_REPLYV", "MSG_ERROR"):
                    if not is_exit:
                        rcvid = a.get("rcvid")
                        tx = open_by_rcvid.get((actor[0], rcvid)) if actor else None
                        if tx is None:
                            c["reply_without_txn"] += 1
                            continue
                        tx.replier = actor
                        tx.t_reply_call = ev.ts
                        if name == "MSG_ERROR":
                            tx.error = a.get("err")
                        replying[actor] = tx
                    else:
                        tx = replying.pop(actor, None)
                        if tx is not None:
                            tx.t_reply_exit = ev.ts
                elif name in ("MSG_SEND_PULSE", "MSG_SEND_PULSEPTR"):
                    if not is_exit:
                        pend_pulse[actor] = (ev.ts, a.get("coid"), a.get("code"), a.get("value"))
                elif name == "CONNECT_ATTACH":
                    if not is_exit:
                        pend_conn[actor] = (a.get("pid"), a.get("chid"))
                    else:
                        pc = pend_conn.pop(actor, None)
                        if pc and actor and a.get("coid", 0xFFFFFFFF) != 0xFFFFFFFF:
                            self.connections[(actor[0], a["coid"])] = pc
                elif name == "CHANNEL_CREATE":
                    if not is_exit:
                        pend_chan[actor] = a.get("flags", 0)
                    else:
                        fl = pend_chan.pop(actor, None)
                        if fl is not None and actor and a.get("chid", -1) >= 0:
                            self.channels[(actor[0], a["chid"])] = {"flags": fl, "tid": actor[1], "ts": ev.ts}
                continue

            # COMM class
            a = unpack(ev.words, "w0:x w1:d")
            code = ev.code
            if code == COMM_SMSG:
                rcvid, spid = a["w0"], a["w1"]
                if actor is None:
                    c["smsg_without_actor"] += 1
                    continue
                key = (spid, rcvid)
                old = open_by_rcvid.pop(key, None)
                if old is not None:
                    old.warnings.append("rcvid reused while still open")
                    c["rcvid_reused_open"] += 1
                tx = Txn(len(self.txns), actor, spid, rcvid, ev.ts)
                ps = pend_send.get(actor)
                if ps:
                    tx.t_call, tx.coid, tx.msg = ps
                else:
                    tx.warnings.append("no MsgSendv entry before SND_MESSAGE")
                if handling.get(actor):
                    parent = handling[actor][-1]
                    tx.parent = parent.idx
                    parent.children.append(tx.idx)
                self.txns.append(tx)
                open_by_rcvid[key] = tx
                if actor in open_by_client:
                    c["client_already_open"] += 1
                open_by_client[actor] = tx
            elif code == COMM_RMSG:
                rcvid, spid = a["w0"], a["w1"]
                tx = open_by_rcvid.get((spid, rcvid))
                if tx is None:
                    c["rmsg_without_txn"] += 1
                    continue
                if actor is None or actor[0] != spid:
                    tx.warnings.append("REC_MESSAGE actor %s is not in server pid %d" % (actor, spid))
                    c["rmsg_actor_not_server"] += 1
                tx.server = actor
                tx.t_rmsg = ev.ts
                handling[actor].append(tx)
            elif code in (COMM_REPLY, COMM_ERROR):
                ckey = (a["w1"], a["w0"])  # (pid, tid)
                tx = open_by_client.get(ckey)
                if tx is None:
                    c["reply_msg_without_txn"] += 1
                    continue
                tx.t_reply_msg = ev.ts
                if tx.replier is not None and tx.replier != actor:
                    tx.warnings.append("REPLY_MESSAGE actor differs from MsgReplyv caller")
                open_by_rcvid.pop((tx.server_pid, tx.rcvid), None)
                if tx.server is not None and tx in handling.get(tx.server, []):
                    handling[tx.server].remove(tx)
            elif code == COMM_RPULSE:
                q = pulse_q.get((a["w1"], a["w0"]))
                if q:
                    pl = q.popleft()
                else:
                    pl = Pulse(None, None, "unknown", a["w1"], a["w0"])
                    self.pulses.append(pl)
                    c["pulse_recv_without_send"] += 1
                pl.t_recv, pl.receiver = ev.ts, actor
            elif code == COMM_SIGNAL:
                self.signals.append((ev.ts, ev.cpu, actor, a["w0"]))
            else:   # SND_PULSE*, kernel- or user-generated
                kind = {COMM_SPULSE: "pulse"}.get(code, describe(ev)[1].replace("SND_PULSE_", "pulse_").lower())
                pl = Pulse(ev.ts, actor, kind, a["w1"], a["w0"])
                if code == COMM_SPULSE:
                    pp = pend_pulse.pop(actor, None)
                    if pp:
                        pl.code, pl.value = pp[2], pp[3]
                self.pulses.append(pl)
                pulse_q[(a["w1"], a["w0"])].append(pl)

        for tx in open_by_client.values():
            tx.warnings.append("still open at end of trace")
        c["txns"] = len(self.txns)
        c["txns_complete"] = sum(1 for t in self.txns if t.complete)

    # ---- derived views ----------------------------------------------------
    def intervals(self, key, end_ts):
        """[(start, end, state code, prio)] for one thread."""
        t = self.threads[key]
        out = []
        for i, (ts, code, prio, _cpu, _inj) in enumerate(t.states):
            nxt = t.states[i + 1][0] if i + 1 < len(t.states) else end_ts
            if nxt > ts:
                out.append((ts, nxt, code, prio))
        return out

    def fixed_priority_channels(self):
        return {k: v for k, v in self.channels.items() if v["flags"] & _NTO_CHF_FIXED_PRIORITY}
