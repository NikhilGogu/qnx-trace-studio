# Validation evidence

The measurements were made on the build box (Ubuntu 22.04, QNX SDP 8.0) against 20 captures from the QEMU targets described in the README: 17 x86_64 (runs 1 to 4) and 3 aarch64le (run 5). The first 14 x86_64 captures (runs 1 to 3) were the set of v0.1.1; run 4 added the mutex captures.

Eleven of the 20 are in the repository. Run 6 adds the two thread-pool captures (`workload/ts_pool.c`), taken later on an x86_64 QEMU target built by `mkqnximage` (QNX 8.0, 2 CPUs). The test suite runs on these 13:

| directory | captures | target |
|---|---|---|
| `tests/data/run1` | `ipc_fast`, `ipc_wide`, `prio_fixed`, `prio_inherit` | x86_64 |
| `tests/data/run3` | `cancel`, `prio_fixed_rx`, `prio_inherit_rx`, `rm_small` | x86_64 |
| `tests/data/run4` | `mutex_chain_hog`, `mutex_chain_nopi` | x86_64 |
| `tests/data/run5` | `prio_fixed` | aarch64le |
| `tests/data/run6` | `pool_fixed`, `pool_inherit` | x86_64 |

The other nine (seven x86_64, among them the run 2 captures, `rm_traffic` and `mutex_chain`, and two aarch64le) stayed on the build box. The totals below that include them cannot be reproduced from the repository alone.

To check the 13 checked-in captures yourself on a host with the QNX SDP:

```
source ~/qnx800/qnxsdp-env.sh            # puts traceprinter on PATH
python3 tools/validate_all.py tests/data --out validation-summary.txt
```

It runs `traceprinter` and `tools/compare_traceprinter.py` on every capture, and `tracestudio validate` on every capture that has workload logs, and prints one PASS or FAIL line per check with its counts. Without the SDP, `--skip-traceprinter` runs only the validate checks.

