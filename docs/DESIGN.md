# trace-studio design

trace-studio converts QNX OS 8.0 kernel event traces (`.kev` files written by `tracelogger`) into Perfetto traces offline and analyses QNX message passing in them. The QNX Toolkit for VS Code exports traces only as CSV, and QNX's Perfetto port (`qnx-ports/perfetto`) captures live through `traced_qnx_probes` but cannot import a `.kev`. trace-studio takes a `.kev` captured on a QNX 8.0 system, converts it on the host, reconstructs every MsgSend/MsgReceive/MsgReply exchange it can prove, and reports latency, critical paths and suspected priority inversions with their evidence.

Status on 3 Nov 2026 (v0.1 release candidate). Everything marked measured comes from commands run on the build box against traces captured on QNX 8.0.0 x86_64 under QEMU/KVM; the raw traces are on the box under `~/work/ts-data/run1`, `run2` and `run3`, and the small ones needed by the tests are in `tests/data`.

## 1. Architecture

```
 target (QNX 8.0)                              host (Linux, Python 3.10+, no QNX libraries needed)
 ----------------                              ---------------------------------------------------
 workload/ts_*  --msgs-->  procnto (instr)     tracestudio/kev.py       header, syspage, 16-byte slots, combine
                              |                                         reassembly, anchored 64-bit time, loss records
 tracelogger -w -k 32 ...  <--+ kernel buffers tracestudio/events.py    event names, argument layouts (fast and wide),
      |                                                                 message type names (qnx_tables.py)
      v                                        tracestudio/model.py     threads, states, CPU switches, proven wakeups,
  trace.kev  ------------ scp ------------->                            IPC transactions, pulses, run masks, coid paths
  workload CSV logs (ground truth) -------->   tracestudio/analyses.py  Timeline queries, latency, deadlines,
                                                                        server priority, suspected inversions
                                               tracestudio/critical.py  critical path of one request
                                               tracestudio/perfetto_out.py   TracePacket stream
                                               tracestudio/validate.py  checks against workload logs
                                               tracestudio/cli.py       info | convert | analyze | diff | validate
```

The reader is self-contained Python. `libtraceparser` exists in the SDP only for QNX targets, so a host tool cannot link it, and `traceprinter` prints text that is slow and lossy to re-parse; `traceprinter` is used only as the reference in tests (section 6). The Perfetto writer uses the protobuf classes generated in the `perfetto` package (0.59.1) with `protobuf` 7.36.2; it serializes each packet as it is created and frames them as `Trace.packet` entries itself.

## 2. The .kev format as observed (TRACE_VER 2.01, QNX 8.0)

- Text header `TRACE_HEADER_BEGIN::`, `TRACE_<KEY>::<value>` pairs, `TRACE_HEADER_END::`; then `TRACE_SYSPAGE_LEN` bytes of system page; then 16-byte `traceevent_t` slots to the end of the file.
- Header word (from `<sys/trace.h>`, confirmed against traceprinter): bits 31-30 slot structure, bits 29-24 CPU, bit 16 injected (state emitted at trace start), bits 14-10 internal class, bits 9-0 event code. Kernel calls: bits 0-6 call number, bit 7 exit, bit 9 64-bit variant. The interrupted-call variant (`_NTO_TRACE_KERCALLINT`, label INT_CALL) is decoded as bit 8, which no header defines and no capture contained.
- Buffers (measured on all captures): one kernel buffer holds one CPU's events and is written to the file whole. Each starts with CONTROL/TIME and CONTROL/BUFFER; BUFFER's words are the per-CPU sequence number, starting at 1, and the number of slots in the buffer including those two. The SAT guide and the data agree that TIME is also emitted at every rollover of the 32-bit clock LSB (for example 53 TIME events for 51 buffers on CPU 1 of `ipc_wide`, and no LSB wrap without a TIME).
- Payload: `data[0]` is the timestamp LSB of every slot; `data[1]`, `data[2]` of each slot of an event concatenate into its argument words. A combine event's slots share CPU, class, code and timestamp LSB, and other events can be written between them (interlacing, up to 288 us late in `prio_fixed`).
- Message passing as traced (`prio_inherit`): client `KER_CALL MSG_SENDV`, `COMM SND_MESSAGE(rcvid, server pid)`, the server's THREADY at the inherited priority if it was already READY, client THSEND or THREPLY; server `MsgReceivev` entry, client THREPLY, `COMM REC_MESSAGE`, `KER_EXIT MSG_RECEIVEV` with `_msg_info`; `KER_CALL MSG_REPLYV`, `COMM REPLY_MESSAGE(client tid, pid)`, client THREADY, `KER_EXIT MSG_REPLYV`; client `KER_EXIT MSG_SENDV`. Inside MsgSendPulse the receiver's THREADY comes before SND_PULSE.
- Cancellation (`cancel`): a MsgSend ended by TimerTimeout (ETIMEDOUT) or a signal (EINTR) shows as `KER_EXIT MSG_SENDV` with status -1, not as INT_CALL. A server that calls MsgReceive without an info buffer gets an `_msg_info` of zeros.

