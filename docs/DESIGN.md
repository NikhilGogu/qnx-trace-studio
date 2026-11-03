# trace-studio design

trace-studio converts QNX OS 8.0 kernel event traces (`.kev` files written by `tracelogger`) into Perfetto traces offline and analyses QNX message passing in them. The QNX Toolkit for VS Code exports traces only as CSV, and QNX's Perfetto port (`qnx-ports/perfetto`) captures live through `traced_qnx_probes` but cannot import a `.kev`. trace-studio takes a `.kev` captured on a QNX 8.0 system, converts it on the host, links the MsgSend/MsgReceive/MsgReply exchanges it can establish, and reports latency, critical paths and suspected priority inversions with their evidence, or says that the trace does not support a conclusion.

Status on 3 Nov 2026 (v0.1 release candidate after the second review). Everything marked measured comes from commands run on the build box against 14 traces captured on QNX 8.0.0 x86_64 under QEMU/KVM: runs 1 and 2 from stage one, run 3 from target ta. The raw traces are on the box under `~/work/ts-data/run1`, `run2` and `run3`; the small ones the tests need are in `tests/data`. The measurements show what trace-studio does on these workloads on this target (fixture coverage); they are not a proof for other traces.

## 1. Architecture

```
 target (QNX 8.0)                              host (Linux, Python 3.10+, no QNX libraries needed)
 ----------------                              ---------------------------------------------------
 workload/ts_*  --msgs-->  procnto (instr)     tracestudio/kev.py       header, syspage, 16-byte slots, buffer validation,
                              |                                         combine reassembly, anchored 64-bit time, loss records
 tracelogger -w -k 32 ...  <--+ kernel buffers tracestudio/events.py    event names, argument layouts (fast and wide),
      |                                                                 message type names (qnx_tables.py)
      v                                        tracestudio/model.py     threads, states, CPU switches, wakeups with evidence,
  trace.kev  ------------ scp ------------->                            IPC transactions, pulses, run masks, coid paths
  workload CSV logs (ground truth) -------->   tracestudio/analyses.py  Timeline queries, latency, deadlines,
                                                                        server priority, suspected inversions
                                               tracestudio/critical.py  critical path of one request
                                               tracestudio/perfetto_out.py   TracePacket stream
                                               tracestudio/validate.py  checks against workload logs
                                               tracestudio/cli.py       info | convert | analyze | diff | validate
```

The reader is self-contained Python. `libtraceparser` exists in the SDP only for QNX targets, so a host tool cannot link it, and `traceprinter` prints text that is slow and lossy to re-parse; `traceprinter` is used only as a black-box reference in tests (section 6). QNX behaviour comes from the SDP headers, the published documentation, and that comparison. The Perfetto writer uses the protobuf classes generated in the `perfetto` package (0.59.1) with `protobuf` 7.36.2; it serializes each packet as it is created and frames them as `Trace.packet` entries itself.

## 2. The .kev format as observed (TRACE_VER 2.01, QNX 8.0)

