# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Write a Perfetto protobuf trace (TracePacket stream) from a Model.

Mapping:
  * context switches -> ftrace sched_switch per CPU, so Perfetto's CPU tracks,
    sched and thread_state tables work natively. procnto's idle threads
    become pid 0 (swapper). CPU time nobody can be credited with (after lost
    events, or on the old CPU of a thread that migrated before that CPU
    switched) goes to a pseudo thread "UNKNOWN" instead of idle.
  * proven wakeups only (see model.py) -> sched_waking with the waker as the
    emitting thread; other READY transitions get no waker.
  * priorities are QNX priorities (0-255, higher wins), never converted: they
    appear as "QNX priority" counter tracks, "qnx_prio" arguments, and in the
    priority column of Perfetto's sched table, which here holds the QNX value,
    not a Linux prio.
  * processes and threads -> ProcessTree. Perfetto tids are global, QNX tids
    are per process, so thread (pid, tid) becomes pid*100+tid when that fits
    in 31 bits (readable: 109365703 = pid 1093657 tid 3), else a counter.
  * exact QNX thread states (SEND, RECEIVE, REPLY, MUTEX, ..., UNKNOWN for
    lost data) -> slices on a "<thread> QNX state" child track of each thread.
  * message passing -> slices on the thread tracks: client "MsgSend <type> ->
    <server>", server "handle <client>", "MsgReply"/"MsgError", and a client
    "reply received" slice; flow arrows send->handle and reply->received carry
    the transaction id on both ends. Slices of one thread that would cross
    (deferred replies, overlapping handling) go to extra "(overlap n)" tracks
    so nesting stays correct.
  * critical paths of trustworthy top-level requests -> "<thread> critical
    path" track (see critical.py).
  * pulses -> instants; a flow arrow only for a unique send/receive match.
  * interrupts -> instants on per-CPU "interrupts" tracks; lost events ->
    slices on a "lost events" track.
Slices are written per track in nesting order (parents begin first, a
zero-length slice ends right after it begins), and timestamps are converted
with one monotonic rounding, so no slice can end before it begins.
"""
from __future__ import annotations

from collections import defaultdict

from .events import state_name
from .model import ST_UNKNOWN, UNKNOWN

try:
    from perfetto.protos.perfetto.trace import perfetto_trace_pb2 as pb
except ImportError as e:  # pragma: no cover
    raise SystemExit("trace-studio needs the 'perfetto' Python package (pip install perfetto protobuf): %s" % e)

SEQ = 7351
_RUNNABLE, _BLOCKED, _STOPPED, _DEAD = 0, 1, 4, 16
UNKNOWN_TID = (1 << 31) - 2


def linux_state(code):
    """Linux prev_state for Perfetto's thread_state; the QNX state track has the exact state."""
    if code is None or code == ST_UNKNOWN:
        return _RUNNABLE
    n = state_name(code)
    if n in ("RUNNING", "READY"):
        return _RUNNABLE
    if n == "STOPPED":
        return _STOPPED
    if n in ("DEAD", "DESTROY"):
        return _DEAD
    return _BLOCKED


def assign_lanes(items):
    """Give each [t0, t1, ...] item a lane so that items in one lane are nested or disjoint."""
    order = sorted(range(len(items)), key=lambda i: (items[i][0], -(items[i][1] - items[i][0]), i))
    lanes, out = [], [0] * len(items)
    for i in order:
        t0, t1 = items[i][0], items[i][1]
        for li, stack in enumerate(lanes):
            while stack and stack[-1] <= t0:
                stack.pop()
            if not stack or t1 <= stack[-1]:
                stack.append(t1)
                out[i] = li
                break
        else:
            lanes.append([t1])
            out[i] = len(lanes) - 1
    return out


class TidMap:
    def __init__(self, model):
        self.m = model
        self.map = {}
        self.next = (1 << 31) - (1 << 20)

    def __call__(self, key):
        if key is None or self.m.is_idle(key):
            return 0
        if key == UNKNOWN:
            return UNKNOWN_TID
        v = self.map.get(key)
        if v is None:
            pid, tid = key
            v = pid * 100 + tid
            if tid >= 100 or v >= (1 << 31) - (1 << 20):
                v = self.next
                self.next += 1
            self.map[key] = v
        return v