- Parser against `traceprinter` on the 13 checked-in captures (`tools/validate_all.py tests/data`, rerun on 18 Nov 2026 with SDP 8.0): 369,402 events identical, 1,097,066 argument values compared, 0 mismatches; every logged request of the 11 captures with workload logs matched. This is the count anyone can reproduce from the repository.
- Parser against `traceprinter` (`tools/compare_traceprinter.py`, exact values), 17 build-box captures (log on the build box, not reproducible from the repository): 739,841 events identical in timestamp, CPU, class and event; 2,428,184 argument values compared, 0 mismatches, sign included. They are the arguments of 70 event kinds (235 event and field pairs): thread states (pid, tid, priority, policy), process creation, destruction and names, PATHMGR and TIMER system events, interrupts, CONTROL/TIME, the COMM events, the entries and exits of MsgSendv, MsgSendvnc, MsgReceivev, MsgReceivePulsev, MsgReplyv, MsgError, MsgSendPulse, ChannelCreate, ConnectAttach, ConnectDetach, ThreadCtl, ThreadCtlExt, SchedSet, SyncMutexLock, SyncMutexUnlock and SyncTypeCreate, and the return code and errno of every failed kernel call. Not compared: 388,758 values of 304 kinds that traceprinter prints and trace-studio does not decode (the arguments of the other kernel calls, such as the other SYNC_*, TIMER_* and CONNECT_*_INFO calls and MsgReadv/MsgWritev, `_msg_info` fields it skips, and MMAP events; 69,108 of these events have no decoded argument at all), and 9,085 decoded values traceprinter does not print in a comparable form (CONTROL/BUFFER counts, MsgSendvnc exit status, COMM SIGNAL, a few message words). The largest capture (`rm_traffic`, 12.4 MB) has 306,436 events. The layouts of failed exits, INT_CALL, the signed 64-bit counts, MsgSendPulsePtr and the mutex calls were also checked against traceprinter on a synthetic trace (a unit test does this when traceprinter is installed).
- aarch64le (QEMU virt under TCG, 2 CPUs, ClockCycles at 62.5 MHz; 3 captures): 158,352 events identical to traceprinter and 487,334 argument values compared, 0 mismatches; 280 of 280 logged requests matched (200 in `ipc_wide` with 4 of 4 client pulses and 425 of 425 timer pulses, 40 in each `prio_*`); `prio_fixed` has 20 suspected inversions (SEND-blocked, median 9.9 ms of evidence, run mask 0x1, hog at 20 on CPU 0) and 20 of 20 deadline misses at 5 ms (max 14816.9 us), `prio_inherit` none and 0 of 20 (max 4116.2 us); `tracestudio check --deadline hi@ts_psrv=5000 --no-inversions --client ts_pload/` exits 1 and 0; TraceProcessor imports all three without errors.
- Message linking against the workload's own logs (`tracestudio validate`): 1,100 of 1,100 logged requests matched to the right client thread, server thread, rcvid, coid and MsgSend outcome (480 in run 1, 480 in run 2, 80 in run 3, 60 in the mutex captures), every SEND and DONE record paired, including all 160 forwarded requests of runs 1 and 2 linked to their parent, with rcvids reused up to 186 times; the pulse counts of the 4 `ipc_*` captures agree with the logs. Mutexes: all 60 of mrx's waits in the three mutex captures match the logs (mutex address, owner thread, owner value as the holder's TLS `__owner`, release between the holder's UNLOCK and mrx's ACQUIRED logs, and the holder's decoded request to ts_mslow).
- Inversions: 20 of 20 hi requests flagged with a fixed-priority channel and 0 with inheritance, both when the hog strikes before the receive (`prio_fixed`, median 8.2 ms of evidence each, SEND-blocked) and after it (`prio_fixed_rx`, median 10.0 ms, REPLY-blocked); 0 in the wide-mode IPC capture. Requests that procnto received on one thread and replied to on another are reported as insufficient evidence: 2 in `prio_fixed`, 2 in `prio_inherit`, 1 in `prio_inherit_rx`, 2 in `ipc_wide`. Fast mode records no priorities, so `ipc_fast` has no inversions and 15 requests with insufficient evidence (12 for the missing priorities, 3 handoffs). Deadline misses at 5 ms: 20/20 versus 0/20 in both pairs.
- Cancellation (`cancel`, checked by the regression test `test_cancellation_matches_the_workload_log`; validate does not cover this workload): all 15 logged sends decoded with the logged outcome (5 replied, 10 cancelled with return code -1 and the logged errno, ETIMEDOUT or EINTR), and the 10 late MsgReply calls that failed in the log are exactly the 10 replies the decoder could not attach to any exchange.
- Resource managers: summing the bytes of `cat`'s `_IO_READ` replies per candidate path gives 26 paths in `rm_traffic`: 25 equal the file size (`wc -c` on the target) and `librcheck.so.1` has twice its size, because `cat` also read it through the symlink `librcheck.so`; both files in `rm_small` match. In `rm_traffic` 7,293 `_IO_*` names are candidates (servers other than procnto), and 90 messages with an `_IO_*` number on connections with no `_IO_CONNECT` evidence are left unnamed.
- Fault injection (`tools/drop_buffer.py`): removing 4 of the 15 kernel buffers inside the workload window of `ipc_wide` leaves 108 of 200 requests matched, 62 without a candidate, no wrong link in that test (server, rcvid, coid, request id, parent); 2 outcomes contradict the log, both on exchanges flagged untrustworthy, and the pulse counts differ, so validation fails.
- Perfetto import: on the build box, TraceProcessor 0.59.1 loaded the 14 x86_64 captures of runs 1 to 3 (v0.1.1) and the 3 aarch64le captures with no error or data-loss stats; every flow joins slices of one transaction; no slice has a negative duration. The test suite repeats these checks on all 13 checked-in captures (`test_every_checked_in_capture_converts_and_loads`).
- Tests: `python3 tests/test_tracestudio.py` (or pytest) runs 112 tests: 83 on synthetic slot streams (one checks that gzip-compressed traces read like plain ones), including a counterexample for each defect found in code review (among them: deleted middle slots, a partial final slot, losses at trace start, unanchored timestamps, READY inside a loss window, untraced and supplied run masks, inheriting channels, stale channel identities after a loss, other possible receivers, reused thread ids, sporadic scheduling, clipped attribution, untrustworthy children, receive attribution, cross-thread replies, stale replies, ambiguous parents, wakeups across a re-block, pulse queues out of sync, candidate paths and names, untrustworthy exchanges without arrows, failed kernel-call exits, INT_CALL, signed 64-bit and `_msg_info` fields, MsgSendPulsePtr, unpaired and shared log records, wrong coid, outcome and pulse counts, diff rows without samples and changed priority shares, non-finite or negative numbers, a zero inversion threshold, a request whose start is not traced counted as excluded and not unfinished, inversions through a mutex owner (owner known, unknown, at the link limit, at the client's priority), unreadable input and short CSV records, malformed header fields, system page lengths and gzip data, an event placed before cycle 0, an event of another CPU inside a buffer, `open`'s page, protocol, Host, token and Origin checks and private cache, `check` on lost events, empty assertions, inversion coverage, server-scoped deadlines and JUnit edge cases), and 29 on the captures in `tests/data` (including the README quickstart commands, the JSON keys documented in `docs/JSON.md`, the commands in the agent skill, the GitHub Action's split of `args`, the example SQL in `docs/sql`, the examples page with times withheld, the links and test counts in the docs, `tools/validate_all.py` without traceprinter, the two thread-pool captures in `tests/data/run6`, the code and tests named in the README's capability table, the conversion and TraceProcessor load of every one of them, `--min-us 0` from the command line, the two mutex captures with a tampered-log counterexample, an aarch64le capture, CLI exit codes, `check` on `prio_fixed` and `prio_inherit` with their JUnit reports, `check` failing on `mutex_chain_nopi` and passing on `mutex_chain_hog` through the mutex chain, `open`'s cache and default pins, and loss injection on two captures). With `TS_REQUIRE_ALL=1` a missing `traceprinter` or perfetto package fails the run; the 82 tests of v0.3.1 pass that way on the build box; the four thread-pool tests added since run in CI. On 18 Nov 2026 all 112 passed with `TS_REQUIRE_ALL=1` on an x86_64 Linux host with the SDP.
- Packaging: a wheel and an sdist built with `python -m build` install in fresh venvs and run on Python 3.10 (build box) and 3.12 (development host). The sdist carries the test files but not `tests/kevgen.py` or the fixtures; run the tests from the repository.
- Speed on the build box: `ipc_wide` (1.7 MB, 44,807 events) converts in 0.87 s with 103 MB peak memory; `rm_traffic` in 7.5 s with 734 MB; `analyze` takes 0.37 s and 2.8 s.

## Seeded scheduling regressions (QNX target)

`tools/seeded_regressions.py` plants one scheduling bug in a healthy workload and asks two gates about it. Ten bugs: priority inheritance off on a channel (`prio_fixed`, `prio_fixed_rx`, `pool_fixed`, `pool_long_fixed`, and the nested server's channel in `mutex_chain_slowfixed`), a `PTHREAD_PRIO_NONE` mutex (`mutex_chain_nopi`), a receiver's run mask narrowed to one CPU (`pool_narrow`), the hog raised above the client (`prio_hog_high`), the client lowered below the hog (`prio_hi_low`) and a thread pool cut to one receiver (`pool_long_one`). Each bug has a healthy parent among seven controls (`prio_inherit`, `prio_inherit_rx`, `prio_inherit_late`, `mutex_chain_hog`, `pool_inherit`, `pool_wide`, `pool_long`). All 17 are scenarios of `workload/run_scenarios.sh`. The expected cause of each bug is written in the script before any capture is scored.

The baseline is a deadline-only gate: tracelogger writes the trace, traceprinter decodes it, and the workload's own per-request log says whether hi missed its deadline. trace-studio runs `check --deadline ... --no-inversions --client ...` and names a cause for every late request: a suspected inversion with its preemptor and blocked server thread, or else the longest kind of the request's critical path and the thread that held the CPU.

Run on 18 Nov 2026: one x86_64 QEMU target built by `mkqnximage` (QNX 8.0.0, 2 CPUs), three rounds of all 17 scenarios, 51 captures.

| | baseline | trace-studio |
|---|---|---|
| bug captures where the gate fails (of 30) | 30 | 30 |
| bug captures with a cause named (of 30) | 0 | 30 |
| bug captures with the expected cause (of 30) | 0 | 30 (10 of 10 bugs, every round) |
| wrong cause / insufficient evidence | none named | 0 / 0 |
| control captures where the gate fails (of 21) | 0 | 1 |

The one failing control is `mutex_chain_hog` in round 1: 0 suspected inversions and no late request, but one request from hi to procnto had insufficient evidence (procnto received it on one thread and replied on another), and the gate allowed none. The other two rounds of that control pass.

## Loss injection (host only)

`tools/loss_injection.py` damages the 13 checked-in captures and compares what trace-studio says about each damaged copy with what it says about the original: links of every exchange it still calls trustworthy (server thread, rcvid, coid, replier, status, timestamps, parent and nested requests), suspected inversions, mutex owners, `validate` against the workload logs, and whether `check` fails its trace integrity assertion when loss is reported. A missing parent link is counted apart when the parent itself is no longer trusted.

Each of the 13 captures gets 10 seeds of each of ten damage kinds. Six kinds leave a mark the decoder can see: whole buffers dropped, slots dropped, a kernel "dropped events" record, the file cut inside a slot or inside a buffer, and two buffers of one CPU swapped. Four cannot be seen in principle, because the result is a valid trace of a shorter or reordered run: the last one to three buffers of the file dropped, the file cut at a buffer boundary, buffers of different CPUs interleaved in another order (each CPU's own order kept), and short runs of plain events shuffled inside a buffer. Copies where a kind does not apply are skipped.

Run on 18 Nov 2026 (`python3 tools/loss_injection.py tests/data --seeds 10 --json ...`):

| | detectable damage | undetectable damage |
|---|---|---|
| damaged copies (skipped) | 780 (0) | 423 (97) |
| copies where loss is reported | 780 | 0 |
| copies where `check` passes although loss is reported | 0 | 0 |
| exchanges still called trustworthy | 503,832 | 293,129 |
| wrong links among them (copies with one or more) | 84 (33) | 60 (38) |
| suspected inversions: wrong / missed | 0 of 2,056 / 0 | 0 of 2,602 / 0 |
| mutex owners named: wrong | 0 of 2,945 | 0 of 1,626 |
| `validate` WRONG lines / crashes | 0 / 0 | 0 / 0 |

So the verdicts held: no wrong or missed inversion, no wrong mutex owner, no WRONG from `validate`, and `check` failed on every copy with reported loss. The links did not all hold:

- Under detectable damage, 84 wrong links: 23 wrong parents where the parent is still trusted, 33 parents that point at a request that is itself untrusted, 16 wrong nested requests and 12 wrong fields (server, rcvid, replier or exit time). By kind (copies with a wrong link, of 130): drop_buffers 11, drop_slots 5, kernel_dropped 1, truncate_mid_slot 4, truncate_mid_buffer 7, swap_same_cpu 5.
- Under undetectable damage, 60 wrong links, 59 of them wrong fields: drop_tail 13 copies and truncate_at_buffer 25 copies; reorder_cpus and reorder_slots 0.
- The main cause is the end of the trace. When one CPU's stream stops before another's, a later exit on the other CPU can be attached to an exchange that is still open. Truncation is marked only on the CPU whose buffer was cut, so the other CPUs' missing tails are not marked as loss. A fix would open a loss window on every CPU from its last event when a file is cut; it is not in this branch.
- Separately, 69 missing parent links point at a parent that is no longer trusted; those are counted apart and not as wrong.

`tests/test_tracestudio.py` runs the six detectable kinds on two captures with one seed each and asserts the verdict counts above are 0. It counts links but does not assert them.

### Corrupted copies that still pass `check`

Forty corrupted copies of `run1/prio_inherit` (random bytes, random 4 KiB blocks, cuts, header bytes): 12 still pass `check`, none with reported loss. Ten give the same hi requests and no trusted exchange that the original lacks (four changed one header byte). Two do not: `bytes25` (198 random bytes changed) has 6 trusted exchanges with a wrong server, rcvid or coid, and `block37` (one 4 KiB block overwritten) loses one hi request. A `.kev` file has no checksum, so random byte damage inside a valid slot cannot be told from real data.
