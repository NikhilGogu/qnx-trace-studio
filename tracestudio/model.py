# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct system state from decoded QNX kernel events.

Produces processes and threads (names, state intervals, priorities, run
masks), per-CPU context switches, proven wakeups, interrupts, pulses, and
message-passing transactions (MsgSend -> MsgReceive -> MsgReply -> client
unblock).

Linking rules, all from kernel events, none from message payloads:
  * A kernel call or COMM event belongs to the thread running on its CPU (the
    last non-injected THRUNNING there). This is an inference: after lost
    events, a migration or at trace start the CPU's thread is UNKNOWN and its
    events are counted as actorless and not linked. Partial combine events are
    counted and not linked either.
  * SND_MESSAGE(rcvid, server pid) inside the sender's MsgSendv opens a
    transaction keyed by (server pid, rcvid). An rcvid names one pending
    message, so the key is unique while the client is blocked and is reused
    after. Every index (by client, by rcvid, received-not-replied per server
    thread, replying) is cleaned by object identity, so a late or duplicate
    completion can never remove a newer transaction that reuses a key.
  * REC_MESSAGE(rcvid, pid) names the receiving thread, MsgReceivev exit adds
    _msg_info (wide mode); an _msg_info that names another sender is kept
    aside and marks the transaction untrustworthy. MsgReplyv/MsgError entry
    with the rcvid and REPLY_MESSAGE/MSG_ERROR(tid, pid) close the server side;
    MsgSendv exit (or INT_CALL) on the client ends it.
  * A request sent by a thread that has exactly one received, unreplied
    request is its child (nested request across servers). With several
    outstanding requests the parent is ambiguous: the candidates are recorded,
    no parent is set.
  * Lost events (kev.Loss) close the CPU (its occupant becomes UNKNOWN), set
    every thread that could have changed state there to UNKNOWN until its next
    event, flag all open exchanges and make pulse matching uncertain.
  * A READY is a proven wakeup only when it happens inside a kernel call of
    the thread running on that CPU that addressed the woken thread: a message
    or pulse sent to its process while it was RECEIVE-blocked, a reply or
    error naming it, or a SyncMutexUnlock/CondvarSignal/SemPost releasing a
    thread blocked in the matching state.
  * Pulses: a sender is known only for a pulse emitted inside the sending
    thread's MsgSendPulse (or MsgDeliverEvent, ConnectDetach for the kernel
    pulses those calls generate). A receive matches a send only when exactly
    one pulse is queued for that (server pid, scoid) and nothing made the
    queue uncertain; otherwise the candidates and a confidence are recorded.
A transaction is complete when its whole path was seen, and trustworthy when
it is complete and carries no warnings. Analyses use trustworthy ones only.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .events import (COMM_ERROR, COMM_REPLY, COMM_RMSG, COMM_RPULSE, COMM_SIGNAL, COMM_SMSG, COMM_SPULSE,
                     COMM_SPULSE_DIS, COMM_SPULSE_EXE, INT_DELIVER, PR_PROCCREATE, PR_PROCCREATE_NAME,
                     PR_PROCDESTROY, PR_THREAD_NAME, STATE_MAX, THREAD_WIDE, describe, kercall, msg_type_name,
                     state_name, unpack)
from .kev import C_COMM, C_INT, C_KERCALL, C_PRTH, C_SYSTEM, KevFile

