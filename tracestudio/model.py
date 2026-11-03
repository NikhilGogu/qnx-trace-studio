# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct system state from decoded QNX kernel events.

Produces processes and threads (names, state intervals, priorities, run
masks), per-CPU context switches, wakeups with evidence, interrupts, pulses,
and message-passing transactions (MsgSend -> MsgReceive -> MsgReply -> client
unblock).

Linking rules, all from kernel events, none from message payloads:
  * A kernel call or COMM event belongs to the thread running on its CPU (the
    last non-injected THRUNNING there). This is an inference: inside a loss
    window of that CPU, after a migration or at trace start the CPU's thread
    is UNKNOWN and its events are counted as actorless and not linked. Partial
    combine events are counted and not linked; a partial thread event sets
    the thread UNKNOWN and never restores a CPU's occupant.
  * SND_MESSAGE(rcvid, server pid) inside the sender's MsgSendv opens a
    transaction keyed by (server pid, rcvid). An rcvid names one pending
    message, so the key is unique while the client is blocked and is reused
    after. Every index (by client, by rcvid, received-not-replied per server
    thread, replying) is cleaned by object identity, and a REPLY_MESSAGE that
    does not belong to the reply call in progress is quarantined before it can
    touch any transaction.
  * REC_MESSAGE(rcvid, pid) names the receiving thread, MsgReceivev exit adds
    _msg_info (wide mode); an _msg_info that names another sender is kept
    aside and marks the transaction untrustworthy. MsgReplyv/MsgError entry
    with the rcvid and REPLY_MESSAGE/MSG_ERROR(tid, pid) close the server side;
    MsgSendv exit (or INT_CALL) on the client ends it.
  * A request sent by a thread that has exactly one received, unreplied
    request is its child. With several the parent is ambiguous: candidates
    are recorded and no parent is set; such a request still counts for
    latency but not for critical paths or inversion attribution.
  * Lost events (kev.Loss) define loss windows. Inside a window of its CPU no
    event has a known actor and no switch is trusted; every thread that could
    have changed state is UNKNOWN from the window's start until its first
    event after the window ends (an event inside a window holds only at its
    instant); open exchanges are flagged; run masks, coid paths, kernel calls
    in progress and pulse queue synchronisation are dropped.
  * A wakeup is attributed only with object evidence: the woken thread later
    receives the very message or pulse sent in the waker's call, or the reply
    in the waker's MsgReplyv/MsgError names it.
  * Pulses: a sender is known only for a pulse emitted inside the sending
    thread's MsgSendPulse (or MsgDeliverEvent, ConnectDetach for the kernel
    pulses those calls generate). A receive matches a send only when exactly
    one pulse is queued for that (server pid, scoid) and the queue is in sync:
    queues start unsynchronised, lose sync at every loss and after every
    ambiguous receive, and regain it when a receiving thread blocks in RECEIVE
    on the channel the pulses arrive on (so nothing is pending there).
  * Paths: a coid gets a path only when the PATHMGR event of the opening
    thread is followed by a ConnectAttach within 10 ms and the first message on
    the new coid is an _IO_CONNECT that the server answers without error.
  * Message names: _IO_* names are given only for resource-manager
    connections (procnto, a coid confirmed by an _IO_CONNECT, a channel that
    answered an _IO_CONNECT, or the answered _IO_CONNECT itself); otherwise
    the message is described by its type number.
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
ST_DESTROY = 25
ST_MUTEX = 13
SYNC_OWNER_MASK = 0x7FFFFFFF      # _NTO_SYNC_OWNER_MASK; the top bit is _NTO_SYNC_WAITING
NOT_BLOCKED = (None, ST_UNKNOWN, ST_RUNNING, ST_READY)
BLOCKED_IPC = {"SEND", "RECEIVE", "REPLY"}
UNKNOWN = (-1, 0)          # pseudo thread: what ran on a CPU is not known
PROCNTO_PID = 1
INF = float("inf")
_NTO_CHF_FIXED_PRIORITY = 0x1
_NTO_CHF_INHERIT_RUNMASK = 0x4000
_NTO_TCTL_RUNMASK = 4
_RUNMASK_BY_POINTER = (6, 7, 10)   # _GET_AND_SET, _GET_AND_SET_64, _GET_AND_SET_INHERIT
SCHED_SPORADIC = 4
SEND_CALLS = ("MSG_SENDV", "MSG_SENDVNC")
REPLY_CALLS = ("MSG_REPLYV", "MSG_ERROR")
RECEIVE_CALLS = ("MSG_RECEIVEV", "MSG_RECEIVEPULSEV")
PULSE_CALLS = ("MSG_SEND_PULSE", "MSG_SEND_PULSEPTR")
# kernel call of the running thread that proves who sent a pulse of this kind
PULSE_SENDER_CALLS = {COMM_SPULSE: PULSE_CALLS, COMM_SPULSE_EXE: ("MSG_DELIVER_EVENT",),
                      COMM_SPULSE_DIS: ("CONNECT_DETACH",)}