- Text header `TRACE_HEADER_BEGIN::`, `TRACE_<KEY>::<value>` pairs, `TRACE_HEADER_END::`; then `TRACE_SYSPAGE_LEN` bytes of system page; then 16-byte `traceevent_t` slots to the end of the file.
- Header word (from `<sys/trace.h>`, confirmed against traceprinter): bits 31-30 slot structure, bits 29-24 CPU, bit 16 injected (state emitted at trace start), bits 14-10 internal class, bits 9-0 event code. Kernel calls: bits 0-6 call number, bit 7 exit, bit 9 64-bit variant. The interrupted-call variant (`_NTO_TRACE_KERCALLINT`, label INT_CALL) is decoded as bit 8, which no header defines and no capture contained.
- Buffers (measured on all captures): one kernel buffer holds one CPU's events and is written whole. Each starts with CONTROL/TIME and CONTROL/BUFFER; BUFFER's words are the per-CPU sequence number, starting at 1, and the number of slots in the buffer including those two. The SAT guide and the data agree that TIME is also emitted at every rollover of the 32-bit clock LSB.
- Payload: `data[0]` is the timestamp LSB of every slot; `data[1]`, `data[2]` of each slot of an event concatenate into its argument words. A combine event's slots share CPU, class, code and timestamp LSB, and other events can be written between them (interlacing, up to 288 us late in `prio_fixed`).
- Message passing as traced (`prio_inherit`): client `KER_CALL MSG_SENDV`, `COMM SND_MESSAGE(rcvid, server pid)`, the server's THREADY at the inherited priority if it was already READY, client THSEND or THREPLY; server `MsgReceivev` entry, client THREPLY, `COMM REC_MESSAGE`, `KER_EXIT MSG_RECEIVEV` with `_msg_info`; `KER_CALL MSG_REPLYV`, `COMM REPLY_MESSAGE(client tid, pid)`, client THREADY, `KER_EXIT MSG_REPLYV`; client `KER_EXIT MSG_SENDV`. Inside MsgSendPulse the receiver's THREADY comes before SND_PULSE. A thread blocks in RECEIVE inside its MsgReceivev call.
- Cancellation (`cancel`): a MsgSend ended by TimerTimeout (ETIMEDOUT) or a signal (EINTR) shows as `KER_EXIT MSG_SENDV` with status -1, not as INT_CALL. A server that calls MsgReceive without an info buffer gets an `_msg_info` of zeros.
- ThreadCtlExt (wide) carries pid, tid, cmd and data; `_NTO_CHF_INHERIT_RUNMASK` is 0x4000 and SCHED_SPORADIC is 4 in the SDP headers.

Reader rules (`kev.py`):
- Buffers are validated first: a pass over the slots finds every BUFFER header and compares the slots up to the next one with the announced count. A buffer that differs may miss slots anywhere inside it; it becomes a loss window from its own TIME anchor to that CPU's next buffer, and every combine event that has a slot in it is kept as partial (not trusted). Only a file cut inside its last slot is treated as missing data at the end.
- Timestamps: a TIME slot's full timestamp (`data[1]` MSB, `data[0]` LSB) is an anchor and is taken as is. Every other begin or simple slot is placed in the 2^32-cycle window that starts 2^28 cycles (0.1 s at 2.7 GHz) before its CPU's latest anchor. Continuations carry the full timestamp of their begin. A slot before its CPU's first TIME borrows another CPU's epoch; its event is marked partial and not trusted (0 such slots in all captures).
- Loss, tracked in file order per CPU: sequence gaps (32-bit wrap allowed; a backwards jump is a "sequence reset" of unknown size; a first buffer above 1 means earlier buffers are missing, a loss from the trace start), buffers with a wrong slot count, CONTROL/DROPPED, and truncated files. Each loss becomes a `Loss(cpu, t0, t1, kind, count)` record; gaps are stored as ranges, never expanded. A loss closes every combine still being assembled on that CPU as partial; a begin older than 2^28 cycles at its CPU's next anchor is closed as stale.
- Events are sorted by (timestamp, file position).

## 3. Mapping .kev events to Perfetto