ST_UNKNOWN = -1
ST_DEAD, ST_RUNNING, ST_READY, ST_STOPPED, ST_SEND, ST_RECEIVE, ST_REPLY = range(7)
ST_MUTEX, ST_CONDVAR, ST_SEM, ST_MUON_MUTEX = 13, 14, 17, 26
NOT_BLOCKED = (None, ST_UNKNOWN, ST_RUNNING, ST_READY)
BLOCKED_IPC = {"SEND", "RECEIVE", "REPLY"}
UNKNOWN = (-1, 0)          # pseudo thread: what ran on a CPU is not known
PROCNTO_PID = 1
_NTO_CHF_FIXED_PRIORITY = 0x1
_NTO_TCTL_RUNMASK = 4
_RUNMASK_BY_POINTER = (6, 7, 10)   # _GET_AND_SET, _GET_AND_SET_64, _GET_AND_SET_INHERIT
SEND_CALLS = ("MSG_SENDV", "MSG_SENDVNC")
REPLY_CALLS = ("MSG_REPLYV", "MSG_ERROR")
PULSE_CALLS = ("MSG_SEND_PULSE", "MSG_SEND_PULSEPTR")
# kernel call of the running thread that proves who sent a pulse of this kind
PULSE_SENDER_CALLS = {COMM_SPULSE: PULSE_CALLS, COMM_SPULSE_EXE: ("MSG_DELIVER_EVENT",),
                      COMM_SPULSE_DIS: ("CONNECT_DETACH",)}
SYNC_WAKE = {"SYNC_MUTEX_UNLOCK": (ST_MUTEX, ST_MUON_MUTEX), "SYNC_CONDVAR_SIGNAL": (ST_CONDVAR,),
             "SYNC_SEM_POST": (ST_SEM,)}


@dataclass
class Thread:
    pid: int
    tid: int
    name: str = ""
    states: list = field(default_factory=list)    # (ts, state code, prio or None, cpu, injected)
    prios: list = field(default_factory=list)     # (ts, prio) on change
    runmask: list = field(default_factory=list)   # (ts, mask or None) from ThreadCtl

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


@dataclass(eq=False)
class Txn:
    idx: int
    client: tuple
    server_pid: int
    rcvid: int
    t_smsg: int
    coid: int | None = None
    msg: tuple = ()
    msg_type: str | None = None          # _IO_* or system message name from msg[0]
    path: str | None = None              # path the client opened this coid with (PATHMGR + ConnectAttach)
    t_call: int | None = None
    client_block: str | None = None      # SEND or REPLY: first blocked state after the send
    server: tuple | None = None
    t_rmsg: int | None = None
    t_rx_exit: int | None = None
    info: dict = field(default_factory=dict)
    info_conflict: dict | None = None    # _msg_info that names another sender (quarantined)
    replier: tuple | None = None
    t_reply_call: int | None = None
    t_reply_msg: int | None = None
    t_reply_exit: int | None = None
    t_unblock: int | None = None          # client THREADY after the reply
    t_exit: int | None = None
    status: int | None = None
    error: int | None = None
    outcome: str | None = None            # replied, error, cancelled, interrupted
    parent: int | None = None
    parent_candidates: list = field(default_factory=list)
    children: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def complete(self):
        return None not in (self.t_call, self.server, self.t_rx_exit, self.t_reply_call, self.t_exit)

    @property
    def trustworthy(self):
        return self.complete and not self.warnings


@dataclass(eq=False)
class Pulse:
    idx: int
    t_send: int | None
    sender: tuple | None        # thread proven to have sent it; None for kernel or unknown senders
    kind: str
    server_pid: int
    scoid: int
    code: int | None = None
    value: int | None = None
    prio: int | None = None
    context: tuple | None = None    # thread that happened to run on the sending CPU, when the sender is unknown
    t_recv: int | None = None
    receiver: tuple | None = None
    cpu: int | None = None          # CPU the send was emitted on
    match: str | None = None        # unique, ambiguous, none (received, no traced send); None: not received
    candidates: list = field(default_factory=list)
    confidence: float | None = None


@dataclass(eq=False)
class Call:
    name: str
    ts: int
    cpu: int
    args: dict
    sent_msg: set = field(default_factory=set)      # server pids messaged in this call
    sent_pulse: set = field(default_factory=set)    # server pids pulsed in this call
    replied: set = field(default_factory=set)       # client keys replied to in this call
    deferred: list = field(default_factory=list)    # READYs from RECEIVE seen before this call's SND_PULSE*


