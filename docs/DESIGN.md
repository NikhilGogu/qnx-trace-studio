# trace-studio design

trace-studio converts QNX kernel event traces (`.kev` files written by `tracelogger`) into Perfetto traces offline, and analyses QNX message passing in them. The QNX Toolkit for VS Code exports traces only as CSV. QNX's Perfetto fork (`qnx-ports/perfetto`) captures live through `traced_qnx_probes` but cannot import a `.kev`. trace-studio fills that gap: take any `.kev` captured on any QNX 8.0 system, convert it on the host, and open it in ui.perfetto.dev or query it with Perfetto SQL. On top of the conversion it reconstructs every MsgSend/MsgReceive/MsgReply exchange and reports blocking chains and suspected priority inversions, which generic viewers do not do.

Status on 3 Nov 2026 (stage 1): parser, model, Perfetto writer, validation, two analyses and a CLI all work on traces captured from QNX 8.0 x86_64 under QEMU. Everything below marked "measured" comes from commands run on that setup, with the inputs in `tests/data/run1`.

## 1. Architecture

```
 target (QNX 8.0)                              host (Linux, Python 3.10, no QNX libraries needed)
 ----------------                              ---------------------------------------------------
 workload/ts_*  --msgs-->  procnto (instr)     tracestudio/kev.py      header, syspage, 16-byte slots,
                              |                                        combine reassembly, 64-bit time,
 tracelogger -w -k 32 ...  <--+ kernel buffers                         buffer-loss accounting
      |                                        tracestudio/events.py   event names, argument layouts
      v                                                                (fast and wide), qnx_tables.py
  trace.kev  ------------ scp ------------->   tracestudio/model.py    threads, states, CPU switches,
  workload CSV logs (ground truth) -------->                           wakeups, IPC transactions, pulses
                                               tracestudio/perfetto_out.py   TracePacket protobuf
                                               tracestudio/analyses.py       latency, chains, inversions
                                               tracestudio/validate.py       checks against workload logs
                                               tracestudio/cli.py            info|convert|validate|analyze
```

The reader is self-contained Python. `libtraceparser` exists in the SDP only for QNX targets (`$QNX_TARGET/{x86_64,aarch64le}/usr/lib/libtraceparser.so`), so a host tool cannot link it; the host `traceprinter` exists but prints text that is slow and lossy to re-parse. Route (b) from the plan, an own binary parser, is therefore the main path, and `traceprinter` is used only as the reference oracle in tests (section 6). Only the Perfetto writer needs a third-party package (`perfetto` 0.59.1 for its generated protos, `protobuf` 7.36.2).

## 2. The .kev format as observed (TRACE_VER 2.01, QNX 8.0)

- Text header: `TRACE_HEADER_BEGIN::` then `TRACE_<KEY>::<value>` pairs without separators (DATE, VER_MAJOR, VER_MINOR, LITTLE_ENDIAN, ENCODING, BOOT_DATE, CYCLES_PER_SEC, CPU_NUM, SYSNAME, NODENAME, SYS_RELEASE, SYS_VERSION, MACHINE, SYSPAGE_LEN, FILE_NAME, TRACELOGGER_ARGS, plus `-A` attributes), then `TRACE_HEADER_END::`.
- The system page follows immediately, `TRACE_SYSPAGE_LEN` bytes (2384 here; its first two 16-bit fields are 0x00f0 and 0x0950 = 2384, the syspage size and total size).
- Then 16-byte `traceevent_t` slots to end of file (probe trace: 84287 bytes = 2879 header+syspage + 5088 x 16).
- Header word, from `<sys/trace.h>` and confirmed against traceprinter: bits 31-30 slot structure (simple, combine begin, continuation, end), bits 29-24 CPU, bit 16 `_NTO_TRACE_PROCESS_INJECTED` (182 slots in the probe: state emitted at trace start), bits 14-10 internal class, bits 9-0 event code.
- Kernel-call class codes: bits 0-6 call number (`__KER_*`), bit 7 exit, bit 9 64-bit argument variant. Example measured counts in the probe: MsgSendv entry 0x20b x37, exits 0x8b x13 + 0x28b x24 (32- and 64-bit status), matching traceprinter's 37 `KER_CALL MSG_SENDV/11` and 37 `KER_EXIT`.
- Process/thread class: codes below 63 are thread states (`STATE_*` from `<sys/states.h>`), 64/128/192/256/320 are PROCCREATE, PROCDESTROY, PROCCREATE_NAME, PROCDESTROY_NAME and PROCTHREAD_NAME.
- Slots arrive in kernel-buffer order: one buffer (1011 slots, `(16 KB - 208-byte header) / 16`) holds one CPU's events, and buffers from different CPUs follow each other as they fill. Each buffer starts with CONTROL/TIME (MSB of the clock) and CONTROL/BUFFER (per-CPU sequence number, event count). traceprinter merges CPUs by time; so does trace-studio.
- Payload: `data[0]` is the timestamp LSB in every slot; `data[1]`, `data[2]` of each slot of an event concatenate into its argument words. All slots of a combine event share CPU, class, code and timestamp, and other events can interlace between them.