Reader rules (`kev.py`):
- Timestamps: a TIME slot's full timestamp (`data[1]` MSB, `data[0]` LSB) is an anchor and is taken as is. Every other begin or simple slot is placed in the 2^32-cycle window that starts 2^28 cycles (0.1 s at 2.7 GHz) before its CPU's latest anchor; this resolves slots stamped just before an anchor but written after it without ever moving an anchor. Continuations carry the full timestamp of their begin. Slots before their CPU's first TIME use the latest anchor of any CPU and are counted.
- Loss, tracked in file order per CPU: sequence gaps (32-bit wrap allowed; a backwards jump is a "sequence reset" of unknown size; a first buffer above 1 means earlier buffers are missing), buffers with fewer slots than announced, CONTROL/DROPPED, and a file that ends inside a slot or buffer. Each loss becomes a `Loss(cpu, t0, t1, kind, count)` record covering the time from the CPU's latest timestamp before it to the first one after; gaps are stored as ranges, never expanded. A loss closes every combine event still being assembled on that CPU as partial, so slots from both sides of a gap are never joined; a begin older than 2^28 cycles at its CPU's next anchor is closed as stale.
- Events are sorted by (timestamp, file position).

## 3. Mapping .kev events to Perfetto

| QNX | Perfetto | notes |
|---|---|---|
| THRUNNING on CPU n | ftrace `sched_switch` on CPU n | `prev_state` from the previous thread's QNX state |
| READY proven by the waker's kernel call | ftrace `sched_waking` from the waker | MsgSendv to a RECEIVE-blocked thread of the server, MsgReplyv/MsgError naming the client, MsgSendPulse, ConnectDetach pulses, SyncMutexUnlock, SyncCondvarSignal, SyncSemPost with the matching blocked state; other READYs have no waker (1,400 proven, 3,351 not in `ipc_wide`) |
| CPU time nobody can be credited with | switch to the pseudo thread `UNKNOWN` | after lost events, and on the old CPU of a thread that runs elsewhere before that CPU switched (3 in `ipc_wide`); never fabricated idle |
| lost events | slice on a global `lost events` track | plus UNKNOWN thread states, see section 4 |
| procnto idle threads | pid 0 (swapper) | |
| PROCCREATE_NAME, PROCTHREAD_NAME | ProcessTree (no cmdline) and process track descriptors with the short name | TraceProcessor would otherwise name the process after the path |
| every thread state | slices on `<thread> QNX state` with `qnx_prio` | exact QNX states, UNKNOWN for lost data |
| priority of wide THREAD events | `<thread> QNX priority` counter when it changes; also the sched `priority` column | the QNX value (0 to 255, higher wins), not a Linux prio |
| MsgSendv entry to exit | `MsgSend [<type>] -> <server>` on the client | args txn, rcvid, coid, message, path, msg0-2, client_blocked, parent_txn, outcome, latency_us, trustworthy, warnings |
| MsgReceivev exit to MsgReplyv entry | `handle [<type>] <client>` on the receiving thread | flow from the MsgSend |
| MsgReplyv / MsgError | `MsgReply` / `MsgError` on the replying thread | flow to `reply received` on the client |
| crossing lifetimes of one thread | extra `<thread> (overlap n)` child track | deferred and cross-thread replies; 1 overlap lane in `ipc_wide` |
| critical path of each trustworthy top-level request | `<client> critical path` track: request slice, segments, nested requests | short names, full text in `what` |
| SND_PULSE*, REC_PULSE | instants; a flow only for a unique match | kernel pulses on `CPU n kernel pulses` |
| INT_DELIVER | instant on `CPU n interrupts` | |