| QNX | Perfetto | notes |
|---|---|---|
| THRUNNING on CPU n | ftrace `sched_switch` on CPU n | `prev_state` from the previous thread's QNX state; not inside a loss window of CPU n |
| READY with object evidence | ftrace `sched_waking` from the waker, no target CPU | the woken thread later receives the message or pulse sent in the waker's call, or the waker's REPLY_MESSAGE names it; 1,296 with evidence and 3,455 without in `ipc_wide` |
| CPU time nobody can be credited with | switch to the pseudo thread `UNKNOWN` | loss windows, partial THRUNNING, and the old CPU of a thread that runs elsewhere before that CPU switched; never fabricated idle |
| lost events | slice on a global `lost events` track | plus UNKNOWN thread states, see section 4 |
| procnto idle threads | pid 0 (swapper) | |
| PROCCREATE_NAME, PROCTHREAD_NAME | ProcessTree (no cmdline) and process track descriptors with the short name | TraceProcessor would otherwise name the process after the path |
| thread states | slices on `<thread> QNX state` with `qnx_prio` | the kernel's states, UNKNOWN where events were lost |
| priority of wide THREAD events | `<thread> QNX priority` counter when it changes; also the sched `priority` column | the QNX value (0 to 255, higher wins), not a Linux prio (decision of the lane lead) |
| MsgSendv entry to exit | `MsgSend [<type>] -> <server>` on the client | args txn, rcvid, coid, message, path, msg0-2, client_blocked, parent_txn, outcome, latency_us, trustworthy, warnings; `<type>` only on a resource-manager connection |
| MsgReceivev exit to MsgReplyv entry | `handle [<type>] <client>` on the receiving thread | flow from the MsgSend |
| MsgReplyv / MsgError | `MsgReply` / `MsgError` on the replying thread | flow to `reply received` on the client |
| crossing lifetimes of one thread | extra `<thread> (overlap n)` child track | deferred and cross-thread replies |
| critical path of each trustworthy top-level request | `<client> critical path` track | short names, full text in `what` |
| SND_PULSE*, REC_PULSE | instants; a flow only for a unique match | kernel pulses on `CPU n kernel pulses`; `candidate_count` otherwise |
| INT_DELIVER | instant on `CPU n interrupts` | |

Thread ids: Perfetto tids are global and QNX tids are per process, so (pid, tid) becomes pid*100+tid when that fits below 2^31-2^20, a counter otherwise. Timestamps: ns = cycles x 10^9 / TRACE_CYCLES_PER_SEC. Slices are written per track in nesting order (parents begin first, a zero-length slice ends right after it begins) after a lane assignment that keeps every track properly nested, so rounding cannot make a slice end before it begins.

Checked in ui.perfetto.dev v59 with headless Chromium (screenshots in `docs/img`, taken before the second review). Measured with TraceProcessor 0.59.1 on all 14 captures: no error or data-loss stats, no flow joining slices of two transactions, no negative duration.

## 4. Message-passing reconstruction (`model.py`)