class Model:
    def __init__(self, kev: KevFile):
        self.kev = kev
        self.processes: dict[int, Process] = {}
        self.threads: dict[tuple, Thread] = {}
        self.switches = []      # (ts, cpu, prev key|None|UNKNOWN, prev state, prev prio, next key|UNKNOWN, next prio)
        self.wakeups = []       # (ts, cpu, waker key, woken key, prio, how)
        self.interrupts = []    # (ts, cpu, intr_id)
        self.txns: list[Txn] = []
        self.pulses: list[Pulse] = []
        self.channels = {}      # (pid, chid) -> dict(flags, tid, ts)
        self.connections = {}   # (pid, coid) -> (server pid, chid)
        self.coid_paths = {}    # (pid, coid) -> path
        self.signals = []
        self.gaps = []          # kev.Loss records applied, in time order
        self.counters = defaultdict(int)
        self.wide = None        # True when thread events carry priorities
        evs = kev.events()
        self.start_ts = evs[0].ts if evs else 0
        self.end_ts = evs[-1].ts if evs else 0
        self._path_window = kev.cycles_per_sec // 100
        self._running = {}            # cpu -> thread key, UNKNOWN, or absent (nothing seen yet)
        self._where = {}              # thread key -> cpu it last ran on
        self._in_call = {}            # thread key -> Call in progress
        self._pend_send = {}          # client key -> [ts, coid, msg, spans_loss]
        self._by_client = {}          # client key -> open Txn
        self._by_rcvid = {}           # (server pid, rcvid) -> open Txn
        self._handling = defaultdict(list)   # server thread -> [Txn] received, not yet replied
        self._replying = {}           # server thread -> Txn between MsgReplyv entry and exit
        self._pulse_q = defaultdict(deque)   # (server pid, scoid) -> sent, not yet received
        self._pulse_uncertain = set()
        self._last_path = {}          # thread key -> (ts, path) from the latest PATHMGR event
        self._run(evs)

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

    def prio_of(self, key):
        t = self.threads.get(key)
        if not t or not t.states:
            return None
        last = t.states[-1]
        return None if last[1] == ST_UNKNOWN else (t.prios[-1][1] if t.prios else None)

    def is_idle(self, key):
        """procnto's per-CPU idle threads: pid 1, running at priority 0."""
        if key is None or key[0] != PROCNTO_PID:
            return False
        t = self.threads.get(key)
        if t and t.name:
            return t.name.startswith("idle")
        if t and any(s[2] is not None for s in t.states):
            return all(s[2] == 0 for s in t.states if s[2] is not None and s[1] == ST_RUNNING)
        return key[1] <= self.kev.ncpu

    def label(self, key):
        if key is None:
            return "?"
        if key == UNKNOWN:
            return "UNKNOWN"
        t = self.threads.get(key)
        p = self.processes.get(key[0])
        pn = p.name if p else "pid%d" % key[0]
        return "%s/%s" % (pn, t.name if t and t.name else "tid%d" % key[1])

    def server_name(self, tx):
        p = self.processes.get(tx.server_pid)
        return p.name if p else "pid%d" % tx.server_pid

    # ---- main pass --------------------------------------------------------
    def _run(self, evs):
        gaps = sorted((l for l in self.kev.losses if l.t0 is not None), key=lambda l: l.t0)
        gi = 0
        c = self.counters
        for ev in evs:
            while gi < len(gaps) and gaps[gi].t0 < ev.ts:
                self._gap(gaps[gi])
                gi += 1
            cls = ev.cls
            if cls == C_PRTH:
                if ev.code < STATE_MAX:
                    self._state(ev)
                else:
                    self._process_event(ev)
            elif cls == C_KERCALL:
                self._kercall(ev)
            elif cls == C_COMM:
                self._comm(ev)
            elif cls == C_INT:
                if ev.code == INT_DELIVER and ev.words:
                    self.interrupts.append((ev.ts, ev.cpu, ev.words[0]))
            elif cls == C_SYSTEM and ev.code & 0x1FF == 2 and not ev.partial:   # PATHMGR
                a = unpack(ev.words, "pid:d tid:d path:s")
                if "path" in a:
                    self._last_path[(a["pid"], a["tid"])] = (ev.ts, a["path"])
        for g in gaps[gi:]:
            self._gap(g)
        for tx in {id(t): t for t in list(self._by_client.values()) + list(self._by_rcvid.values())}.values():
            tx.warnings.append("still open at end of trace")
        c["txns"] = len(self.txns)
        c["txns_complete"] = sum(1 for t in self.txns if t.complete)
        c["txns_trustworthy"] = sum(1 for t in self.txns if t.trustworthy)

    def _actor(self, ev):
        k = self._running.get(ev.cpu)
        if k is None or k == UNKNOWN:
            self.counters["events_without_actor"] += 1
            return None
        if ev.partial:
            self.counters["partial_events_not_linked"] += 1
            return None
        if self.state_at(k) != ST_RUNNING:
            self.counters["actor_not_running"] += 1
        return k

    # ---- lost events ------------------------------------------------------
    def _gap(self, loss):
        cpu, t0 = loss.cpu, loss.t0
        self.gaps.append(loss)
        self.counters["lost_event_gaps"] += 1
        old = self._running.get(cpu)
        if old != UNKNOWN:
            self.switches.append((t0, cpu, old, self.state_at(old) if old else None,
                                  self.prio_of(old) if old else None, UNKNOWN, None))
            self._running[cpu] = UNKNOWN
            if old is not None:
                self._end_call(old)
        note = "spans lost events on cpu %d" % cpu
        open_txns = {id(t): t for t in list(self._by_client.values()) + list(self._by_rcvid.values())
                     + list(self._replying.values()) + [x for lst in self._handling.values() for x in lst]}
        for tx in open_txns.values():
            if note not in tx.warnings:
                tx.warnings.append(note)
        for ps in self._pend_send.values():
            ps[3] = True
        self._pulse_uncertain.update(self._pulse_q.keys())
        for key, t in self.threads.items():
            if not t.states or t.states[-1][1] == ST_UNKNOWN or self.is_idle(key):
                continue
            oc = self._where.get(key)
            # a thread running on another CPU, whose events are intact, keeps its state
            if oc is not None and oc != cpu and self._running.get(oc) == key:
                continue
            t.states.append((t0, ST_UNKNOWN, None, cpu, False))

    # ---- thread and process events ----------------------------------------
    def _state(self, ev):
        c = self.counters
        a = unpack(ev.words, THREAD_WIDE)
        pid, tid = a.get("pid"), a.get("tid")
        if pid is None or tid is None:
            c["state_events_incomplete"] += 1
            return
        if self.wide is None and not ev.partial:
            self.wide = "priority" in a
        prio = a.get("priority")
        key = (pid, tid)
        t = self.thread(pid, tid)
        prev_state = t.states[-1][1] if t.states else None
        prio_before = self.prio_of(key)
        code = ev.code
        t.states.append((ev.ts, code, prio, ev.cpu, ev.injected))
        if prio is not None and (not t.prios or t.prios[-1][1] != prio):
            t.prios.append((ev.ts, prio))
        if ev.injected:
            return
        if code == ST_RUNNING:
            prev = self._running.get(ev.cpu)
            oc = self._where.get(key)
            if oc is not None and oc != ev.cpu and self._running.get(oc) == key:
                # it runs here before its old CPU switched to anything: that CPU's occupant is unknown
                self.switches.append((ev.ts, oc, key, prev_state, prio_before, UNKNOWN, None))
                self._running[oc] = UNKNOWN
                c["migrations_without_switch_out"] += 1
            if prev == key:
                prev_code, prev_prio = prev_state, prio_before
            else:
                prev_code = self.state_at(prev) if prev not in (None, UNKNOWN) else None
                prev_prio = self.prio_of(prev) if prev not in (None, UNKNOWN) else None
            self.switches.append((ev.ts, ev.cpu, prev, prev_code, prev_prio, key, prio))
            self._running[ev.cpu] = key
            self._where[key] = ev.cpu
            return
        if code == ST_READY and prev_state not in NOT_BLOCKED:
            self._wakeup(ev, key, prev_state, prio)
        tx = self._by_client.get(key)
        if tx is not None:
            if tx.client_block is None and code in (ST_SEND, ST_REPLY):
                tx.client_block = state_name(code)
            if code == ST_READY and tx.t_reply_msg is not None and tx.t_unblock is None:
                tx.t_unblock = ev.ts
        if code == ST_DEAD:
            self._thread_gone(key, ev.ts, "died")

    def _wakeup(self, ev, woken, prev_state, prio):
        k = self._running.get(ev.cpu)
        call = self._in_call.get(k) if k not in (None, UNKNOWN) else None
        how = None
        if call is not None and k != woken:
            if prev_state == ST_RECEIVE and woken[0] in call.sent_msg:
                how = "message"
            elif prev_state == ST_RECEIVE and woken[0] in call.sent_pulse:
                how = "pulse"
            elif prev_state in (ST_REPLY, ST_SEND) and woken in call.replied:
                how = "reply"
            elif prev_state in SYNC_WAKE.get(call.name, ()):
                how = call.name.lower()
        if how:
            self.wakeups.append((ev.ts, ev.cpu, k, woken, prio, how))
            self.counters["wakeups_proven"] += 1
        elif call is not None and prev_state == ST_RECEIVE and \
                any(call.name in names for names in PULSE_SENDER_CALLS.values()):
            # inside MsgSendPulse the receiver's READY comes before SND_PULSE: decide at the pulse
            call.deferred.append((ev.ts, ev.cpu, k, woken, prio))
        else:
            self.counters["wakeups_unproven"] += 1

    def _end_call(self, key):
        call = self._in_call.pop(key, None)
        if call is not None and call.deferred:
            self.counters["wakeups_unproven"] += len(call.deferred)
        return call

    def _thread_gone(self, key, ts, why):
        tx = self._by_client.get(key)
        if tx is not None:
            tx.warnings.append("client %s before MsgSendv returned" % why)
            self._close(tx)
        self._pend_send.pop(key, None)
        self._end_call(key)
        self._replying.pop(key, None)
        for tx in self._handling.pop(key, []):
            tx.warnings.append("receiving thread %s before the reply" % why)
        t = self.threads.get(key)
        if t is not None:
            t.runmask.append((ts, None))

    def _process_event(self, ev):
        if ev.code in (PR_PROCCREATE, PR_PROCCREATE_NAME):
            a = unpack(ev.words, "ppid:d pid:d name:s")
            if "pid" in a:
                p = self.process(a["pid"])
                p.ppid = a.get("ppid", 0)
                if a.get("name"):
                    p.path = a["name"]
        elif ev.code == PR_PROCDESTROY:
            a = unpack(ev.words, "ppid:d pid:d")
            pid = a.get("pid")
            if pid is not None:
                self.process(pid).destroyed_ts = ev.ts
                for key in [k for k in self.threads if k[0] == pid]:
                    self._thread_gone(key, ev.ts, "process destroyed")
        elif ev.code == PR_THREAD_NAME:
            a = unpack(ev.words, "pid:d tid:d name:s")
            if "tid" in a:
                self.thread(a["pid"], a["tid"]).name = a.get("name", "")

    # ---- kernel calls -----------------------------------------------------
    def _kercall(self, ev):
        actor = self._actor(ev)
        if actor is None:
            return
        c = self.counters
        name, is_exit, _, intr = kercall(ev)
        _, _, a = describe(ev)
        if not is_exit and not intr:
            if actor in self._in_call:
                c["call_entry_while_in_call"] += 1
                self._end_call(actor)
            self._in_call[actor] = Call(name, ev.ts, ev.cpu, a)
            if name in SEND_CALLS:
                self._pend_send[actor] = [ev.ts, a.get("coid"),
                                          tuple(a["msg%d" % i] for i in range(3) if "msg%d" % i in a), False]
            elif name in REPLY_CALLS:
                self._reply_entry(actor, name, a, ev.ts)
            elif name == "CONNECT_DETACH" and "coid" in a:
                self.coid_paths.pop((actor[0], a["coid"]), None)
            return
        call = self._end_call(actor)
        if call is None or call.name != name:
            c["call_exit_without_entry"] += 1
            call = None
        if intr:
            c["interrupted_calls"] += 1
        if name in SEND_CALLS:
            self._send_exit(actor, a, ev.ts, intr)
        elif name in ("MSG_RECEIVEV", "MSG_RECEIVEPULSEV"):
            if not intr:
                self._receive_exit(actor, a, ev.ts)
        elif name in REPLY_CALLS:
            tx = self._replying.pop(actor, None)
            if tx is not None:
                tx.t_reply_exit = ev.ts
        elif call is None or intr:
            return
        elif name == "CONNECT_ATTACH":
            coid = a.get("coid")
            if coid is not None and coid != 0xFFFFFFFF:
                self.connections[(actor[0], coid)] = (call.args.get("pid"), call.args.get("chid"))
                lp = self._last_path.pop(actor, None)
                if lp and ev.ts - lp[0] <= self._path_window:
                    self.coid_paths[(actor[0], coid)] = lp[1]
                else:
                    self.coid_paths.pop((actor[0], coid), None)
        elif name == "CHANNEL_CREATE":
            chid = a.get("chid", -1)
            if chid >= 0:
                self.channels[(actor[0], chid)] = {"flags": call.args.get("flags", 0), "tid": actor[1], "ts": ev.ts}
        elif name == "THREAD_CTL":
            cmd = call.args.get("cmd")
            if a.get("ret_val", 0) != 0:
                return
            if cmd == _NTO_TCTL_RUNMASK and "data" in call.args:
                self.thread(*actor).runmask.append((call.ts, call.args["data"]))
            elif cmd in _RUNMASK_BY_POINTER:
                self.thread(*actor).runmask.append((call.ts, None))

    def _reply_entry(self, actor, name, a, ts):
        tx = self._by_rcvid.get((actor[0], a.get("rcvid")))
        if tx is None:
            self.counters["reply_without_txn"] += 1
            return
        tx.replier = actor
        tx.t_reply_call = ts
        if name == "MSG_ERROR":
            tx.error = a.get("err")
        if tx.server is not None and tx.server != actor:
            self.counters["cross_thread_replies"] += 1
        self._replying[actor] = tx

    def _send_exit(self, actor, a, ts, intr):
        self._pend_send.pop(actor, None)
        tx = self._by_client.get(actor)
        if tx is None:
            self.counters["send_exit_without_txn"] += 1
            return
        tx.t_exit = ts
        tx.status = None if intr else a.get("status")
        if intr:
            tx.outcome = "interrupted"
            tx.warnings.append("MsgSendv ended with INT_CALL (interrupted)")
        elif tx.t_reply_msg is None:
            tx.outcome = "cancelled"
            tx.warnings.append("client returned without REPLY_MESSAGE/MSG_ERROR")
        self._close(tx)

    def _receive_exit(self, actor, a, ts):
        rcvid = a.get("rcvid", 0)
        if not rcvid or rcvid == 0xFFFFFFFF:
            return
        tx = self._by_rcvid.get((actor[0], rcvid))
        if tx is None:
            self.counters["receive_exit_without_txn"] += 1
            return
        if tx.server is None:
            tx.server = actor
            tx.warnings.append("REC_MESSAGE missing; server taken from MsgReceivev exit")
            self._handling[actor].append(tx)
        elif tx.server != actor:
            tx.warnings.append("MsgReceivev exit on %s, REC_MESSAGE on %s" % (actor, tx.server))
        tx.t_rx_exit = ts
        if "pid" in a:
            info = {k: a[k] for k in ("pid", "tid", "chid", "scoid", "coid", "msglen", "priority") if k in a}
            if (a["pid"], a.get("tid")) != tx.client:
                tx.info_conflict = info
                tx.warnings.append("_msg_info names %s, traced sender is %s" % ((a["pid"], a.get("tid")), tx.client))
                self.counters["info_sender_mismatch"] += 1
            else:
                tx.info = info
                self.counters["info_sender_match"] += 1

    # ---- COMM events ------------------------------------------------------
    def _comm(self, ev):
        a = unpack(ev.words, "w0:x w1:d")
        if "w1" not in a:
            self.counters["comm_events_incomplete"] += 1
            return
        actor = self._actor(ev)
        code = ev.code
        if code == COMM_SMSG:
            self._smsg(actor, a["w0"], a["w1"], ev.ts)
        elif code == COMM_RMSG:
            self._rmsg(actor, a["w0"], a["w1"], ev.ts)
        elif code in (COMM_REPLY, COMM_ERROR):
            self._reply_msg(actor, (a["w1"], a["w0"]), ev.ts, code)
        elif code == COMM_RPULSE:
            self._recv_pulse(actor, a["w1"], a["w0"], ev.ts)
        elif code == COMM_SIGNAL:
            self.signals.append((ev.ts, ev.cpu, actor, a["w0"]))
        elif not ev.partial:
            self._send_pulse(actor, code, a["w1"], a["w0"], ev)

    def _smsg(self, actor, rcvid, spid, ts):
        c = self.counters
        if actor is None:
            c["smsg_without_actor"] += 1
            return
        key = (spid, rcvid)
        old = self._by_rcvid.get(key)
        if old is not None:
            old.warnings.append("rcvid reused while still open")
            c["rcvid_reused_open"] += 1
            self._close(old)
        prev = self._by_client.get(actor)
        if prev is not None:
            prev.warnings.append("client sent again before MsgSendv returned")
            c["client_already_open"] += 1
            self._close(prev)
        tx = Txn(len(self.txns), actor, spid, rcvid, ts)
        ps = self._pend_send.pop(actor, None)
        call = self._in_call.get(actor)
        if ps is None or call is None or call.name not in SEND_CALLS:
            tx.warnings.append("no MsgSendv entry before SND_MESSAGE")
        else:
            tx.t_call, tx.coid, tx.msg = ps[0], ps[1], ps[2]
            if ps[3]:
                tx.warnings.append("MsgSendv entry before lost events")
            call.sent_msg.add(spid)
        cands = self._handling.get(actor) or []
        if len(cands) == 1:
            tx.parent = cands[0].idx
            cands[0].children.append(tx.idx)
        elif cands:
            tx.parent_candidates = [x.idx for x in cands]
            c["ambiguous_parents"] += 1
        if tx.msg:
            tx.msg_type = msg_type_name(tx.msg[0], spid == PROCNTO_PID)
        if tx.coid is not None:
            tx.path = self.coid_paths.get((actor[0], tx.coid))
        self.txns.append(tx)
        self._by_rcvid[key] = tx
        self._by_client[actor] = tx

    def _rmsg(self, actor, rcvid, spid, ts):
        tx = self._by_rcvid.get((spid, rcvid))
        if tx is None:
            self.counters["rmsg_without_txn"] += 1
            return
        if actor is None or actor[0] != spid:
            tx.warnings.append("REC_MESSAGE actor %s is not in server pid %d" % (actor, spid))
            self.counters["rmsg_actor_not_server"] += 1
            return
        if tx.server is not None and tx.server != actor:
            tx.warnings.append("received twice (%s, %s)" % (tx.server, actor))
        tx.server = actor
        tx.t_rmsg = ts
        self._handling[actor].append(tx)

    def _reply_msg(self, actor, ckey, ts, code):
        tx = self._by_client.get(ckey)
        if tx is None:
            self.counters["reply_msg_without_txn"] += 1
            return
        if actor is None or self._replying.get(actor) is not tx:
            tx.warnings.append("REPLY_MESSAGE outside the MsgReplyv/MsgError of this exchange")
            self.counters["reply_msg_outside_reply_call"] += 1
        tx.t_reply_msg = ts
        tx.outcome = "error" if code == COMM_ERROR else "replied"
        call = self._in_call.get(actor) if actor is not None else None
        if call is not None:
            call.replied.add(ckey)
        self._unindex_server(tx)

    def _send_pulse(self, actor, code, spid, scoid, ev):
        kind = "pulse" if code == COMM_SPULSE else describe(ev)[1].replace("SND_PULSE_", "pulse_").lower()
        pl = Pulse(len(self.pulses), ev.ts, None, kind, spid, scoid, cpu=ev.cpu)
        call = self._in_call.get(actor) if actor is not None else None
        if call is not None and call.name in PULSE_SENDER_CALLS.get(code, ()):
            pl.sender = actor
            call.sent_pulse.add(spid)
            keep = []
            for w in call.deferred:
                if w[3][0] == spid:
                    self.wakeups.append(w + ("pulse",))
                    self.counters["wakeups_proven"] += 1
                else:
                    keep.append(w)
            call.deferred = keep
            if call.name in PULSE_CALLS:
                pl.code, pl.value, pl.prio = call.args.get("code"), call.args.get("value"), call.args.get("priority")
        else:
            pl.context = actor
            self.counters["pulse_sender_unknown"] += 1
        self.pulses.append(pl)
        self._pulse_q[(spid, scoid)].append(pl)

    def _recv_pulse(self, actor, spid, scoid, ts):
        key = (spid, scoid)
        q = self._pulse_q.get(key)
        if not q:
            pl = Pulse(len(self.pulses), None, None, "unknown", spid, scoid, match="none")
            self.pulses.append(pl)
            self.counters["pulse_recv_without_send"] += 1
        elif len(q) == 1 and key not in self._pulse_uncertain:
            pl = q.popleft()
            pl.match, pl.candidates, pl.confidence = "unique", [pl.idx], 1.0
        else:
            cands = list(q)
            pl = q.popleft()   # queue order is a guess: pulses are delivered by priority and may be compressed
            pl.match, pl.candidates = "ambiguous", [x.idx for x in cands]
            pl.confidence = None if key in self._pulse_uncertain else round(1 / len(cands), 3)
            self._pulse_uncertain.add(key)
            self.counters["pulse_recv_ambiguous"] += 1
        if not q:
            self._pulse_uncertain.discard(key)
        pl.t_recv, pl.receiver = ts, actor

    # ---- index maintenance (by identity) ----------------------------------
    def _unindex_server(self, tx):
        key = (tx.server_pid, tx.rcvid)
        if self._by_rcvid.get(key) is tx:
            del self._by_rcvid[key]
        if tx.server is not None:
            lst = self._handling.get(tx.server)
            if lst:
                for i, x in enumerate(lst):
                    if x is tx:
                        del lst[i]
                        break

    def _close(self, tx):
        self._unindex_server(tx)
        if self._by_client.get(tx.client) is tx:
            del self._by_client[tx.client]
        if tx.replier is not None and self._replying.get(tx.replier) is tx:
            del self._replying[tx.replier]

    # ---- derived views ----------------------------------------------------
    def intervals(self, key, end_ts=None):
        """[(start, end, state code, prio)] for one thread."""
        end_ts = self.end_ts if end_ts is None else end_ts
        t = self.threads[key]
        out = []
        for i, (ts, code, prio, _cpu, _inj) in enumerate(t.states):
            nxt = t.states[i + 1][0] if i + 1 < len(t.states) else end_ts
            if nxt > ts:
                out.append((ts, nxt, code, prio))
        return out

    def fixed_priority_channels(self):
        return {k: v for k, v in self.channels.items() if v["flags"] & _NTO_CHF_FIXED_PRIORITY}