Thread ids: Perfetto tids are global and QNX tids are per process, so (pid, tid) becomes pid*100+tid when that fits below 2^31-2^20 (109365702 is pid 1093657 tid 2), a counter otherwise. Timestamps: ns = cycles x 10^9 / TRACE_CYCLES_PER_SEC from the trace's absolute ClockCycles(). Slices are written per track in nesting order (parents begin first, a zero-length slice ends right after it begins) after a lane assignment that keeps every track properly nested, so rounding cannot make a slice end before it begins.

Checked in ui.perfetto.dev v59 with headless Chromium (screenshots in `docs/img`): process names showed paths and critical-path names truncated to the same prefix; both fixed (short names). Measured with TraceProcessor 0.59.1 on all 14 captures: no error or data-loss stats, no flow joining slices of two transactions, no negative duration.

## 4. Message-passing reconstruction (`model.py`)

- Actor: a kernel call or COMM event belongs to the thread most recently switched in on its CPU. After lost events or a migration, or before the CPU's first switch, the actor is unknown and the event is counted (`events_without_actor`) and not linked; partial combine events are not linked either. Events whose actor is not in RUNNING are counted (`actor_not_running`): 104 in `ipc_wide`, all from threads in CREATE or DESTROY doing kernel work in their own context, so they are kept.
- A transaction is opened by SND_MESSAGE inside the sender's MsgSendv and keyed by (server pid, rcvid); rcvids are reused (up to 186 times per key in run 1). REC_MESSAGE names the receiving thread (it must be in the server process), MsgReceivev exit adds `_msg_info`, MsgReplyv/MsgError entry with the rcvid and REPLY_MESSAGE/MSG_ERROR naming the client close the server side, MsgSendv exit ends it.
- Every index (by client, by rcvid, received-not-replied per server thread, replying) is cleaned by object identity, so a late completion can never remove a newer transaction that reuses a key (unit test with a late REPLY_MESSAGE and MsgSendv exit). A key reused while open, a client sending again, a thread dying (THDEAD) or its process being destroyed closes the old transaction with a warning.
- `_msg_info` naming another sender is kept aside (`info_conflict`) and marks the transaction untrustworthy (1 in `ipc_wide`: ts_front's exit-time _IO_CLOSE names tid 1, traced sender tid 2); zeros mean no info buffer and are ignored.
- Outcomes: replied, error, cancelled (MsgSendv returned without REPLY_MESSAGE, as for timeouts and signals), interrupted (INT_CALL).
- Complete means the whole path was seen; trustworthy means complete with no warnings. Analyses use trustworthy transactions only and report the excluded count (682 of 690 in `prio_fixed` are trustworthy).
- Parent: a request sent by a thread with exactly one received, unreplied request is its child. With several, the candidates are recorded and no parent is set (15 in `ipc_wide`, all from procnto's proc_pool_thread).
- Lost events: all open exchanges are flagged ("spans lost events on cpu n"), pending MsgSendv entries are marked, pulse queues become uncertain, the CPU's occupant becomes UNKNOWN, and every thread except those running on another CPU gets an UNKNOWN state until its next event.
- Pulses: a sender is set only when SND_PULSE happens inside the thread's MsgSendPulse (or SND_PULSE_EXE inside MsgDeliverEvent, SND_PULSE_DIS inside ConnectDetach). A receive matches a send only when exactly one pulse is queued for that (server pid, scoid) and nothing made the queue uncertain; otherwise the candidates and a confidence (1/n) are recorded. In `ipc_wide`: 521 pulses have no known sender (517 SND_PULSE_EXE, kernel-delivered events such as ts_back's timer, and 4 SND_PULSE_DIS outside a ConnectDetach), 6 receives are ambiguous, 11 have no traced send.
- Run masks come from `ThreadCtl(_NTO_TCTL_RUNMASK, mask)` entries with a successful exit; the pointer forms leave the mask unknown.
- Message names: msg[0]'s low 16 bits name `_IO_*` types (and the `_IO_CONNECT` subtype in the high 16 bits) for any server, and system manager types (`_PROC_*`, `_MEM_*`, `_SYS_*`, `_PATH_*`) only for messages to procnto; tables are generated from the SDP headers by `tools/gen_qnx_tables.py`. A coid gets the path of the PATHMGR event of the same thread when its ConnectAttach follows within 10 ms; ConnectDetach removes it.

## 5. Analyses

- Latency per (client thread, server process): n, min, p50, p90, p99, max over trustworthy exchanges, excluded count per pair.
- Deadlines (`--deadline THREAD=US`): requests, misses and max per server.
- Server priority while handling each client: the time-weighted priority of the receiving thread while RUNNING between receive and reply. Measured: psrv-rx ran hi's requests at 10 with the fixed-priority channel and at 30 with inheritance; with inheritance it ran lo's requests at 30 for 43.8 ms and at 10 for 37.9 ms, the boost arriving while it still handles lo.
- Suspected priority inversion (`analyses.py`): the client's wait is split at every state change of client and server, every switch on the CPUs the server may use and every run-mask change. An interval is evidence when the client is SEND- or REPLY-blocked on the server thread, the server is READY at Ps below the client's Pc (priorities from the latest thread event at that instant), and every CPU in the server's run mask (all CPUs if none was traced) runs a thread above Ps, at least one below Pc. While SEND-blocked the eventual receiver counts as the dependency only if it is the only thread seen receiving on that channel. Intervals with an unknown occupant, state or priority, or with an eligible CPU running something at or below Ps, count as insufficient evidence. A request is reported when evidence covers `--min-us` (100 us by default), or as insufficient evidence when unexplained time does.
- Critical path (`critical.py`): partitions [MsgSendv entry, exit] into segments without overlap: client running, client ready (who held its CPUs), server waiting (still RECEIVE-blocked), server busy (another client's request, several open requests, or no request), server preempted (who held the server's CPUs, aggregated per segment), server running (receiving, this request, replying), nested request (the child request's own critical path clipped to the server's wait), server blocked, unknown. The sum equals the latency exactly (asserted for every top-level request in the fixtures).
- `diff A B`: deadlines, inversion counts, fixed-priority channels, latency percentiles of the pairs with the largest p99 change, and server priority while handling each client.

Measured (`tracestudio analyze/diff`, deadline 5 ms):

| trace | hi requests | missed | hi max latency | suspected inversions | where |
|---|---|---|---|---|---|
| prio_fixed (run 1) | 20 | 20 | 11429.5 us | 20 | SEND-blocked, about 8.2 ms each, hog (20) on CPU 0, psrv-rx run mask 0x1 |
| prio_inherit (run 1) | 20 | 0 | 2740.4 us | 0 | |
| prio_fixed_rx (run 3) | 20 | 20 | 10760.4 us | 20 | REPLY-blocked, about 10 ms each |
| prio_inherit_rx (run 3) | 20 | 0 | 513.9 us | 0 | |

`ipc_wide`: 0 suspected, 0 insufficient. `ipc_fast` has no priorities: 0 suspected, 12 insufficient evidence. The critical path of a forwarded request (`ipc_wide` txn 975, 553.7 us) splits into server running 208.2 us (front-rx0 receiving, running and replying), nested request 209.8 us (ts_back), server preempted 47.6 us (front-rx0 READY while CPU 0 ran its idle thread and CPU 1 ran back-rx), client ready 86.4 us and client running 1.6 us.

## 6. Validation (measured)

1. Against the host `traceprinter` (`tools/compare_traceprinter.py`, labels normalised because names differ, signed and unsigned prints of one value equal): run 1, 5 traces, 139,421 events identical in (64-bit timestamp, CPU, class, event), 376,523 argument values, 0 mismatches; run 2, 4 traces, 135,749 events, 367,075 values, 0 mismatches; run 3, 5 traces, 396,873 events, 1,454,655 values, 0 mismatches. Per-CPU order differences 0 everywhere. Logs: `~/work/ts-data/run3/evidence_stage1_traces.log`, `evidence_run3.log`.
2. Against workload ground truth (`tracestudio validate`, exit status 1 on any failure): each logged request must contain exactly one decoded transaction from the same client thread naming the same server pid and thread and rcvid, received before the server's post-receive log, replied after its pre-reply log, complete and trustworthy; in wide mode msg[1] must equal the request id; forwarded requests must be children of the client request. Run 1 480/480 and run 2 480/480 (each with 80/80 parent links), run 3 prio scenarios 80/80. Client pulses: 4 logged, 4 matched with their value in wide mode; back-end timer pulses: 413 logged, 413 received.
3. Cancellation (`tests/test_tracestudio.py`): the 15 sends of `cancel` decode with their logged outcome (5 replied, 5 ETIMEDOUT and 5 EINTR as cancelled with status -1), and the 10 failed late MsgReply calls are the 10 replies the decoder could not attach.
4. Resource managers: bytes returned by `cat`'s `_IO_READ` per coid path equal `wc -c` of the file for all 26 files of `rm_traffic` (librcheck.so, a symlink, counts under librcheck.so.1) and both files of `rm_small` (132,400 and 65,536 bytes).
5. Fault injection (`tools/drop_buffer.py`, buffers 79, 80, 83, 84 of `ipc_wide`, two per CPU inside the workload window): 4 losses reported, 127 of 200 requests matched, 62 without a candidate, 0 wrong links, validation fails.
6. Unit tests on synthetic slot streams (`tests/kevgen.py`, `tests/test_synthetic.py`, 26 tests) and regression tests on real captures (`tests/test_tracestudio.py`, 9 tests): 35 pass on Python 3.10 on the build box with `TS_REQUIRE_ALL=1` (traceprinter and TraceProcessor required) and under pytest on Python 3.12.
7. Performance on the build box: `ipc_wide` (1.7 MB, 44,807 events) converts in 0.83 s, 103 MB peak RSS; `rm_traffic` (12.4 MB, 306,436 events) in 7.0 s, 720 MB; `analyze` 0.32 s and 2.4 s (`~/work/ts-data/run3/evidence_perf.log`).

## 7. Capture (exact commands used)

Target: QNX 8.0.0 x86_64 under QEMU/KVM built with `mkqnximage --type=qemu --arch=x86_64` (2 CPUs, 1 GB), TRACE_CYCLES_PER_SEC 2699969068 in every capture; runs 1 and 2 come from stage one, run 3 from target ta. Workload built with `qcc -Vgcc_ntox86_64 -O2 -g -Wall -Wextra -std=gnu11` (`make -C workload`, no warnings) and run by `workload/run_scenarios.sh`, which calls

```
tracelogger -w -k 32 -b 128 -s 4 -A SCENARIO=<name> -f <out>/<name>.kev     # ipc_fast: no -w
```

(`-s 3` for cancel and rm_traffic, `-s 2` for rm_small). Non-daemon mode enables all classes; `-w` adds priorities, `_msg_info` and 12 bytes of each message; `-k 32 -b 128` gave no lost buffers and no dropped events in any capture. Scenarios: `ipc_*` (ts_back with a 5 ms timer pulse, ts_front with two receive threads forwarding every fourth request, ts_clients 4 x 40 requests and a MsgSendPulse every tenth request of client 0); `prio_*` (ts_psrv one thread at FIFO 10 on CPU 0, `-f` for `_NTO_CHF_FIXED_PRIORITY`; ts_pload lo 10 asks for 4 ms at t+0, hog 20 burns 10 ms at t+2 ms, hi 30 asks for 0.5 ms at t+4 ms, period 25 ms, 20 periods, all on CPU 0; work is a calibrated loop, so preemption delays it); `prio_*_rx` (`ts_pload -l 500 -o 0,4200,4000`); `cancel` (`ts_cancel -n 5`); `rm_traffic` and `rm_small` (shell commands on /system and /data).

## 8. CLI

```
tracestudio info     TRACE.kev                                   # header, losses, decoder counters (JSON)
tracestudio convert  TRACE.kev [-o OUT] [--no-critical-path]     # Perfetto trace
tracestudio analyze  TRACE.kev [--deadline THREAD=US] [--request TXN] [--slowest N] [--client TEXT]
                               [--min-us US] [--top N] [--json]
tracestudio diff     BEFORE.kev AFTER.kev [--deadline THREAD=US] [--client TEXT] [--json]
tracestudio validate TRACE.kev --logs DIR                        # workload ground truth (JSON)
python3 tools/compare_traceprinter.py TRACE.kev TRACE.txt        # oracle check
python3 tools/tp_queries.py OUT.perfetto-trace                   # SQL: tables, latency, chains, server priority
python3 tools/drop_buffer.py IN.kev OUT.kev N...                 # fault injection
```

Exit status 0 on success, 1 for failed validation, missing logs, unreadable or empty traces, 2 for usage errors.

## 9. Limits

- Verified on x86_64 QEMU with 2 CPUs and one SDP 8.0 build, little-endian; aarch64le, more CPUs, real hardware, adaptive partitioning and QVM are untested; big-endian traces are refused.
- Loss handling is tested with synthetic streams and removed buffers, not with a capture that lost data; INT_CALL decoding is untested on real data.
- Fast mode has no priorities, `_msg_info`, message payload beyond msg[0] or pulse values.
- The actor rule (section 4) is an inference that held for all 1,040 logged requests.
- Wakeups without a proving kernel call have no waker in Perfetto; Perfetto's derived thread states are Linux states; the QNX state track is exact.
- Inversion analysis is one level deep (no chains through blocked servers or mutexes); the critical path does not follow mutexes, condvars or handoffs to other threads.
- Thread names are a snapshot (last name wins); Perfetto thread ids are synthetic; a thread id reused after THDEAD is the same Perfetto thread.
- Timestamps assume ClockCycles() synchronised across CPUs, as tracelogger itself requires.
- In pinned priority runs the client's ClockCycles() is about 15 us before its MsgSendv entry event; the disassembly of our own ts_pload shows only register moves and the MsgSend call between the two, so the time is spent before the event is written. Cause open; linking is unaffected.

## 10. Review items (GPT-6 Astra on stage one) and what was done

- A, loss: confirmed (only buffer gaps in time order, `range()` over the gap, combines joined across gaps, states continued). Fixed as in sections 2 and 4; unit tests for each case.
- B, timestamps: confirmed (the neighbour rule was applied to TIME slots themselves, so a TIME after more than 2^31 cycles of silence was moved to the previous epoch; unit test). Fixed with anchors and window placement.
- C, transactions: confirmed (reuse removed only one index; a late REPLY_MESSAGE could delete a newer transaction). Fixed; `_msg_info` conflicts quarantined; complete and trustworthy separated; ambiguous parents recorded. KER_INT is handled, but real timeouts and signals turned out to end with KER_EXIT, which the cancel capture checks.
- D, inversion: confirmed (priorities backfilled from the future, any CPU counted, receive window only). Rewritten as in section 5 with negative tests (unrelated CPU, plain blocking, idle CPU without run mask) and positives before and after the receive, on synthetic streams and real captures. Not done: transitive chains (documented limit).
- E, slices: confirmed in principle (END closed the latest BEGIN on a shared track); fixed with lanes and nesting-order emission, checked by exact TraceProcessor tests.
- F, scheduler: migration idle replaced by UNKNOWN, wakeups only when proven. Kept: QNX priorities in the sched priority column, unconverted and labelled, because the per-slice priority is what a QNX engineer needs on the CPU tracks; separate QNX priority tracks exist. The critical path reports blocked, runnable and executing time separately.
- G, pulses: confirmed (timer pulses inherited the running thread as sender); fixed with call-based senders and unique-match arrows.
- validate.py and cli.py: parent-link failures, empty logs and failed validation now give exit status 1.
- Not adopted for v0.1: live or SSH capture, mutex owner chains, interrupt-to-IST latency, aarch64 fixtures (all listed as next steps).

## 11. Demo storyline (165 s)

1. Problem (15 s): hi (priority 30) asks ts_psrv for 0.5 ms of work every 25 ms with a 5 ms budget for its MsgSend; hog (20) and lo (10) share CPU 0. The log says 20 of 20 missed.
2. Capture (20 s): `run_scenarios.sh /tmp/ts/bin /tmp/ts/out prio_fixed`, `scp`, `tracestudio convert`.
3. Diagnosis (55 s): in ui.perfetto.dev, CPU 0 shows hog holding the CPU while psrv-rx is preempted; hi's MsgSend arrow lands on psrv-rx only after lo's request; hi's critical path says 71% "psrv-rx READY (10), CPU: hog (20)". `tracestudio analyze --deadline hi=5000` lists 20 suspected inversions with the evidence and the channel created with `_NTO_CHF_FIXED_PRIORITY`.
4. Fix and comparison (40 s): drop `-f`, recapture, `tracestudio diff`: 0 of 20 missed, max 2.7 ms, psrv-rx runs hi's requests at 30; the QNX priority track shows the boost arriving while psrv-rx still handles lo.
5. Trust (20 s): 672,043 events and 2.2 million argument values identical to traceprinter, 1,040/1,040 logged requests linked, dropped buffers give flagged gaps and never wrong links.
6. Value (15 s): the same view works for any server, for example `MsgSend _IO_READ -> devb-eide` with the file path.

## 12. Build and run

```
source ~/qnx800/qnxsdp-env.sh && make -C workload          # workload/build/x86_64/ts_*
# copy workload/build/x86_64/* and workload/run_scenarios.sh to the target, then as root:
sh run_scenarios.sh /tmp/ts/bin /tmp/ts/out                 # .kev files plus CSV logs
python3 -m venv venv && venv/bin/pip install .
venv/bin/tracestudio convert /path/prio_fixed.kev
venv/bin/tracestudio analyze /path/prio_fixed.kev --deadline hi=5000
TS_REQUIRE_ALL=1 venv/bin/python tests/test_tracestudio.py  # with the SDK env sourced for traceprinter
```