Reader rules (`kev.py`): reassemble combine events keyed by (CPU, class, code, timestamp LSB); a second begin with the same key closes the first as `partial`; continuation slots without a begin are counted as orphans. Extend timestamps per CPU with the MSB from the last TIME event on that CPU, and when the result is more than 2^31 cycles from that CPU's latest timestamp pick MSB-1 or MSB+1 instead (interlaced slots around a rollover). Detect lost buffers from gaps in each CPU's BUFFER sequence and sum CONTROL/DROPPED counts. Sort by (timestamp, file position).

## 3. Mapping .kev events to Perfetto

| QNX event | Perfetto output | Notes |
|---|---|---|
| THRUNNING on CPU n | ftrace `sched_switch` on CPU n (prev = previous thread on n, `prev_state` from its QNX state) | native CPU tracks, `sched`, `thread_state` tables |
| READY after a blocked state | ftrace `sched_waking`, `pid` = thread running on the emitting CPU | gives Perfetto's "woken by" (`waker_utid`) |
| thread migrates before its old CPU switches | synthetic switch to idle on the old CPU | keeps per-CPU switch chains consistent |
| lost buffer on CPU n | synthetic switch to idle; open IPC marked "spans lost buffer" | no running thread assumed across a gap |
| procnto idle threads (pid 1, priority 0) | pid 0 (swapper) | Linux idle convention |
| PROCCREATE_NAME, PROCTHREAD_NAME | ProcessTree (pid, ppid, path; tid, tgid, name) plus process track descriptors | |
| every thread state (RUNNING, READY, SEND, RECEIVE, REPLY, MUTEX, CONDVAR, SEM, NANOSLEEP, INTR, MUON_MUTEX, ...) | slices on a `<thread> state` child track, `prio` argument | exact QNX states, which Linux states cannot express |
| priority field of wide THREAD events | `<thread> priority` counter track when it changes | shows inheritance boosts |
| MsgSendv entry to exit (client) | slice `MsgSend -> <server process>` with coid, rcvid, msg0-2, client_blocked, latency_us, parent_txn | flow out to the server's handle slice |
| MsgReceivev exit to MsgReplyv entry (server) | slice `handle <client>` | flow in from the client |
| MsgReplyv / MsgError (server) | slice `MsgReply` / `MsgError` | flow out to the client |
| REPLY_MESSAGE or THREADY to MsgSendv exit (client) | slice `reply received` nested in MsgSend | flow in from MsgReply |
| SND_PULSE*, REC_PULSE | instants `send pulse...`/`receive pulse...` linked by a flow when matched | code and value from MsgSendPulse entry (wide mode) |
| INT_DELIVER | instant `INT 0x...` on a `CPU n interrupts` track | QNX 8.0 delivers interrupts to IST threads; traceprinter shows no INT_ENTR/INT_EXIT line in any of the four captures |
| ChannelCreate entry/exit | channel table with flags | `_NTO_CHF_FIXED_PRIORITY` detected (section 5) |

Thread ids: Perfetto tids are global and QNX tids are per process, so (pid, tid) becomes `pid*100 + tid` when that fits below 2^31 - 2^20 (readable: 109365702 is pid 1093657 tid 2) and a counter otherwise. Timestamps: ns = cycles x 10^9 / TRACE_CYCLES_PER_SEC, from the trace start's absolute ClockCycles(), so they line up with ClockCycles() values logged by applications.

Measured TraceProcessor table population for `ipc_wide`: sched 8086, thread_state 16971 (4742 with a known waker), slice 24556, flow 3032, counter 23, thread 162, process 39, and no rows in `stats` with severity error or data_loss (the test asserts this for two traces). An earlier version produced `mismatched_sched_switch_tids` (3 and 4); the synthetic switch-out on migration fixed it.