- Actor: a kernel call or COMM event belongs to the thread most recently switched in on its CPU. Inside a loss window of that CPU, after a migration, after a partial THRUNNING, or before the CPU's first switch, the actor is unknown and the event is counted and not linked; partial events are not linked either. Events whose actor is not RUNNING are counted (104 in `ipc_wide`, all threads in CREATE or DESTROY doing kernel work in their own context) and kept.
- A transaction is opened by SND_MESSAGE inside the sender's MsgSendv and keyed by (server pid, rcvid); rcvids are reused (up to 186 times per key in run 1). REC_MESSAGE names the receiving thread (it must be in the server process), MsgReceivev exit adds `_msg_info`, MsgReplyv/MsgError entry with the rcvid and REPLY_MESSAGE/MSG_ERROR naming the client close the server side, MsgSendv exit ends it.
- Every index is cleaned by object identity. A REPLY_MESSAGE that is not inside the reply call in progress for that client's exchange is quarantined: it changes nothing and marks the exchange untrustworthy. A key reused while open, a client sending again, a thread dying or its process being destroyed closes the old transaction with a warning.
- `_msg_info` naming another sender is kept aside and marks the transaction untrustworthy; zeros mean no info buffer and are ignored.
- Outcomes: replied, error, cancelled (MsgSendv returned without REPLY_MESSAGE), interrupted (INT_CALL). Complete means the whole path was seen; trustworthy means complete with no warnings. Analyses use trustworthy transactions only and report the excluded and unfinished counts (672 of 690 trustworthy in `prio_fixed`).
- Parent: a request sent by a thread with exactly one received, unreplied request is its child. With several, the candidates are recorded, no parent is set and the request carries a warning (15 in `ipc_wide`, all from procnto's proc_pool_thread).
- Loss windows (any loss: buffers, wrong slot count, DROPPED, truncation, losses before the first buffer): when a window opens, the CPU's occupant becomes UNKNOWN; kernel calls in progress, coid paths, path candidates, confirmed resource-manager connections, pending wakeup evidence and pulse queue synchronisation are dropped; known run masks become unknown; every thread gets an UNKNOWN state until its first event after the window ends. A thread event inside any loss window holds only at its instant (UNKNOWN follows it). At the end of the pass every exchange whose lifetime overlaps a loss window is flagged.
- Pulses: a sender is set only when SND_PULSE happens inside the thread's MsgSendPulse (or SND_PULSE_EXE inside MsgDeliverEvent, SND_PULSE_DIS inside ConnectDetach). Queues of (server pid, scoid) start unsynchronised; a receive inside MsgReceivev(chid) tells which channel the scoid's pulses arrive on, and when a thread blocks in RECEIVE in MsgReceivev on that channel nothing is pending there, so the queue is empty and in sync (pulses still queued are marked "unseen"). A receive is "unique" only with one queued pulse on a synchronised queue; with several it is "ambiguous", with one on an unsynchronised queue "unsynced", and the candidates are listed. Losses and ambiguous receives drop sync. In `ipc_wide`: 631 unique, 35 unsynced, 3 ambiguous, 26 without a traced send, 15 unseen.
- Run masks: `ThreadCtl` and `ThreadCtlExt` with `_NTO_TCTL_RUNMASK` set a known mask after a successful exit; the pointer forms, thread death, loss windows, and receiving a message on a channel whose flags are not in the trace or include `_NTO_CHF_INHERIT_RUNMASK` make it unknown.
- Paths: PATHMGR(pid, tid, path) of the opening thread followed by its ConnectAttach within 10 ms makes a candidate; the coid gets the path only when its first message is an `_IO_CONNECT` the server answers without error. ConnectDetach, a new ConnectAttach on the coid and losses remove it.
- Message names: msg[0]'s low 16 bits give the type and, for `_IO_CONNECT`, the high 16 bits the subtype (fast mode keeps msg[0]). System manager types (`_PROC_*`, `_MEM_*`, `_SYS_*`, `_PATH_*`) are named only for messages to procnto; `_IO_*` types only on a resource-manager connection: to procnto, on a coid confirmed by an answered `_IO_CONNECT`, to a channel that answered an `_IO_CONNECT` in the trace, or the answered `_IO_CONNECT` itself. Others are described as `type 0x....` (81 such messages in `rm_traffic`). Tables are generated from the SDP headers by `tools/gen_qnx_tables.py`.

## 5. Analyses

- Latency per (client role, server process), where the role is (process, thread name, priority at send): n, min, p50, p90, p99, max over trustworthy exchanges, with excluded and unfinished counts and the number of client threads merged into the row.
- Deadlines (`--deadline THREAD=US`): requests, misses, max, excluded and unfinished per server.
- Server priority while handling each client: each server thread's running time while it holds exactly one trustworthy request is credited to that request's client at the priority it ran at; while it holds several, the time is credited once to "(several requests open)".
- Suspected priority inversion: the client's wait is split at every state change of client and server, every switch on the CPUs and every run-mask change. An interval is evidence when the client is SEND- or REPLY-blocked on the server thread, the server is READY at Ps below the client's Pc (priorities from the latest thread event at that instant), the server's run mask is known and names at least one CPU of the trace, and every CPU in it runs a thread above Ps, at least one of them below Pc. While SEND-blocked the server thread must be provably the only live thread of its process (process created during the trace, or its threads listed at trace start). An unknown mask, occupant, state or priority, an eligible CPU at or below Ps, other possible receivers, SCHED_SPORADIC, or APS events in the trace count as insufficient evidence. A request is reported when evidence covers `--min-us` (100 us by default), or as insufficient evidence when unexplained time does.
- Critical path: partitions [MsgSendv entry, exit] into segments without overlap: client running, client ready, server waiting, server busy (another client's request, several open requests including unreplied ones, or no request), server preempted, server running (receiving, this request, replying), nested request (the trustworthy child's own critical path clipped to the server's wait, with READY attribution recomputed from the clipped pieces), server blocked (including untrustworthy children, "not followed"), unexplained ready (an idle, lower-priority or unknown occupant on a CPU the thread may use, or a mask naming no CPU of the trace), unknown. The sum equals the latency exactly (asserted in the tests).
- `diff A B`: deadlines, inversion counts, fixed-priority channels, latency rows matched by role with the largest p99 change, and server priority while handling each client.

Measured (`tracestudio analyze` and the rerun script, deadline 5 ms):

| trace | hi requests | missed | hi max latency | suspected inversions | evidence |
|---|---|---|---|---|---|
| prio_fixed (run 1) | 20 | 20 | 11429.5 us | 20 | SEND-blocked, median 8187.9 us, hog (20) on CPU 0, psrv-rx run mask 0x1 |
| prio_inherit (run 1) | 20 | 0 | 2740.4 us | 0 | |
| prio_fixed_rx (run 3) | 20 | 20 | 10760.4 us | 20 | REPLY-blocked, median 10046.2 us |
| prio_inherit_rx (run 3) | 20 | 0 | 513.9 us | 0 | |

`ipc_wide`, `cancel`, `rm_traffic`, `rm_small`: 0 suspected, 0 insufficient. `ipc_fast` has no priorities: 0 suspected, 12 insufficient evidence. Server priority while handling: psrv-rx ran hi's requests at 10 with the fixed-priority channel and at 30 with inheritance; with inheritance it ran lo's requests at 30 for 43.8 ms and at 10 for 37.9 ms (the boost arrives while it still handles lo). The critical path of a forwarded request (`ipc_wide` txn 975, 553.7 us) splits into nested request 209.8 us (ts_back), server running 208.2 us, client ready 86.4 us, unexplained ready 47.0 us (front-rx0 READY while CPU 0 was idle and no run mask was traced), client running 1.6 us, server preempted 0.6 us, server waiting 0.1 us.

## 6. Validation (measured)

Commands: `tools/compare_traceprinter.py`, `tracestudio validate`, `tracestudio analyze`, the regression and unit tests, and a rerun script over all 14 captures; its log is `~/work/ts-data/run3/evidence_phase1b.log` on the box (with `evidence_tests_phase1b.log` and `evidence_perf.log`).

1. Against the host `traceprinter` (labels normalised because names differ, signed and unsigned prints of one value equal): run 1, 5 traces, 139,421 events identical in (64-bit timestamp, CPU, class, event), 376,732 argument values; run 2, 4 traces, 135,749 events, 367,276 values; run 3, 5 traces, 396,873 events, 1,454,831 values. 0 mismatches and 0 per-CPU order differences everywhere.
2. Against workload ground truth (`tracestudio validate`, exit status 1 on any failure): each logged request must contain exactly one decoded transaction from the same client thread naming the same server pid, thread and rcvid, received before the server's post-receive log, replied after its pre-reply log, complete and trustworthy; in wide mode msg[1] must equal the request id; forwarded requests must be children of the client request. Run 1 480/480 and run 2 480/480 (each with 80/80 parent links), run 3 prio scenarios 80/80. Client pulses: 4 logged, 4 matched with their value in wide mode; back-end timer pulses: 413 logged, 413 received.
3. Cancellation (unit test on `tests/data/run3/cancel`): the 15 sends decode with their logged outcome (5 replied, 5 ETIMEDOUT and 5 EINTR as cancelled with status -1), and the 10 failed late MsgReply calls are the 10 replies the decoder could not attach.
4. Resource managers: bytes returned by `cat`'s `_IO_READ` per coid path equal `wc -c` of the file for all 26 files of `rm_traffic` (librcheck.so, a symlink, counts under librcheck.so.1) and both files of `rm_small` (132,400 and 65,536 bytes).
5. Fault injection (`tools/drop_buffer.py`, buffers 79, 80, 83, 84 of `ipc_wide`): 4 losses reported, 108 of 200 requests matched, 62 without a candidate, none matched to a wrong exchange in this test, validation fails.
6. Tests: 47 unit tests on synthetic slot streams (`tests/kevgen.py`, `tests/test_synthetic.py`) and 10 regression tests on real captures (`tests/test_tracestudio.py`): 57 pass on Python 3.10 on the build box with `TS_REQUIRE_ALL=1` (traceprinter and TraceProcessor required) and under pytest on Python 3.12.
7. Performance on the build box: `ipc_wide` (1.7 MB, 44,807 events) converts in 0.86 s, 102 MB peak RSS; `rm_traffic` (12.4 MB, 306,436 events) in 7.4 s, 725 MB; `analyze` 0.37 s and 2.7 s.

## 7. Capture (exact commands used)

Target: QNX 8.0.0 x86_64 under QEMU/KVM built with `mkqnximage --type=qemu --arch=x86_64` (2 CPUs, 1 GB), TRACE_CYCLES_PER_SEC 2699969068 in every capture. Workload built with `qcc -Vgcc_ntox86_64 -O2 -g -Wall -Wextra -std=gnu11` (`make -C workload`, no warnings) and run by `workload/run_scenarios.sh`, which calls

```
tracelogger -w -k 32 -b 128 -s 4 -A SCENARIO=<name> -f <out>/<name>.kev     # ipc_fast: no -w
```

(`-s 3` for cancel and rm_traffic, `-s 2` for rm_small). Non-daemon mode enables all classes; `-w` adds priorities, `_msg_info` and 12 bytes of each message; `-k 32 -b 128` gave no lost buffers and no dropped events in any capture. Scenarios: `ipc_*` (ts_back with a 5 ms timer pulse, ts_front with two receive threads forwarding every fourth request, ts_clients 4 x 40 requests and a MsgSendPulse every tenth request of client 0); `prio_*` (ts_psrv one thread at FIFO 10 on CPU 0, creating its channel and run mask during the trace, `-f` for `_NTO_CHF_FIXED_PRIORITY`; ts_pload lo 10 asks for 4 ms at t+0, hog 20 burns 10 ms at t+2 ms, hi 30 asks for 0.5 ms at t+4 ms, period 25 ms, 20 periods, all on CPU 0; work is a calibrated loop, so preemption delays it); `prio_*_rx` (`ts_pload -l 500 -o 0,4200,4000`); `cancel` (`ts_cancel -n 5`); `rm_traffic` and `rm_small` (shell commands on /system and /data).

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

Exit status 0 on success, 1 for failed validation, missing logs, unreadable or empty traces, 2 for usage errors (unit test `test_cli_exit_codes`).

## 9. Limits

- Verified on x86_64 QEMU with 2 CPUs and one SDP 8.0 build, little-endian; aarch64le, more CPUs, real hardware, adaptive partitioning and QVM are untested; big-endian traces are refused.
- Loss handling is tested with synthetic streams and removed buffers, not with a capture that lost data; INT_CALL decoding is untested on real data.
- Fast mode has no priorities, `_msg_info`, message payload beyond msg[0] or pulse values.
- The actor rule (section 4) is an inference that held for all 1,040 logged requests.
- Inversion evidence needs a traced run mask and, while SEND-blocked, a single-threaded server process; it is one level deep (no chains through blocked servers or mutexes). The critical path does not follow mutexes, condvars or handoffs to other threads.
- Wakeups without object evidence have no waker in Perfetto; Perfetto's derived thread states are Linux states.
- Thread names are a snapshot (last name wins); Perfetto thread ids are synthetic; a thread id reused after THDEAD is the same Perfetto thread.
- Timestamps assume ClockCycles() synchronised across CPUs, as tracelogger itself requires.
- In pinned priority runs the client's ClockCycles() is about 15 us before its MsgSendv entry event; the disassembly of our own ts_pload shows only register moves and the MsgSend call between the two, so the time is spent before the event is written. Cause open; linking is unaffected.

## 10. Review findings (GPT-6 Astra) and their status

First review (stage one code):

| item | status | tests |
|---|---|---|
| A loss: gaps in time order, `range()` over gaps, combines joined across gaps, states continued | fixed | test_lost_buffer_never_joins_combine_slots, test_short_buffer_is_a_loss, test_sequence_wrap_reset_and_huge_gap, test_dropped_events_make_state_unknown_and_exchanges_untrustworthy, test_truncated_file, test_fault_injection_never_links_wrong |
| B timestamps: neighbour rule overrode TIME after long silence, stale combines | fixed | test_time_anchor_is_authoritative_after_long_silence, test_interlaced_slots_around_a_rollover, test_interlaced_combine_takes_the_begin_timestamp, test_combine_across_consecutive_buffers |
| C transactions: one-index cleanup, actorless and partial events, KER_INT, `_msg_info` conflicts, complete vs trustworthy, ambiguous parents | fixed | test_rcvid_reuse_and_late_completion_keep_the_new_exchange, test_thread_death_and_id_reuse, test_interrupted_send_and_late_reply, test_cancelled_send_is_complete_but_untrustworthy, test_injected_only_start, test_untrustworthy_child_is_not_followed, test_ambiguous_parent_is_not_trustworthy, test_cancellation_matches_the_workload_log |
| D inversion: backfilled priorities, unrelated CPUs, receive window only | fixed; transitive chains open | test_inversion_positive_after_receive, test_inversion_before_receive_is_found_too, test_no_inversion_from_an_unrelated_cpu, test_no_inversion_for_plain_blocking, test_inversion_after_receive_on_a_real_capture, test_priority_story |
| E slices: END closed the latest BEGIN, rounding, flows | fixed | test_cross_thread_and_deferred_replies, test_perfetto_exact_slices_threads_and_flows, test_zero_length_and_rounded_slices_nest, test_perfetto_loads |
| F scheduler: migration idle, inferred wakeups, Linux priority semantics, blocked time label | fixed (wakeups in the second review); QNX priorities kept in the sched priority column by decision, labelled | test_migration_marks_old_cpu_unknown_not_idle, test_wakeup_needs_object_evidence |
| G pulses: FIFO pairing, kernel senders | fixed (second review) | test_pulse_matching_needs_a_synchronised_queue, test_pulse_queue_loses_sync_at_a_loss |
| validate.py and cli.py: parent links, empty logs, exit codes | fixed | test_ground_truth_links, test_cli_exit_codes |
| tests to add (rollover, interlacing, missing slots, wrap, DROPPED, truncation, mixed modes, injected start, migration, KER_INT, cancellation, id reuse, cross-thread replies, ambiguous parents, reordered pulses, exact Perfetto checks) | done | the tests above, test_mixed_fast_and_wide_events |
| design: inspect the UI, pin dependencies, support matrix instead of "any .kev" | done | screenshots in docs/img, pyproject.toml, README |
| later work: mutex chains, aarch64 fixtures, interrupt-to-IST latency, SSH capture, HTML report | open | |

Second review (v0.1 code):

| item | status | tests |
|---|---|---|
| 1 loss: completed combines in a short buffer trusted; initial losses ignored; masks, paths, calls kept; UNKNOWN ended by an intact CPU's READY inside the gap; partial thread events restored actors | fixed | test_deleted_middle_slot_taints_combine_and_exchange, test_loss_at_trace_start_flags_overlapping_exchange, test_loss_drops_run_mask_path_and_calls, test_ready_inside_a_loss_window_does_not_end_unknown, test_partial_thread_event_does_not_restore_actor |
| B unanchored epochs are guesses | fixed (such events are not trusted) | test_slots_before_their_cpus_first_time_are_not_trusted |
| 2 inversion: unknown mask taken as all CPUs; masks after loss and inheriting channels; one observed receiver taken as exclusive; policy evidence; wording | fixed | test_unknown_run_mask_is_not_all_cpus, test_run_mask_dropped_after_loss_and_inheriting_channel, test_send_phase_needs_a_sole_receiver, test_sporadic_server_is_insufficient_evidence, test_inversion_positive_after_receive (wording) |
| 3 critical path: clipping kept full attribution; untrustworthy children followed; unreplied requests missing from the handler index; READY beside idle or unknown CPUs called preempted; empty masks crashed | fixed | test_clip_recomputes_attribution, test_untrustworthy_child_is_not_followed, test_unreplied_request_keeps_the_server_ambiguous, test_ready_beside_an_idle_cpu_is_unexplained, test_empty_run_mask_does_not_crash |
| 4 transactions: stale reply selected and unindexed a replacement; ambiguous parents without warning | fixed | test_stale_reply_is_quarantined, test_ambiguous_parent_is_not_trustworthy |
| 5 wakeups attributed by process or state type only; destination CPU inferred | fixed | test_wakeup_needs_object_evidence |
| 6 pulses: uncertainty cleared by guessed exhaustion, loss tainted only existing queues, 1/n called confidence | fixed | test_pulse_matching_needs_a_synchronised_queue, test_pulse_queue_loses_sync_at_a_loss |
| 7 paths and names: PATHMGR plus unrelated ConnectAttach gave a path; `_IO_*` numbers named without protocol evidence | fixed | test_paths_need_a_confirmed_open_and_names_a_resource_manager, test_resource_manager_names_and_paths |
| 8 diff inputs: equal names merged, unfinished requests dropped, overlapping handling counted twice | fixed | test_latency_rows_are_roles_with_unfinished_and_excluded, test_server_time_is_credited_once |
| documentation overstatements ("every MsgSend", exact states, proven wakeups, never wrong links, fast-mode subtypes) | fixed in README and this document | |
| E slices | fixed (rated fixed) | |
| one-level inversion, bounded nesting, unresolved handoffs | documented limits | |

## 11. Demo storyline (165 s)

1. Problem (15 s): hi (priority 30) asks ts_psrv for 0.5 ms of work every 25 ms with a 5 ms budget for its MsgSend; hog (20) and lo (10) share CPU 0. The log says 20 of 20 missed.
2. Capture (20 s): `run_scenarios.sh /tmp/ts/bin /tmp/ts/out prio_fixed`, `scp`, `tracestudio convert`.
3. Diagnosis (55 s): in ui.perfetto.dev, CPU 0 shows hog holding the CPU while psrv-rx is preempted; hi's MsgSend arrow lands on psrv-rx only after lo's request; hi's critical path says 71% "psrv-rx READY (10), CPU: hog (20)". `tracestudio analyze --deadline hi=5000` lists 20 suspected inversions with the evidence (psrv-rx's traced run mask, hog on CPU 0) and the channel created with `_NTO_CHF_FIXED_PRIORITY`.
4. Fix and comparison (40 s): drop `-f`, recapture, `tracestudio diff`: 0 of 20 missed, max 2.7 ms, psrv-rx runs hi's requests at 30; the QNX priority track shows the boost arriving while psrv-rx still handles lo.
5. Trust (20 s): 672,043 events and 2.2 million argument values identical to traceprinter on 14 captures, 1,040/1,040 logged requests linked; in the fault-injection test, dropped buffers gave flagged exchanges and no wrong link; where the trace cannot show a cause, the tool says so.
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
