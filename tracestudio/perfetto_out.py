# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Write a Perfetto protobuf trace (TracePacket stream) from a Model.

Mapping:
  * context switches -> ftrace sched_switch per CPU, wakeups -> sched_waking
    (the waker is the thread running on the CPU that emitted THREADY), so
    Perfetto's CPU tracks, sched and thread_state tables work natively.
    procnto's idle threads become pid 0 (swapper), like Linux idle.
  * processes and threads -> ProcessTree. Perfetto tids are global, QNX tids
    are per process, so thread (pid, tid) becomes pid*100+tid when that fits
    in 31 bits (readable: 109365703 = pid 1093657 tid 3), else a counter.
  * exact QNX thread states (SEND, RECEIVE, REPLY, MUTEX, CONDVAR, ...) ->
    slices on a "<thread> state" child track of each thread.
  * message passing -> slices on the thread tracks: client "MsgSend",
    server "MsgReceive" (blocked waiting), "handle", "MsgReply", and a client
    "reply received" slice; flow arrows send->handle and reply->client.
  * pulses -> instants with a flow from sender to receiver when matched.
  * interrupts -> instants on per-CPU "interrupts" tracks.
  * priorities -> a "priority" counter track per thread whose priority changes.
"""
from __future__ import annotations

from .events import state_name

try:
    from perfetto.protos.perfetto.trace import perfetto_trace_pb2 as pb
except ImportError as e:  # pragma: no cover
    raise SystemExit("trace-studio needs the 'perfetto' Python package (pip install perfetto protobuf): %s" % e)

SEQ = 7351
_BLOCKED = 1      # Linux TASK_INTERRUPTIBLE ("S")
_RUNNABLE = 0     # Linux TASK_RUNNING ("R")
_STOPPED = 4
_DEAD = 16


def linux_state(code):
    if code is None:
        return _RUNNABLE
    n = state_name(code)
    if n in ("RUNNING", "READY"):
        return _RUNNABLE
    if n == "STOPPED":
        return _STOPPED
    if n in ("DEAD", "DESTROY"):
        return _DEAD
    return _BLOCKED


class TidMap:
    def __init__(self, model):
        self.m = model
        self.map = {}
        self.next = (1 << 31) - (1 << 20)

    def __call__(self, key):
        if key is None or self.m.is_idle(key):
            return 0
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
    def __init__(self, model):
        self.m = model
        self.kev = model.kev
        self.tid = TidMap(model)
        self.trace = pb.Trace()
        self.events = []      # (ts_ns, order, seq, TracePacket)
        self._seq = 0
        self._uuid = 1000
        self.thread_track = {}
        self.state_track = {}
        self.prio_track = {}
        self.stats = {}

    def ns(self, cyc):
        return self.kev.cycles_to_ns(cyc)

    def uuid(self):
        self._uuid += 1
        return self._uuid

    def comm(self, key):
        if key is None or self.m.is_idle(key):
            return "swapper"
        t = self.m.threads.get(key)
        if t and t.name:
            return t.name[:15]
        p = self.m.processes.get(key[0])
        return (p.name if p else "pid%d" % key[0])[:15]

    # ---- packets ----------------------------------------------------------
    def _add(self, ts_cyc, order, pkt):
        self._seq += 1
        self.events.append((self.ns(ts_cyc), order, self._seq, pkt))

    def _te(self, ts_cyc, track, typ, name=None, args=None, flows=(), term=(), order=1):
        p = pb.TracePacket()
        p.timestamp = self.ns(ts_cyc)
        p.trusted_packet_sequence_id = SEQ
        e = p.track_event
        e.type = typ
        e.track_uuid = track
        if name is not None:
            e.name = name
        for k, v in (args or {}).items():
            d = e.debug_annotations.add()
            d.name = k
            if isinstance(v, bool):
                d.bool_value = v
            elif isinstance(v, int):
                d.int_value = v
            else:
                d.string_value = str(v)
        if flows:
            e.flow_ids.extend(flows)
        if term:
            e.terminating_flow_ids.extend(term)
        self._add(ts_cyc, order, p)

    def slice(self, track, t0, t1, name, args=None, flows=(), term=(), end_flows=()):
        if t0 is None or t1 is None or t1 < t0:
            return False
        self._te(t0, track, pb.TrackEvent.TYPE_SLICE_BEGIN, name, args, flows, term, order=1)
        self._te(t1, track, pb.TrackEvent.TYPE_SLICE_END, flows=end_flows, order=0)
        return True

    def instant(self, track, ts, name, args=None, flows=(), term=()):
        self._te(ts, track, pb.TrackEvent.TYPE_INSTANT, name, args, flows, term, order=2)

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
            d.thread.pid = key[0]
            d.thread.tid = self.tid(key)
            d.thread.thread_name = self.comm(key)
            self.descriptor(d)
        return u

    def child_track(self, cache, key, name, counter=False):
        u = cache.get(key)
        if u is None:
            u = cache[key] = self.uuid()
            d = pb.TrackDescriptor(uuid=u, name=name, parent_uuid=self.track_for(key))
            if counter:
                d.counter.SetInParent()
            self.descriptor(d)
        return u

    # ---- content ----------------------------------------------------------
    def build(self):
        m = self.m
        first = pb.TracePacket()
        first.trusted_packet_sequence_id = SEQ
        first.sequence_flags = pb.TracePacket.SEQ_INCREMENTAL_STATE_CLEARED
        first.trace_config.SetInParent()
        self.trace.packet.append(first)
        self._process_tree()
        evs = m.kev.events()
        end_ts = evs[-1].ts if evs else 0
        self._sched()
        self._states(end_ts)
        self._ipc()
        self._pulses()
        self._interrupts()
        self.events.sort(key=lambda x: (x[0], x[1], x[2]))
        for _, _, _, p in self.events:
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
        self.trace.packet.append(p)
        for pid, pr in sorted(m.processes.items()):   # short process names; ProcessTree keeps the path
            d = pb.TrackDescriptor(uuid=self.uuid())
            d.process.pid = pid
            d.process.process_name = pr.name
            self.descriptor(d)

    def _sched(self):
        """sched_switch and sched_waking as ftrace bundles, per CPU, in timestamp order."""
        m = self.m
        per_cpu = {}
        for ts, cpu, prev, prev_code, prev_prio, nxt, next_prio in m.switches:
            fe = pb.FtraceEvent(timestamp=self.ns(ts), pid=self.tid(prev))
            sw = fe.sched_switch
            sw.prev_comm, sw.prev_pid = self.comm(prev), self.tid(prev)
            sw.prev_prio = prev_prio if prev_prio is not None else 0
            sw.prev_state = linux_state(prev_code)
            sw.next_comm, sw.next_pid = self.comm(nxt), self.tid(nxt)
            sw.next_prio = next_prio if next_prio is not None else 0
            per_cpu.setdefault(cpu, []).append(fe)
        for ts, cpu, waker, woken, prio in m.wakeups:
            fe = pb.FtraceEvent(timestamp=self.ns(ts), pid=self.tid(waker))
            wk = fe.sched_waking
            wk.comm, wk.pid = self.comm(woken), self.tid(woken)
            wk.prio = prio if prio is not None else 0
            wk.success, wk.target_cpu = 1, cpu
            per_cpu.setdefault(cpu, []).append(fe)
        for cpu, lst in per_cpu.items():
            lst.sort(key=lambda fe: fe.timestamp)
            for i in range(0, len(lst), 2000):
                pk = pb.TracePacket()
                pk.ftrace_events.cpu = cpu
                pk.ftrace_events.event.extend(lst[i:i + 2000])
                self._seq += 1
                self.events.append((lst[i].timestamp, 3, self._seq, pk))
        self.stats["sched_switch"] = len(m.switches)
        self.stats["sched_waking"] = len(m.wakeups)

    def _states(self, end_ts):
        m = self.m
        n = 0
        for key, t in m.threads.items():
            if m.is_idle(key) or not t.states:
                continue
            tr = self.child_track(self.state_track, key, "%s state" % self.comm(key))
            for t0, t1, code, prio in m.intervals(key, end_ts):
                args = {"prio": prio} if prio is not None else None
                self.slice(tr, t0, t1, state_name(code), args)
                n += 1
            if len({p for _, p in t.prios}) > 1:
                ct = self.child_track(self.prio_track, key, "%s priority" % self.comm(key), counter=True)
                for ts, prio in t.prios:
                    p = pb.TracePacket()
                    p.timestamp = self.ns(ts)
                    p.trusted_packet_sequence_id = SEQ
                    p.track_event.type = pb.TrackEvent.TYPE_COUNTER
                    p.track_event.track_uuid = ct
                    p.track_event.counter_value = prio
                    self._add(ts, 2, p)
        self.stats["state_slices"] = n

    def _ipc(self):
        m = self.m
        n = 0
        for tx in m.txns:
            sname = m.processes[tx.server_pid].name if tx.server_pid in m.processes else "pid%d" % tx.server_pid
            send_flow, reply_flow = 2 * tx.idx + 1, 2 * tx.idx + 2
            args = {"txn": tx.idx, "rcvid": "0x%08x" % tx.rcvid, "server_pid": tx.server_pid,
                    "server": m.label(tx.server), "client": m.label(tx.client)}
            if tx.coid is not None:
                args["coid"] = "0x%08x" % tx.coid
            for i, w in enumerate(tx.msg):
                args["msg%d" % i] = "0x%08x" % w
            if tx.client_block:
                args["client_blocked"] = tx.client_block
            if tx.parent is not None:
                args["parent_txn"] = tx.parent
            if tx.error is not None:
                args["error"] = tx.error
            if tx.t_call is not None and tx.t_exit is not None:
                args["latency_us"] = (tx.t_exit - tx.t_call) * 1_000_000 // self.kev.cycles_per_sec
            if tx.warnings:
                args["warnings"] = "; ".join(tx.warnings)
            ct = self.track_for(tx.client)
            have_send = self.slice(ct, tx.t_call, tx.t_exit, "MsgSend -> %s" % sname, args, flows=(send_flow,))
            if tx.server is not None:
                st = self.track_for(tx.server)
                h_args = {"txn": tx.idx, "client": m.label(tx.client), "rcvid": "0x%08x" % tx.rcvid}
                self.slice(st, tx.t_rx_exit, tx.t_reply_call, "handle %s" % m.label(tx.client), h_args,
                           term=(send_flow,) if have_send else ())
                rt = self.track_for(tx.replier or tx.server)
                self.slice(rt, tx.t_reply_call, tx.t_reply_exit, "MsgError" if tx.error is not None else "MsgReply",
                           {"txn": tx.idx}, flows=(reply_flow,))
            if have_send:
                t_back = tx.t_unblock or tx.t_reply_msg
                if t_back is not None and tx.t_reply_exit is not None:
                    self.slice(ct, t_back, tx.t_exit, "reply received", {"txn": tx.idx}, term=(reply_flow,))
            n += 1
        self.stats["ipc_txns"] = n

    def _pulses(self):
        m = self.m
        fid = 1 << 40
        for pl in m.pulses:
            fid += 1
            linked = pl.t_send is not None and pl.t_recv is not None and pl.receiver is not None
            args = {"scoid": "0x%08x" % pl.scoid, "server_pid": pl.server_pid, "kind": pl.kind}
            if pl.code is not None:
                args["code"] = pl.code
            if pl.value is not None:
                args["value"] = pl.value
            if pl.t_send is not None and pl.sender is not None:
                self.instant(self.track_for(pl.sender), pl.t_send, "send %s" % pl.kind, args,
                             flows=(fid,) if linked else ())
            if pl.t_recv is not None and pl.receiver is not None:
                self.instant(self.track_for(pl.receiver), pl.t_recv, "receive %s" % pl.kind, args,
                             term=(fid,) if linked else ())
        self.stats["pulses"] = len(m.pulses)

    def _interrupts(self):
        tracks = {}
        for ts, cpu, intr in self.m.interrupts:
            u = tracks.get(cpu)
            if u is None:
                u = tracks[cpu] = self.uuid()
                self.descriptor(pb.TrackDescriptor(uuid=u, name="CPU %d interrupts" % cpu))
            self.instant(u, ts, "INT 0x%08x" % intr, {"intr_id": intr})
        self.stats["interrupts"] = len(self.m.interrupts)


def write(model, path):
    w = Writer(model)
    tr = w.build()
    with open(path, "wb") as f:
        f.write(tr.SerializeToString())
    return w.stats