APS_SYSTEM_CODES = {3, 4, 5, 22, 23, 24, 25, 26, 27}
IO_CONNECT = 0x100


@dataclass
class Thread:
    pid: int
    tid: int
    name: str = ""
    states: list = field(default_factory=list)    # (ts, state code, prio or None, cpu, injected, policy or None)
    prios: list = field(default_factory=list)     # (ts, prio) on change
    runmask: list = field(default_factory=list)   # (ts, mask or None) from ThreadCtl; None: not known

    @property
    def key(self):
        return (self.pid, self.tid)


@dataclass
class Process:
    pid: int
    ppid: int = 0
    path: str = ""
    destroyed_ts: int | None = None
    created_ts: int | None = None       # PROCCREATE seen in the trace: every thread of it is traced

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
    msg_type: str | None = None          # _IO_* or system message name, when the protocol is known or a candidate
    msg_candidate: bool = False          # msg_type rests on the _IO_CONNECT heuristic, not on the server's identity
    msg_desc: str | None = None          # "type 0x...." when it is not
    rm_conn: bool = False                # the coid answered an _IO_CONNECT (candidate resource-manager connection)
    rm_chan: bool = False                # its channel had answered an _IO_CONNECT before this receive (candidate)
    path: str | None = None              # candidate path of the coid (PATHMGR, ConnectAttach, answered _IO_CONNECT)
    open_path: str | None = None         # PATHMGR path waiting for this _IO_CONNECT's answer
    t_call: int | None = None
    client_block: str | None = None      # SEND or REPLY: first blocked state after the send
    client_prio: int | None = None
    server: tuple | None = None
    t_rmsg: int | None = None
    t_rx_call: int | None = None          # entry of the MsgReceivev call that returned this request
    t_rx_exit: int | None = None
    rx_chid: int | None = None           # channel the receiving thread was receiving on
    info: dict = field(default_factory=dict)
    info_conflict: dict | None = None    # _msg_info that names another sender (quarantined)
    replier: tuple | None = None
    t_reply_call: int | None = None
    t_reply_msg: int | None = None
    t_reply_exit: int | None = None
    t_unblock: int | None = None          # client THREADY after the reply
    t_exit: int | None = None
    status: int | None = None
    errno: int | None = None              # errno of a failed MsgSendv
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

    @property
    def causal(self):
        """Usable for causal analyses (critical paths, inversion attribution): trustworthy, parent not ambiguous."""
        return self.trustworthy and not self.parent_candidates

    @property
    def msg_label(self):
        """Message name for display: "_IO_READ", "_IO_READ (candidate)", or ""."""
        if not self.msg_type:
            return ""
        return self.msg_type + (" (candidate)" if self.msg_candidate else "")


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
    match: str | None = None        # unique, ambiguous, unsynced, none (no traced send), unseen; None: not received
    candidates: list = field(default_factory=list)


@dataclass(eq=False)
class MutexWait:
    """One SyncMutexLock call that blocked. The owner is known only when the owner's own SyncMutexUnlock of
    the same mutex (same process, same address) carried the owner value the waiter's call saw, with no lost
    events in between: then `owner` owned the mutex from t_call to t_release."""
    idx: int
    waiter: tuple
    pid: int
    sync: int
    owner_value: int | None         # owner word read by the waiter's call, without _NTO_SYNC_WAITING
    t_call: int
    t_block: int | None = None      # waiter entered MUTEX
    t_release: int | None = None    # the owner's SyncMutexUnlock entry
    t_exit: int | None = None       # waiter's SyncMutexLock exit
    ret: int | None = None
    errno: int | None = None
    owner: tuple | None = None
    unknown: str | None = None      # why the owner is not known, once that is decided

    @property
    def owner_known(self):
        return self.owner is not None and self.unknown is None


@dataclass(eq=False)
class Call:
    name: str
    ts: int
    cpu: int
    args: dict
    sent: list = field(default_factory=list)        # (server pid, rcvid) of SND_MESSAGE in this call
    pulses: list = field(default_factory=list)      # Pulse objects sent in this call (proven sender)
    replied: set = field(default_factory=set)       # client keys named by REPLY_MESSAGE in this call
    ready: list = field(default_factory=list)       # (ts, cpu, woken, prio) READY from RECEIVE in this call