class Writer:
    def __init__(self, model, critical_paths=True, tl=None):
        self.m = model
        self.kev = model.kev
        self.tid = TidMap(model)
        self.critical_paths = critical_paths
        self.tl = tl
        self.trace = pb.Trace()
        self.out = []          # (ts_ns, seq, TracePacket)
        self._seq = 0
        self._uuid = 1000
        self.thread_track = {}
        self.child_tracks = {}
        self.items = defaultdict(list)   # track id -> [t0, t1, name, args, flows, term, instant]
        self.stats = defaultdict(int)

    def ns(self, cyc):
        return self.kev.cycles_to_ns(cyc)

    def uuid(self):
        self._uuid += 1
        return self._uuid

    def comm(self, key):
        if key is None or self.m.is_idle(key):
            return "swapper"
        if key == UNKNOWN:
            return "UNKNOWN"
        t = self.m.threads.get(key)
        if t and t.name:
            return t.name[:15]
        p = self.m.processes.get(key[0])
        return (p.name if p else "pid%d" % key[0])[:15]

    # ---- packets ----------------------------------------------------------
    def _push(self, ts_ns, pkt):
        self._seq += 1
        self.out.append((ts_ns, self._seq, pkt))

    def _te(self, ts_cyc, track, typ, name=None, args=None, flows=(), term=()):
        p = pb.TracePacket()
        p.timestamp = self.ns(ts_cyc)
        p.trusted_packet_sequence_id = SEQ
        e = p.track_event
        e.type = typ
        e.track_uuid = track
        if name is not None:
            e.name = name
        for k, v in (args or {}).items():
            if v is None:
                continue
            d = e.debug_annotations.add()
            d.name = k
            if isinstance(v, bool):
                d.bool_value = v
            elif isinstance(v, int):
                d.int_value = v
            elif isinstance(v, float):
                d.double_value = v
            else:
                d.string_value = str(v)
        if flows:
            e.flow_ids.extend(flows)
        if term:
            e.terminating_flow_ids.extend(term)
        self._push(p.timestamp, p)

    def descriptor(self, desc):
        p = pb.TracePacket()
        p.trusted_packet_sequence_id = SEQ
        p.track_descriptor.CopyFrom(desc)
        self.trace.packet.append(p)

    def track_for(self, key):
        u = self.thread_track.get(key)
        if u is None:
            u = self.thread_track[key] = self.uuid()
            d = pb.TrackDescriptor(uuid=u)
            d.thread.pid = key[0] if key != UNKNOWN else UNKNOWN_TID
            d.thread.tid = self.tid(key)
            d.thread.thread_name = self.comm(key)
            self.descriptor(d)
        return u

    def child_track(self, key, name, counter=False):
        """A named track under a thread's track (key = thread key) or a global one (key None)."""
        u = self.child_tracks.get((key, name))
        if u is None:
            u = self.child_tracks[(key, name)] = self.uuid()
            d = pb.TrackDescriptor(uuid=u, name=name)
            if key is not None:
                d.parent_uuid = self.track_for(key)
            if counter:
                d.counter.SetInParent()
            self.descriptor(d)
        return u

    def add(self, track_id, t0, t1, name, args=None, flows=(), term=()):
        """Queue a slice; track_id is (thread key, child track name or None for the thread track)."""
        if t0 is None or t1 is None:
            return False
        if t1 < t0:
            self.stats["slices_dropped_end_before_begin"] += 1
            return False
        self.items[track_id].append([t0, t1, name, args, tuple(flows), tuple(term), False])
        return True

    def add_instant(self, track_id, ts, name, args=None, flows=(), term=()):
        self.items[track_id].append([ts, ts, name, args, tuple(flows), tuple(term), True])

    def _emit_items(self):
        for (key, name), items in self.items.items():
            lanes = assign_lanes(items)
            by_lane = defaultdict(list)
            for it, ln in zip(items, lanes):
                by_lane[ln].append(it)
            for ln, lst in sorted(by_lane.items()):
                if ln == 0:
                    uuid = self.track_for(key) if name is None else self.child_track(key, name)
                else:
                    base = self.comm(key) if name is None else name
                    uuid = self.child_track(key, "%s (overlap %d)" % (base, ln))
                    self.stats["overlap_lanes"] = max(self.stats["overlap_lanes"], ln)
                self._emit_lane(uuid, lst)

    def _emit_lane(self, uuid, items):
        items.sort(key=lambda x: (x[0], -(x[1] - x[0]), x[6]))
        stack = []
        for t0, t1, name, args, flows, term, instant in items:
            while stack and stack[-1] <= t0:
                self._te(stack.pop(), uuid, pb.TrackEvent.TYPE_SLICE_END)
            if instant:
                self._te(t0, uuid, pb.TrackEvent.TYPE_INSTANT, name, args, flows, term)
                self.stats["instants"] += 1
            else:
                self._te(t0, uuid, pb.TrackEvent.TYPE_SLICE_BEGIN, name, args, flows, term)
                stack.append(t1)
                self.stats["slices"] += 1
        while stack:
            self._te(stack.pop(), uuid, pb.TrackEvent.TYPE_SLICE_END)

    # ---- content ----------------------------------------------------------
    def build(self):
        first = pb.TracePacket()
        first.trusted_packet_sequence_id = SEQ
        first.sequence_flags = pb.TracePacket.SEQ_INCREMENTAL_STATE_CLEARED
        first.trace_config.SetInParent()
        self.trace.packet.append(first)
        self._process_tree()
        self._sched()
        self._states()
        self._ipc()
        if self.critical_paths:
            self._critical_paths()
        self._pulses()
        self._interrupts()
        self._losses()
        self._emit_items()
        self.out.sort(key=lambda x: (x[0], x[1]))
        for _, _, p in self.out:
            self.trace.packet.append(p)
        self.stats["packets"] = len(self.trace.packet)
        return self.trace

    def _process_tree(self):
        m = self.m
        p = pb.TracePacket()
        pt = p.process_tree
        for pid, pr in sorted(m.processes.items()):
            x = pt.processes.add()
            x.pid = pid
            x.ppid = pr.ppid or 0
            x.cmdline.append(pr.path or pr.name)
        for key, t in sorted(m.threads.items()):
            if m.is_idle(key):
                continue
            x = pt.threads.add()
            x.tid = self.tid(key)
            x.tgid = key[0]
            x.name = self.comm(key)
        if any(sw[5] == UNKNOWN for sw in m.switches):
            x = pt.processes.add()
            x.pid = UNKNOWN_TID
            x.cmdline.append("trace-studio: CPU occupant unknown (lost events or migration)")
            x = pt.threads.add()
            x.tid, x.tgid, x.name = UNKNOWN_TID, UNKNOWN_TID, "UNKNOWN"
        self.trace.packet.append(p)
        for pid, pr in sorted(m.processes.items()):   # short process names; ProcessTree keeps the path
            d = pb.TrackDescriptor(uuid=self.uuid())
            d.process.pid = pid
            d.process.process_name = pr.name
            self.descriptor(d)

    def _sched(self):
        """sched_switch and proven sched_waking as ftrace bundles, per CPU, in timestamp order."""
        m = self.m
        per_cpu = defaultdict(list)
        for ts, cpu, prev, prev_code, prev_prio, nxt, next_prio in m.switches:
            fe = pb.FtraceEvent(timestamp=self.ns(ts), pid=self.tid(prev))
            sw = fe.sched_switch
            sw.prev_comm, sw.prev_pid = self.comm(prev), self.tid(prev)
            sw.prev_prio = prev_prio if prev_prio is not None else 0
            sw.prev_state = linux_state(prev_code)
            sw.next_comm, sw.next_pid = self.comm(nxt), self.tid(nxt)
            sw.next_prio = next_prio if next_prio is not None else 0
            per_cpu[cpu].append(fe)
            if nxt == UNKNOWN:
                self.stats["switches_to_unknown"] += 1
        for ts, cpu, waker, woken, prio, _how in m.wakeups:
            fe = pb.FtraceEvent(timestamp=self.ns(ts), pid=self.tid(waker))
            wk = fe.sched_waking
            wk.comm, wk.pid = self.comm(woken), self.tid(woken)
            wk.prio = prio if prio is not None else 0
            wk.success, wk.target_cpu = 1, cpu
            per_cpu[cpu].append(fe)
        for cpu, lst in per_cpu.items():
            lst.sort(key=lambda fe: fe.timestamp)
            for i in range(0, len(lst), 2000):
                pk = pb.TracePacket()
                pk.ftrace_events.cpu = cpu
                pk.ftrace_events.event.extend(lst[i:i + 2000])
                self._push(lst[i].timestamp, pk)
        self.stats["sched_switch"] = len(m.switches)
        self.stats["sched_waking"] = len(m.wakeups)

    def _states(self):
        m = self.m
        for key, t in m.threads.items():
            if m.is_idle(key) or not t.states:
                continue
            name = "%s QNX state" % self.comm(key)
            for t0, t1, code, prio in m.intervals(key):
                self.add((key, name), t0, t1, state_name(code), {"qnx_prio": prio})
                self.stats["state_slices"] += 1
            if len({p for _, p in t.prios}) > 1:
                ct = self.child_track(key, "%s QNX priority" % self.comm(key), counter=True)
                for ts, prio in t.prios:
                    p = pb.TracePacket()
                    p.timestamp = self.ns(ts)
                    p.trusted_packet_sequence_id = SEQ
                    p.track_event.type = pb.TrackEvent.TYPE_COUNTER
                    p.track_event.track_uuid = ct
                    p.track_event.counter_value = prio
                    self._push(p.timestamp, p)

    def _ipc(self):
        m = self.m
        cps = self.kev.cycles_per_sec
        for tx in m.txns:
            sname = m.server_name(tx)
            send_flow, reply_flow = 2 * tx.idx + 1, 2 * tx.idx + 2
            args = {"txn": tx.idx, "rcvid": "0x%08x" % tx.rcvid, "server_pid": tx.server_pid,
                    "server": m.label(tx.server), "client": m.label(tx.client), "message": tx.msg_type,
                    "path": tx.path, "client_blocked": tx.client_block, "parent_txn": tx.parent,
                    "error": tx.error, "outcome": tx.outcome, "trustworthy": tx.trustworthy}
            if tx.coid is not None:
                args["coid"] = "0x%08x" % tx.coid
            for i, w in enumerate(tx.msg):
                args["msg%d" % i] = "0x%08x" % w
            if tx.parent_candidates:
                args["parent_ambiguous"] = ", ".join(map(str, tx.parent_candidates))
            if tx.t_call is not None and tx.t_exit is not None:
                args["latency_us"] = round((tx.t_exit - tx.t_call) * 1e6 / cps, 3)
            if tx.warnings:
                args["warnings"] = "; ".join(tx.warnings)
            have_handle = tx.server is not None and tx.t_rx_exit is not None and tx.t_reply_call is not None
            have_send = tx.t_call is not None and tx.t_exit is not None
            have_reply = tx.t_reply_call is not None and tx.t_reply_exit is not None
            t_back = tx.t_unblock or tx.t_reply_msg
            have_back = have_send and have_reply and t_back is not None and tx.t_call <= t_back <= tx.t_exit
            label = "MsgSend %s-> %s" % (tx.msg_type + " " if tx.msg_type else "", sname)
            self.add((tx.client, None), tx.t_call, tx.t_exit, label, args,
                     flows=(send_flow,) if have_send and have_handle else ())
            if have_handle:
                self.add((tx.server, None), tx.t_rx_exit, tx.t_reply_call,
                         "handle %s%s" % (tx.msg_type + " " if tx.msg_type else "", m.label(tx.client)),
                         {"txn": tx.idx, "client": m.label(tx.client), "rcvid": "0x%08x" % tx.rcvid},
                         term=(send_flow,) if have_send else ())
            if have_reply:
                self.add((tx.replier or tx.server, None), tx.t_reply_call, tx.t_reply_exit,
                         "MsgError" if tx.error is not None else "MsgReply", {"txn": tx.idx},
                         flows=(reply_flow,) if have_back else ())
            if have_back:
                self.add((tx.client, None), t_back, tx.t_exit, "reply received", {"txn": tx.idx}, term=(reply_flow,))
            self.stats["ipc_txns"] += 1

    def _critical_paths(self):
        from .analyses import Timeline, us
        from .critical import critical_path
        m = self.m
        tl = self.tl or Timeline(m)

        def put(key, track, segs, depth):
            for sg in segs:
                args = {"kind": sg.kind, "txn": sg.txn, "thread": m.label(sg.thread) if sg.thread else None,
                        "qnx_prio": sg.prio, "us": round(us(m, sg.t1 - sg.t0), 3)}
                self.add((key, track), sg.t0, sg.t1, sg.label, args)
                put(key, track, sg.children, depth + 1)

        for tx in m.txns:
            if not tx.trustworthy or tx.parent is not None or m.is_idle(tx.client):
                continue
            segs = critical_path(m, tx, tl)
            track = "%s critical path" % self.comm(tx.client)
            self.add((tx.client, track), tx.t_call, tx.t_exit, "txn %d %s-> %s: %.1f us" % (
                tx.idx, tx.msg_type + " " if tx.msg_type else "", m.server_name(tx), us(m, tx.t_exit - tx.t_call)),
                {"txn": tx.idx, "kind": "request"})
            put(tx.client, track, segs, 1)
            self.stats["critical_paths"] += 1

    def _pulses(self):
        m = self.m
        fid = 1 << 40
        for pl in m.pulses:
            fid += 1
            linked = pl.match == "unique" and pl.t_send is not None and pl.receiver is not None
            args = {"scoid": "0x%08x" % pl.scoid, "server_pid": pl.server_pid, "kind": pl.kind, "code": pl.code,
                    "value": pl.value, "match": pl.match, "confidence": pl.confidence,
                    "sender": m.label(pl.sender) if pl.sender else "kernel or unknown"}
            if pl.match == "ambiguous":
                args["candidates"] = ", ".join(map(str, pl.candidates))
            if pl.context is not None:
                args["cpu_thread_at_send"] = m.label(pl.context)
            if pl.t_send is not None:
                track = (pl.sender, None) if pl.sender else (None, "CPU %s kernel pulses" % pl.cpu)
                self.add_instant(track, pl.t_send, "send %s" % pl.kind, dict(args, pulse=pl.idx),
                                 flows=(fid,) if linked else ())
            if pl.t_recv is not None and pl.receiver is not None:
                self.add_instant((pl.receiver, None), pl.t_recv, "receive %s" % pl.kind, dict(args, pulse=pl.idx),
                                 term=(fid,) if linked else ())
            self.stats["pulse_arrows"] += linked
        self.stats["pulses"] = len(m.pulses)

    def _interrupts(self):
        for ts, cpu, intr in self.m.interrupts:
            self.add_instant((None, "CPU %d interrupts" % cpu), ts, "INT 0x%08x" % intr, {"intr_id": intr})
        self.stats["interrupts"] = len(self.m.interrupts)

    def _losses(self):
        for g in self.m.gaps:
            t1 = g.t1 if g.t1 is not None else self.m.end_ts
            self.add((None, "lost events"), g.t0, max(t1, g.t0), "CPU %d: %s%s" % (
                g.cpu, g.kind, " (%d)" % g.count if g.count is not None else ""), {"detail": g.detail, "cpu": g.cpu})


def write(model, path, critical_paths=True, tl=None):
    w = Writer(model, critical_paths, tl)
    tr = w.build()
    with open(path, "wb") as f:
        f.write(tr.SerializeToString())
    return dict(w.stats)