## 4. Message-passing reconstruction

The actor of a kernel call or COMM event is the thread most recently switched in (non-injected THRUNNING) on that CPU. Observed sequence for one exchange: client `KER_CALL MSG_SENDV(coid, msg[0..2])`, `COMM SND_MESSAGE(rcvid, server pid)`, client THREPLY (server was waiting) or THSEND (server busy), server THRUNNING, `COMM REC_MESSAGE(rcvid, pid)`, `KER_EXIT MSG_RECEIVEV(rcvid, ..., info pid, tid, chid, scoid, coid, msglen, priority)`, `KER_CALL MSG_REPLYV(rcvid, status)`, `COMM REPLY_MESSAGE(client tid, pid)`, client THREADY, `KER_EXIT MSG_REPLYV`, client `KER_EXIT MSG_SENDV(status)`.

A transaction is opened by SND_MESSAGE and keyed by (server pid, rcvid). The rcvid names one pending message, so the key is unique while the client is blocked and is reused afterwards (measured: up to 186 transactions on one key in `ipc_wide`). REC_MESSAGE names the receiving thread, which resolves concurrent receivers on one channel. MsgReceivev exit adds `_msg_info` in wide mode and is cross-checked (1176 agree, 1 disagrees in `ipc_wide`; see limits). A request sent by a thread that is handling another request becomes its child, which gives the cross-server critical path. Payload words are recorded but never used for linking.

Measured example (`ipc_wide`, request 4007 from client-3, forwarded): client MsgSend at 0 us, front-rx1 receives at 1.4 us, front sends to back at 99.0 us, back-rx receives at 101.6 us, back replies at 300.3 us, front's send returns at 336.0 us, front replies at 433.2 us, client returns at 467.5 us. The chain printer reports `client-3 (prio 10) REPLY-blocked on ts_front/front-rx1 for 467.5 us`, nested `front-rx1 SEND-blocked on ts_back/back-rx for 237.0 us`.

## 5. Analyses

Implemented now (`tracestudio analyze`):
- IPC latency per (client thread, server process): count, min, median, max.
- Blocking chains for the longest top-level requests, recursing into nested requests, with the server's time per state and priority.
- Suspected priority inversion, labelled "suspected" and emitted only with evidence: client at priority Pc waits on a server thread that is READY at Ps < Pc while threads with Ps < Pr < Pc run; the report lists those threads, their priorities, CPUs and run time, and which request the server was busy with. Ordinary blocking is never flagged.
- Deadline check: `--deadline THREAD=US` per server.
- Channels created with `_NTO_CHF_FIXED_PRIORITY` during the trace.

Measured on the priority scenario (section 7):

| | hi requests | missed 5 ms | hi max latency | suspected inversions | server priority while handling hi (Q3) |
|---|---|---|---|---|---|
| default channel (inheritance) | 20 | 0 | 2740.4 us | 0 | 30 |
| `_NTO_CHF_FIXED_PRIORITY` | 20 | 20 | 11429.5 us | 20 | 10 |

Run 2 reproduced it: inheritance 0 of 20 missed (max 2863.3 us, 0 inversions), fixed priority 20 of 20 missed (max 11035.3 us, 20 inversions, fixed-priority channel `2019351/1` detected).

In the fixed case each report reads: `ts_pload/hi` (prio 30) SEND-blocked, `ts_psrv/psrv-rx` READY at priority 10 for a median 8177.6 us, preempted by `ts_pload/hog` (prio 20) on CPU 0 for a median 8155.5 us, server busy with `ts_pload/lo`'s request; channel `1421335/1` has flags 0x1. With inheritance the same SQL shows the server at priority 10 for lo and 30 once hi has sent, matching the System Architecture guide ("the change in the server's priority happens when T1 sends the message").

The same chain printer, unprompted, found a real system chain in `prio_inherit`: `tracelogger` REPLY-blocked 30041.4 us on `devb-eide/fsys_resmgr`, which spent 29917.6 us of it in MUTEX.