class Model:
    def __init__(self, kev: KevFile):
        self.kev = kev
        self.processes: dict[int, Process] = {}
        self.threads: dict[tuple, Thread] = {}
        self.switches = []      # (ts, cpu, prev key|None|UNKNOWN, prev state, prev prio, next key|UNKNOWN, next prio)
        self.wakeups = []       # (ts, cpu, waker key, woken key, prio, evidence)
        self.interrupts = []    # (ts, cpu, intr_id)
        self.txns: list[Txn] = []
        self.pulses: list[Pulse] = []
        self.channels = {}      # (pid, chid) -> dict(flags, tid, ts): every ChannelCreate in the trace
        self.connections = {}   # (pid, coid) -> (server pid, chid)
        self.coid_paths = {}    # (pid, coid) -> path, confirmed opens only
        self.signals = []
        self.mutex_waits: list[MutexWait] = []
        self.gaps = []          # kev.Loss records applied, in time order
        self.loss_windows = {}  # cpu -> [(t0, t1)]
        self.counters = defaultdict(int)
        self.wide = None        # True when thread events carry priorities
        self.aps_seen = False   # adaptive partitioning events in the trace
        self.injected_pids = set()
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
        self._pulse_synced = set()    # pulse queues known to be in step with the channel
        self._scoid_chan = {}         # (server pid, scoid) -> chid the pulses are received on
        self._last_path = {}          # thread key -> (ts, path) from the latest PATHMGR event
        self._coid_candidate = {}     # (pid, coid) -> path waiting for its _IO_CONNECT
        self._rm_coids = set()        # (pid, coid) confirmed resource-manager connections
        self._rm_channels = set()     # (server pid, chid) that answered an _IO_CONNECT
        self._chan_flags = {}         # (pid, chid) -> flags of a channel still known to be that channel
        self._uncertain = {}          # thread key -> end of the loss window its state depends on
        self._wake_msg = {}           # woken key -> (READY, Txn) waiting for that Txn's REC_MESSAGE
        self._wake_pulse = {}         # woken key -> (READY, Pulse) waiting for its REC_PULSE
        self._mutex_call = {}         # waiter key -> MutexWait of its SyncMutexLock in progress
        self._mutex_held = defaultdict(list)   # (pid, sync address) -> [MutexWait] not yet released
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

    def in_loss(self, cpu, ts):
        for a, b in self.loss_windows.get(cpu, ()):
            if a <= ts < b:
                return True
        return False

    def loss_end_at(self, ts):
        """Latest end of the loss windows (any CPU) that contain ts, or None."""
        end = None
        for lst in self.loss_windows.values():
            for a, b in lst:
                if a <= ts < b and (end is None or b > end):
                    end = b
        return end

    # ---- main pass --------------------------------------------------------
    def _run(self, evs):
        gaps = []
        for l in self.kev.losses:
            t0 = l.t0 if l.t0 is not None else self.start_ts - 1
            t1 = l.t1 if l.t1 is not None else INF
            gaps.append((t0, t1, l))
            self.loss_windows.setdefault(l.cpu, []).append((t0, t1))
        gaps.sort(key=lambda g: g[0])
        gi = 0
        for ev in evs:
            while gi < len(gaps) and gaps[gi][0] < ev.ts:
                self._gap(*gaps[gi])
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
            elif cls == C_SYSTEM:
                base = ev.code & 0x1FF
                if base in APS_SYSTEM_CODES:
                    self.aps_seen = True
                elif base == 2 and not ev.partial:   # PATHMGR
                    a = unpack(ev.words, "pid:d tid:d path:s")
                    if "path" in a:
                        self._last_path[(a["pid"], a["tid"])] = (ev.ts, a["path"])
        for g in gaps[gi:]:
            self._gap(*g)
        self._finish()

    def _finish(self):
        c = self.counters
        for tx in {id(t): t for t in list(self._by_client.values()) + list(self._by_rcvid.values())}.values():
            tx.warnings.append("still open at end of trace")
        # an exchange whose lifetime overlaps any loss window may miss events on that CPU
        for tx in self.txns:
            a = tx.t_call if tx.t_call is not None else tx.t_smsg
            b = tx.t_exit if tx.t_exit is not None else self.end_ts
            for cpu, lst in self.loss_windows.items():
                if any(w0 < b and a < w1 for w0, w1 in lst):
                    note = "spans lost events on cpu %d" % cpu
                    if note not in tx.warnings:
                        tx.warnings.append(note)
        c["wakeups_without_evidence"] += len(self._wake_msg) + len(self._wake_pulse)
        for w in self._mutex_call.values():          # still blocked when the trace ended
            if w.t_block is not None:
                w.idx = len(self.mutex_waits)
                self.mutex_waits.append(w)
                if w.t_release is None:
                    w.unknown = w.unknown or "still waiting at the end of the trace"
        c["mutex_waits"] = len(self.mutex_waits)
        c["mutex_waits_owner_known"] = sum(1 for w in self.mutex_waits if w.owner_known)
        self._name_messages()
        c["txns"] = len(self.txns)
        c["txns_complete"] = sum(1 for t in self.txns if t.complete)
        c["txns_trustworthy"] = sum(1 for t in self.txns if t.trustworthy)

    def _name_messages(self):
        for tx in self.txns:
            if not tx.msg:
                continue
            m0 = tx.msg[0]
            t = m0 & 0xFFFF
            name = msg_type_name(m0, True)
            to_procnto = tx.server_pid == PROCNTO_PID
            answered_connect = t == IO_CONNECT and tx.outcome == "replied" and (tx.status or 0) >= 0
            # procnto speaks only system and I/O protocols; for any other server an answered _IO_CONNECT
            # (type 0x100) could be an application message, so names that rest on it are candidates
            candidate = not to_procnto and (tx.rm_conn or tx.rm_chan or answered_connect)
            if name and (to_procnto or (candidate and t >= 0x100)):
                tx.msg_type = name
                tx.msg_candidate = candidate
                if candidate:
                    self.counters["io_names_candidate"] += 1
            else:
                tx.msg_desc = "type 0x%04x" % t
                if name and t >= 0x100:
                    tx.msg_desc += " (%s on a resource-manager connection)" % name
                    self.counters["io_names_not_qualified"] += 1

    def _actor(self, ev):
        if ev.partial:
            self.counters["partial_events_not_linked"] += 1
            return None
        if self.loss_windows and self.in_loss(ev.cpu, ev.ts):
            self.counters["events_in_loss_window"] += 1
            return None
        k = self._running.get(ev.cpu)
        if k is None or k == UNKNOWN:
            self.counters["events_without_actor"] += 1
            return None
        if self.state_at(k) != ST_RUNNING:
            self.counters["actor_not_running"] += 1
        return k

    # ---- lost events ------------------------------------------------------
    def _gap(self, t0, t1, loss):
        cpu = loss.cpu
        self.gaps.append(loss)
        self.counters["lost_event_gaps"] += 1
        old = self._running.get(cpu)
        if old != UNKNOWN:
            self.switches.append((t0, cpu, old, self.state_at(old) if old else None,
                                  self.prio_of(old) if old else None, UNKNOWN, None))
            self._running[cpu] = UNKNOWN
        note = "spans lost events on cpu %d" % cpu
        open_txns = {id(t): t for t in list(self._by_client.values()) + list(self._by_rcvid.values())
                     + list(self._replying.values()) + [x for lst in self._handling.values() for x in lst]}
        for tx in open_txns.values():
            if note not in tx.warnings:
                tx.warnings.append(note)
        for ps in self._pend_send.values():
            ps[3] = True
        for key in list(self._in_call):     # the rest of any call in progress may be in the lost events
            self._end_call(key)
        self._pulse_synced.clear()
        self._scoid_chan.clear()      # a channel may have been destroyed and its chid reused unseen
        self._chan_flags.clear()
        self._rm_channels.clear()
        self.coid_paths.clear()
        self._coid_candidate.clear()
        self._rm_coids.clear()
        self._last_path.clear()
        for lst in self._mutex_held.values():
            for w in lst:
                w.unknown = w.unknown or "events lost during the wait"
        self._mutex_held.clear()
        self.counters["wakeups_without_evidence"] += len(self._wake_msg) + len(self._wake_pulse)
        self._wake_msg.clear()
        self._wake_pulse.clear()
        for key, t in self.threads.items():
            if self.is_idle(key):
                continue
            if t.runmask and t.runmask[-1][1] is not None:
                t.runmask.append((t0, None))
            if t.states and t.states[-1][1] != ST_UNKNOWN:
                t.states.append((t0, ST_UNKNOWN, None, cpu, False, None))
            if t.states:
                self._uncertain[key] = max(self._uncertain.get(key, t1), t1)

    # ---- thread and process events ----------------------------------------
    def _state(self, ev):
        c = self.counters
        a = unpack(ev.words, THREAD_WIDE)
        pid, tid = a.get("pid"), a.get("tid")
        if pid is None or tid is None:
            c["state_events_incomplete"] += 1
            return
        key = (pid, tid)
        t = self.thread(pid, tid)
        if ev.injected:
            self.injected_pids.add(pid)
        if ev.partial:   # slots missing: nothing beyond "some event happened" is known
            c["partial_state_events"] += 1
            self._drop_wake(key)
            if not t.states or t.states[-1][1] != ST_UNKNOWN:
                t.states.append((ev.ts, ST_UNKNOWN, None, ev.cpu, ev.injected, None))
            old = self._running.get(ev.cpu)
            if not ev.injected and (ev.code == ST_RUNNING or old == key) and old != UNKNOWN:
                self.switches.append((ev.ts, ev.cpu, old, self.state_at(old) if old else None,
                                      self.prio_of(old) if old else None, UNKNOWN, None))
                self._running[ev.cpu] = UNKNOWN
            return
        if self.wide is None:
            self.wide = "priority" in a
        prio, policy = a.get("priority"), a.get("policy")
        prev_state = t.states[-1][1] if t.states else None
        prio_before = self.prio_of(key)
        code = ev.code
        until = self._uncertain.get(key)
        w = self.loss_end_at(ev.ts) if self.loss_windows else None
        if w is not None:
            until = max(until or w, w)
            self._uncertain[key] = until
        uncertain = until is not None and ev.ts < until
        if until is not None and not uncertain:
            del self._uncertain[key]
        t.states.append((ev.ts, code, prio, ev.cpu, ev.injected, policy))
        if uncertain:    # holds at this instant only: lost events may change it before the window ends
            t.states.append((ev.ts, ST_UNKNOWN, None, ev.cpu, False, None))
        if prio is not None and (not t.prios or t.prios[-1][1] != prio):
            t.prios.append((ev.ts, prio))
        if ev.injected:
            return
        if code == ST_RUNNING:
            if uncertain:
                self._drop_wake(key)
            if self.loss_windows and self.in_loss(ev.cpu, ev.ts):
                c["switches_in_loss_window"] += 1
                return
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
        if uncertain or code not in (ST_READY, ST_RUNNING):
            self._drop_wake(key)      # a wake is proven only by a receive in the same READY/RUNNING stretch
        if code == ST_READY and prev_state not in NOT_BLOCKED:
            if uncertain:
                c["wakeups_without_evidence"] += 1
            else:
                self._wakeup(ev, key, prev_state, prio)
        tx = self._by_client.get(key)
        if tx is not None:
            if tx.client_block is None and code in (ST_SEND, ST_REPLY):
                tx.client_block = state_name(code)
            if code == ST_READY and tx.t_reply_msg is not None and tx.t_unblock is None:
                tx.t_unblock = ev.ts
        if code == ST_MUTEX:
            w = self._mutex_call.get(key)
            if w is not None and w.t_block is None:
                w.t_block = ev.ts
        if code == ST_RECEIVE and not uncertain:
            self._resync_pulses(key)
        if code == ST_DEAD:
            self._thread_gone(key, ev.ts, "died")

    def _wakeup(self, ev, woken, prev_state, prio):
        k = self._running.get(ev.cpu)
        call = self._in_call.get(k) if k not in (None, UNKNOWN) else None
        if call is None or k == woken:
            self.counters["wakeups_without_evidence"] += 1
            return
        if prev_state in (ST_REPLY, ST_SEND) and woken in call.replied:
            self.wakeups.append((ev.ts, ev.cpu, k, woken, prio, "reply names it"))
            self.counters["wakeups_with_evidence"] += 1
        elif prev_state == ST_RECEIVE and call.name in SEND_CALLS:
            sent = [x for x in call.sent if x[0] == woken[0]]
            tx = self._by_rcvid.get(sent[0]) if len(sent) == 1 else None
            if tx is not None:   # confirmed when this thread receives that very message
                self._wake_msg[woken] = ((ev.ts, ev.cpu, k, woken, prio), tx)
            else:
                self.counters["wakeups_without_evidence"] += 1
        elif prev_state == ST_RECEIVE and any(call.name in names for names in PULSE_SENDER_CALLS.values()):
            sent = [p for p in call.pulses if p.server_pid == woken[0]]
            if len(sent) == 1:
                self._wake_pulse[woken] = ((ev.ts, ev.cpu, k, woken, prio), sent[0])
            elif not sent:   # inside MsgSendPulse the READY comes before SND_PULSE
                call.ready.append((ev.ts, ev.cpu, k, woken, prio))
            else:
                self.counters["wakeups_without_evidence"] += 1
        else:
            self.counters["wakeups_without_evidence"] += 1

    def _drop_wake(self, key):
        n = (self._wake_msg.pop(key, None) is not None) + (self._wake_pulse.pop(key, None) is not None)
        self.counters["wakeups_without_evidence"] += n

    def _forget_channels(self, pid):
        """Channel identities of a process that destroyed a channel (its chid is not traced) or ended."""
        for d in (self._chan_flags, self._scoid_chan):
            for k in [k for k in d if k[0] == pid]:
                del d[k]
        self._rm_channels = {k for k in self._rm_channels if k[0] != pid}
        self._pulse_synced = {k for k in self._pulse_synced if k[0] != pid}

    def _end_call(self, key):
        call = self._in_call.pop(key, None)
        if call is not None and call.ready:
            self.counters["wakeups_without_evidence"] += len(call.ready)
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
                if p.created_ts is None and not ev.injected:
                    p.created_ts = ev.ts
        elif ev.code == PR_PROCDESTROY:
            a = unpack(ev.words, "ppid:d pid:d")
            pid = a.get("pid")
            if pid is not None:
                self.process(pid).destroyed_ts = ev.ts
                self._forget_channels(pid)
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
            elif name == "CHANNEL_DESTROY":
                self._forget_channels(actor[0])
            elif name == "SYNC_MUTEX_LOCK" and "sync_p" in a:
                own = a.get("owner")
                w = MutexWait(len(self.mutex_waits), actor, actor[0], a["sync_p"],
                              None if own is None else own & SYNC_OWNER_MASK, ev.ts)
                self._mutex_call[actor] = w
                self._mutex_held[(actor[0], a["sync_p"])].append(w)
            elif name == "SYNC_MUTEX_UNLOCK" and "sync_p" in a:
                own = a.get("owner")
                for w in self._mutex_held.pop((actor[0], a["sync_p"]), []):
                    if w.t_release is not None:
                        continue
                    w.t_release = ev.ts
                    if own is None or w.owner_value is None:
                        w.unknown = w.unknown or "owner value not traced"
                    elif own & SYNC_OWNER_MASK != w.owner_value:
                        w.unknown = w.unknown or "unlocked by %s with owner value 0x%x, the waiter saw 0x%x" % (
                            self.label(actor), own & SYNC_OWNER_MASK, w.owner_value)
                    else:
                        w.owner = actor
            elif name == "CONNECT_DETACH" and "coid" in a:
                k = (actor[0], a["coid"])
                self.coid_paths.pop(k, None)
                self._coid_candidate.pop(k, None)
                self._rm_coids.discard(k)
            return
        call = self._end_call(actor)
        if call is None or call.name != name:
            c["call_exit_without_entry"] += 1
            call = None
        if intr:
            c["interrupted_calls"] += 1
        if name in SEND_CALLS:
            self._send_exit(actor, a, ev.ts, intr)
        elif name in RECEIVE_CALLS:
            if not intr:
                self._receive_exit(actor, a, ev.ts, call)
        elif name in REPLY_CALLS:
            tx = self._replying.pop(actor, None)
            if tx is not None:
                tx.t_reply_exit = ev.ts
        elif name == "SYNC_MUTEX_LOCK":
            w = self._mutex_call.pop(actor, None)
            if w is not None:
                w.t_exit, w.ret, w.errno = ev.ts, None if intr else a.get("ret_val", 0), a.get("errno")
                if w.t_block is not None:
                    self.mutex_waits.append(w)
                    w.idx = len(self.mutex_waits) - 1
                    if w.t_release is None:
                        w.unknown = w.unknown or "no SyncMutexUnlock of this mutex seen during the wait"
                lst = self._mutex_held.get((w.pid, w.sync))
                if lst and w in lst:
                    lst.remove(w)
        elif call is None or intr:
            return
        elif name == "CONNECT_ATTACH":
            coid = a.get("coid")
            if coid is not None and coid != 0xFFFFFFFF:
                k = (actor[0], coid)
                self.connections[k] = (call.args.get("pid"), call.args.get("chid"))
                self.coid_paths.pop(k, None)
                self._rm_coids.discard(k)
                lp = self._last_path.pop(actor, None)
                if lp and ev.ts - lp[0] <= self._path_window:
                    self._coid_candidate[k] = lp[1]
                else:
                    self._coid_candidate.pop(k, None)
        elif name == "CHANNEL_CREATE":
            chid = a.get("chid", -1)
            if chid >= 0:
                self.channels[(actor[0], chid)] = {"flags": call.args.get("flags", 0), "tid": actor[1], "ts": ev.ts}
                self._chan_flags[(actor[0], chid)] = call.args.get("flags", 0)
                self._rm_channels.discard((actor[0], chid))
        elif name in ("THREAD_CTL", "THREAD_CTLEXT"):
            if a.get("ret_val", 0) != 0:
                return
            cmd = call.args.get("cmd")
            target = actor
            if name == "THREAD_CTLEXT":
                if "pid" not in call.args:
                    return
                target = (call.args["pid"] or actor[0], call.args.get("tid") or actor[1])
            if cmd == _NTO_TCTL_RUNMASK and "data" in call.args:
                self.thread(*target).runmask.append((call.ts, call.args["data"]))
            elif cmd in _RUNMASK_BY_POINTER:
                self.thread(*target).runmask.append((call.ts, None))

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
        tx.status = None if intr else a.get("status", a.get("ret_val"))
        tx.errno = None if intr else a.get("errno")
        if intr:
            tx.outcome = "interrupted"
            tx.warnings.append("MsgSendv ended with INT_CALL (interrupted)")
        elif tx.t_reply_msg is None:
            tx.outcome = "cancelled"
            tx.warnings.append("client returned without REPLY_MESSAGE/MSG_ERROR")
        if tx.open_path and tx.outcome == "replied" and (tx.status is None or tx.status >= 0):
            # an answered _IO_CONNECT on a new coid: probably the open of this path (a candidate: nothing in the
            # trace ties the PATHMGR lookup to this connection)
            k = (actor[0], tx.coid)
            self.coid_paths[k] = tx.path = tx.open_path
            self._rm_coids.add(k)
        if tx.msg and tx.msg[0] & 0xFFFF == IO_CONNECT and tx.outcome == "replied" and tx.info.get("chid") is not None:
            self._rm_channels.add((tx.server_pid, tx.info["chid"]))
        self._close(tx)

    def _receive_exit(self, actor, a, ts, call):
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
        if call is not None:
            tx.t_rx_call = call.ts
            if call.args.get("chid") is not None:
                tx.rx_chid = call.args["chid"]
        if a.get("pid"):   # a server that passes no _msg_info buffer gets zeros: nothing to compare
            info = {k: a[k] for k in ("pid", "tid", "chid", "scoid", "coid", "msglen", "priority") if k in a}
            if (a["pid"], a.get("tid")) != tx.client:
                tx.info_conflict = info
                tx.warnings.append("_msg_info names %s, traced sender is %s" % ((a["pid"], a.get("tid")), tx.client))
                self.counters["info_sender_mismatch"] += 1
            else:
                tx.info = info
                self.counters["info_sender_match"] += 1
        chid = tx.info.get("chid", tx.rx_chid)
        tx.rm_chan = (actor[0], chid) in self._rm_channels
        flags = self._chan_flags.get((actor[0], chid)) if chid is not None else None
        if flags is None or flags & _NTO_CHF_INHERIT_RUNMASK:
            # the receiver may have taken the client's run mask (_NTO_CHF_INHERIT_RUNMASK, or flags not traced)
            t = self.threads.get(actor)
            if t is not None and t.runmask and t.runmask[-1][1] is not None:
                t.runmask.append((ts, None))
                self.counters["run_masks_dropped_on_receive"] += 1

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
            call.sent.append(key)
        tx.client_prio = self.prio_of(actor)
        cands = self._handling.get(actor) or []
        if len(cands) == 1:
            tx.parent = cands[0].idx
            cands[0].children.append(tx.idx)
        elif cands:   # kept for latency; left out of causal analyses (see Txn.causal)
            tx.parent_candidates = [x.idx for x in cands]
            c["ambiguous_parents"] += 1
        if tx.coid is not None:
            k = (actor[0], tx.coid)
            cand = self._coid_candidate.pop(k, None)
            if cand is not None and tx.msg and tx.msg[0] & 0xFFFF == IO_CONNECT:
                tx.open_path = cand
            tx.path = self.coid_paths.get(k)
            tx.rm_conn = k in self._rm_coids
        self.txns.append(tx)
        self._by_rcvid[key] = tx
        self._by_client[actor] = tx

    def _rmsg(self, actor, rcvid, spid, ts):
        key = (spid, rcvid)
        tx = self._by_rcvid.get(key)
        w = self._wake_msg.pop(actor, None) if actor is not None else None
        if w is not None:
            if w[1] is tx:
                self.wakeups.append(w[0] + ("received the message",))
                self.counters["wakeups_with_evidence"] += 1
            else:
                self.counters["wakeups_without_evidence"] += 1
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
            # not the reply call in progress for this client's exchange (a stale or misattributed reply):
            # leave the exchange as it is, only mark the contradiction
            note = "REPLY_MESSAGE outside the reply call of this exchange (quarantined)"
            if note not in tx.warnings:
                tx.warnings.append(note)
            self.counters["reply_msg_quarantined"] += 1
            return
        tx.t_reply_msg = ts
        tx.outcome = "error" if code == COMM_ERROR else "replied"
        call = self._in_call.get(actor)
        if call is not None:
            call.replied.add(ckey)
        self._unindex_server(tx)

    def _send_pulse(self, actor, code, spid, scoid, ev):
        kind = "pulse" if code == COMM_SPULSE else describe(ev)[1].replace("SND_PULSE_", "pulse_").lower()
        pl = Pulse(len(self.pulses), ev.ts, None, kind, spid, scoid, cpu=ev.cpu)
        call = self._in_call.get(actor) if actor is not None else None
        if call is not None and call.name in PULSE_SENDER_CALLS.get(code, ()):
            pl.sender = actor
            call.pulses.append(pl)
            keep = []
            for w in call.ready:     # READYs seen earlier in this call wait for their REC_PULSE
                if w[2] != w[3] and w[3][0] == spid:
                    self._wake_pulse[w[3]] = (w, pl)
                else:
                    keep.append(w)
            call.ready = keep
            if call.name in PULSE_CALLS:
                pl.code, pl.value, pl.prio = call.args.get("code"), call.args.get("value"), call.args.get("priority")
        else:
            pl.context = actor
            self.counters["pulse_sender_unknown"] += 1
        self.pulses.append(pl)
        self._pulse_q[(spid, scoid)].append(pl)

    def _recv_pulse(self, actor, spid, scoid, ts):
        key = (spid, scoid)
        call = self._in_call.get(actor) if actor is not None else None
        if call is not None and call.name in RECEIVE_CALLS and call.args.get("chid") is not None:
            self._scoid_chan[key] = call.args["chid"]
        q = self._pulse_q.get(key)
        if not q:
            pl = Pulse(len(self.pulses), None, None, "unknown", spid, scoid, match="none")
            self.pulses.append(pl)
            self.counters["pulse_recv_without_send"] += 1
            self._pulse_synced.discard(key)
        elif len(q) == 1 and key in self._pulse_synced:
            pl = q.popleft()
            pl.match, pl.candidates = "unique", [pl.idx]
        else:
            cands = list(q)
            pl = q.popleft()   # queue order is a guess: pulses are delivered by priority and may be compressed
            pl.match, pl.candidates = ("ambiguous" if len(cands) > 1 else "unsynced"), [x.idx for x in cands]
            self._pulse_synced.discard(key)
            self.counters["pulse_recv_ambiguous" if len(cands) > 1 else "pulse_recv_unsynced"] += 1
        pl.t_recv, pl.receiver = ts, actor
        wp = self._wake_pulse.pop(actor, None) if actor is not None else None
        if wp is not None:
            if wp[1] is pl and pl.match == "unique":
                self.wakeups.append(wp[0] + ("received the pulse",))
                self.counters["wakeups_with_evidence"] += 1
            else:
                self.counters["wakeups_without_evidence"] += 1

    def _resync_pulses(self, key):
        """The thread blocked in MsgReceive on a channel: nothing is pending there, so the queues of the
        pulses known to arrive on that channel are empty and in sync again."""
        call = self._in_call.get(key)
        if call is None or call.name not in RECEIVE_CALLS or call.args.get("chid") is None:
            return
        chid = call.args["chid"]
        for k, ch in self._scoid_chan.items():
            if k[0] == key[0] and ch == chid:
                q = self._pulse_q.get(k)
                while q:
                    q.popleft().match = "unseen"     # its receive is not in the trace (lost or merged)
                    self.counters["pulse_receive_unseen"] += 1
                self._pulse_synced.add(k)

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
        sts = self.threads[key].states
        out = []
        for i, s in enumerate(sts):
            nxt = sts[i + 1][0] if i + 1 < len(sts) else end_ts
            if nxt > s[0]:
                out.append((s[0], nxt, s[1], s[2]))
        return out

    def fixed_priority_channels(self):
        return {k: v for k, v in self.channels.items() if v["flags"] & _NTO_CHF_FIXED_PRIORITY}