Planned:
- Critical path across servers as a Perfetto track (per top-level request, the segment on each thread: running, preempted, blocked on whom), exported as slices plus SQL views.
- Resource-manager decoding: name `_IO_*` message types from msg[0] (for example msg[0] `0x00020100` is `_IO_CONNECT` (0x100) with subtype 2 in the upper half), map coids to path names from PATHMGR events and ConnectAttach.
- Mutex and condvar chains: SyncMutexLock wide arguments carry the owner, so "thread A blocked in MUTEX on a mutex owned by B, which is REPLY-blocked on C" can be built the same way as IPC chains.
- Interrupt-to-IST latency: INT_DELIVER to the THRUNNING of the IST that THINTR was waiting.
- Per-process CPU use, APS partition budgets (SYSTEM APS_* events), page waits.
- A `diff` report for before/after traces (the demo's fix) and an HTML summary.

## 6. Validation (measured)

1. Event by event against the host `traceprinter` (`tools/compare_traceprinter.py`), five traces (`probe`, `ipc_wide`, `ipc_fast`, `prio_inherit`, `prio_fixed`): 139,421 events, identical (64-bit timestamp, CPU, class, event) for all of them, per-CPU order differences 0, 375,640 argument values compared over 47 event types, 0 mismatches. Labels are normalised (kernel calls by number, SYSTEM by class) because names differ (`SYNC_CONDVAR_SIG` vs `SYNC_CONDVAR_SIGNAL`, `PATHMGR_OPEN` vs `PATHMGR`); signed and unsigned prints of the same 32- or 64-bit value count as equal. The comparison found one real bug (process event 64 decoded as a virtual-thread state), since fixed. A second capture of the same four scenarios (run 2, same target, not committed) gave the same result: 135,749 events identical, 366,243 argument values, 0 mismatches, with 750 per-CPU timestamp regressions (interlaced combine events) in its fast-mode trace handled by the merge.
2. Against workload ground truth (`tracestudio validate`): every logged request must contain exactly one decoded transaction from the same client thread, naming the same server pid and thread, the same rcvid, with MsgReceivev exit before the server's post-receive log and MsgReplyv entry after its pre-reply log; in wide mode msg[1] must equal the request id; forwarded requests must be children of the client request.

| trace | requests | fully matched | parent links | payload id | missing | wrong |
|---|---|---|---|---|---|---|
| ipc_wide | 200 | 200 | 40/40 | 200/200 | 0 | 0 |
| ipc_fast | 200 | 200 | 40/40 | n/a | 0 | 0 |
| prio_inherit | 40 | 40 | n/a | 40/40 | 0 | 0 |
| prio_fixed | 40 | 40 | n/a | 40/40 | 0 | 0 |

Match rate 480/480 (100%); run 2 also 480/480, with rcvid keys reused up to 173 times (ipc) and 120 times (prio). Client pulses: 4 logged, 4 matched with their value in wide mode, 0 with value in fast mode (fast MsgSendPulse entry has no value). Back-end timer pulses: 413 logged, 413 received in the trace. Log-minus-trace latency (time the client spends outside the kernel call): median 1.85 us (ipc_wide), 2.36 us (ipc_fast), 15.9-16.1 us in the pinned single-CPU priority runs. For request 30005 in `prio_fixed` the split is 15.37 us from the client's ClockCycles() to the MsgSendv entry event and 0.24 us from the exit event to the second ClockCycles(); the disassembly (`ntox86_64-objdump -d ts_pload`) shows only register moves and the `MsgSend@plt` call between rdtsc and the call, so the time is spent in libc or kernel entry before the event is written. Cause still open (candidates: lazy FPU/XSAVE handling under nested virtualisation, kernel-entry locking).

Ambiguities found and how they are handled:
- Concurrent receivers: ts_front has two receive threads on one channel; server thread matched 200/200 through REC_MESSAGE's actor.
- Reused ids: 34 (server pid, rcvid) keys reused in `ipc_wide`, up to 186 times each; no mislink.
- Unpaired events: in `ipc_wide`, 6 REPLY_MESSAGE and 6 MsgSendv exits found no open transaction and 11 pulses were received without a traced send (sends that began before the trace, or exchanges closed by an rcvid reuse; not yet attributed one by one). They are counted, never linked.
- Exchanges that never complete: 6 system transactions get no reply, 5 from unnamed processes (tid 1) sending msg[0] 0x40010 to procnto and 1 from ts_front/front-rx1 sending type 0x116 to procnto as ts_front exits. They carry the warnings "still open at end of trace" and "rcvid reused while still open". For the ts_front one, `_msg_info` on the receiving side names tid 1 while the traced sender was tid 2; flagged, not hidden.
- Missing events (fault injection, `tools/drop_buffer.py`): removing 4 of the 14 buffers inside the workload window (4042 slots) gives 129/200 matched, 55 with no candidate, 16 incomplete, and 0 wrong links; 30 open transactions are marked "spans lost buffer" and the loss is reported per CPU (`{'0': [42, 45], '1': [41, 44]}`). Removing buffer 40 or 41 alone leaves 200/200; removing buffers 30, 45, 60 and 75 gives 199/200 with one missing and none wrong.
3. Perfetto import: TraceProcessor 0.59.1 loads every converted trace; tests assert table population, no error/data-loss stats, and exactly 202 `MsgSend -> ts_*` slices in `ipc_wide` (160 requests + 40 forwards + 2 QUIT) and 41 in `prio_fixed`.
4. Regression suite: `tests/test_tracestudio.py` (pytest or plain Python) runs all of the above on the committed gzip fixtures; traceprinter and perfetto parts skip when absent. Measured run on the build box: all 5 tests pass.

Test strategy going forward: unit tests on synthetic slot streams (interlaced combine events, rollover with interlacing, orphan and partial slots, unknown classes), golden comparison against traceprinter on every new capture, the ground-truth workload for every linking change, fault injection (dropped buffers, truncated file, injected-state-only start), Perfetto import checks, aarch64le traces from a QEMU aarch64 target, and a performance budget (ipc_wide, 106,224 slots: parse 0.23 s, full conversion 1.65 s and 177 MB peak RSS on the build box).

## 7. Capture (exact commands used)

Target: QNX 8.0.0 x86_64 under QEMU/KVM built with `mkqnximage --type=qemu --arch=x86_64`, 2 CPUs, `TRACE_CYCLES_PER_SEC 2699969068`. Workload built with `qcc -Vgcc_ntox86_64 -O2 -g -Wall -Wextra -std=gnu11` (`make -C workload`, no warnings) and run by `workload/run_scenarios.sh`:

```
tracelogger -w -k 32 -b 128 -s 4 -A SCENARIO=ipc_wide     -f /tmp/ts/out/ipc_wide.kev
tracelogger    -k 32 -b 128 -s 4 -A SCENARIO=ipc_fast     -f /tmp/ts/out/ipc_fast.kev
tracelogger -w -k 32 -b 128 -s 4 -A SCENARIO=prio_inherit -f /tmp/ts/out/prio_inherit.kev
tracelogger -w -k 32 -b 128 -s 4 -A SCENARIO=prio_fixed   -f /tmp/ts/out/prio_fixed.kev
```

Rationale: non-daemon mode enables all classes, which the converter needs (THREAD for states and priorities, PROCESS for names, COMM and KERCALL for IPC, CONTROL for time and buffer accounting, INT, SYSTEM). `-w` adds priorities, `_msg_info` and 12 bytes of each message; fast mode keeps the IPC links (still 200/200) but loses those. `-k 32` (32 kernel buffers of 16 KB per CPU) and `-b 128` gave no lost buffers and no dropped events in any capture; `-s 4` is time-based capture; `-A` labels the scenario in the header. Sizes: ipc_wide 1,702,541 bytes (106,224 slots, 44,807 events) vs ipc_fast 890,890 bytes (55,496 slots, 46,035 events) for the same workload.

Scenarios: `ipc_*` runs ts_back (ChannelCreate, 5 ms timer pulse), ts_front (name_attach, two receive threads, forwards every fourth request to ts_back) and ts_clients (4 threads x 40 requests, MsgSendPulse every tenth request of client 0). `prio_*` runs ts_psrv (one thread, FIFO 10, CPU 0, `-f` for `_NTO_CHF_FIXED_PRIORITY`) and ts_pload (lo FIFO 10 sends 4 ms of work at t+0, hog FIFO 20 burns 10 ms at t+2 ms, hi FIFO 30 sends 0.5 ms of work at t+4 ms, period 25 ms, 20 periods, all on CPU 0). Work is a calibrated loop, so preemption delays it instead of shortening it.

## 8. CLI

Current:
```
python3 -m tracestudio info     TRACE.kev                 # header, counts, losses, decoder counters (JSON)
python3 -m tracestudio convert  TRACE.kev [-o OUT]        # Perfetto protobuf, warnings for lost data / fast mode
python3 -m tracestudio validate TRACE.kev --logs DIR      # workload ground-truth check (JSON)
python3 -m tracestudio analyze  TRACE.kev [--deadline THREAD=US] [--chains N]
python3 tools/compare_traceprinter.py TRACE.kev TRACE.txt # oracle check
python3 tools/tp_queries.py OUT.perfetto-trace            # SQL: tables, latency, chains, server priority
```
Planned: `tracestudio capture <ssh target> [--seconds N] [--classes ...]` (runs tracelogger with the options above, or daemon mode with only the needed classes, and fetches the file); `tracestudio diff A.kev B.kev`; `--pid/--process` filters; `--sql` to run bundled Perfetto SQL; a `pip install` package with a `tracestudio` entry point; optional zero-dependency protobuf encoder so conversion needs no third-party package.

## 9. Limits

- Verified on x86_64 QEMU, 2 CPUs, one SDP 8.0 build, little-endian only; aarch64le and big-endian traces untested.
- Fast mode has no priorities, no `_msg_info`, only msg[0], no pulse values; inversion analysis needs wide mode (priorities show as 0 in fast-mode sched tables).
- Exchanges already in flight when tracing starts, and anything inside lost buffers, are reported as incomplete; the injected start-of-trace state does not say which thread runs on which CPU, so the first THRUNNING per CPU defines it.
- The actor rule assumes kernel-call and COMM events are emitted on the caller's CPU after its THRUNNING; it held for all 480 requests here but has not been tested with APS, hypervisor (QVM) or adaptive partitioning traces.
- Thread names are a snapshot (last name wins); Perfetto thread ids are synthetic.
- Timestamps assume ClockCycles() synchronised across CPUs, as tracelogger itself requires.
- Pulse send/receive matching is FIFO per (server pid, scoid) and is inferred, not proven.
- The 15 us between a pinned client's ClockCycles() and its MsgSendv entry event (section 6) is not yet explained; it does not affect linking.

## 10. Judge demo storyline (under 3 minutes)

1. Problem (20 s): a control loop thread `hi` (priority 30) asks server `ts_psrv` for work every 25 ms with a 5 ms deadline; a background `hog` (priority 20) and a low-priority client `lo` share CPU 0. `ts_pload` logs "20 of 20 missed".
2. Capture and convert (30 s): `run_scenarios.sh ... prio_fixed`, `scp`, `tracestudio convert prio_fixed.kev`, open in ui.perfetto.dev.
3. The blocking chain (50 s): CPU 0 track shows `hog` running while `psrv-rx` is Runnable; `hi`'s `MsgSend -> ts_psrv` slice is 11 ms long, its state track shows SEND then REPLY while the `psrv-rx state` track shows READY at priority 10; the flow arrow lands on psrv's `handle hi` slice only after hog finishes. `tracestudio analyze --deadline hi=5000` prints 20 suspected inversions with the evidence (hi 30 SEND-blocked, psrv-rx READY at 10 for 8.2 ms, hog 20 ran 8.2 ms on CPU 0, server busy with lo) and the root cause: channel created with `_NTO_CHF_FIXED_PRIORITY`.
4. The fix (40 s): drop the flag (`ts_psrv` without `-f`), recapture: 0 of 20 missed, max 2.7 ms; the `psrv-rx priority` counter now jumps from 10 to 30 when hi sends, and Q3 shows the server running at 30 while handling hi.
5. Why trust it (30 s): 139,421 events and 375,640 argument values identical to traceprinter; 480/480 logged requests linked to the right server thread and rcvid; dropped buffers produce flagged gaps, never wrong links. Why QNX: message-driven priority inheritance is a microkernel IPC property, and only a tool that understands SEND/RECEIVE/REPLY blocking, rcvids and channels can show it.

## 11. Build and run (current)

```
# on a host with QNX SDP 8.0
source ~/qnx800/qnxsdp-env.sh && make -C workload            # builds workload/build/x86_64/ts_*
# copy workload/build/x86_64/* and workload/run_scenarios.sh to the target, then as root:
sh run_scenarios.sh /tmp/ts/bin /tmp/ts/out                   # four .kev files plus CSV logs
# on the host
python3 -m venv venv && venv/bin/pip install perfetto protobuf
venv/bin/python -m tracestudio convert /path/prio_fixed.kev
venv/bin/python -m tracestudio analyze /path/prio_fixed.kev --deadline hi=5000
venv/bin/python tests/test_tracestudio.py
```
