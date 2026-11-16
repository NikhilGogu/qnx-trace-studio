---
name: trace-studio
description: "How to find out why a request was late on QNX OS 8.0 from a kernel trace (.kev from tracelogger) with the tracestudio command: capture the trace, get latency per client and server, deadline misses, suspected priority inversions with their evidence, the critical path of one request, mutex owner chains, a before and after comparison, and a CI gate with exit status and JUnit XML. Read this before you capture or analyse a .kev file, before you claim a priority inversion, and when a user asks why a QNX client missed its deadline. Written for trace-studio v0.3.1."
license: Apache-2.0
---

# trace-studio: which command for which question

`tracestudio` runs on the development host, not on the target. It needs Python 3.10 or newer and no QNX libraries. Every command reads `.kev` and `.kev.gz` files. The examples use `trace.kev` for the user's capture.

## Capture on the target

Run as root on the QNX target, while the workload runs:

```
tracelogger -w -k 32 -b 128 -s 4 -f /tmp/trace.kev
```

`-w` (wide mode) is required for priorities and for the inversion analysis. Without it the inversion analysis reports "insufficient evidence" for every request. Copy the file to the host with `scp`. If the qnx-mcp tools are available, use `file_get` to copy it.

The inversion analysis needs the server's run mask. The trace records only masks set while tracing (`ThreadCtl(_NTO_TCTL_RUNMASK)`). If the server pins itself before the capture starts, start the capture first or ask the user for the mask.

## Questions and commands

| The user asks | Run |
|---|---|
| Is the trace usable: CPUs, lost events, fast or wide mode | `tracestudio info trace.kev` |
| Which client waits longest on which server | `tracestudio analyze trace.kev` |
| Did thread `hi` meet its 5 ms budget for every MsgSend | `tracestudio analyze trace.kev --deadline hi=5000` |
| The same, only for its requests to server process `ts_psrv` | `tracestudio analyze trace.kev --deadline hi@ts_psrv=5000` |
| Why was this one request slow | `tracestudio analyze trace.kev --request 524` (the `txn` number from the output or from Perfetto) |
| Why were the slowest requests slow | `tracestudio analyze trace.kev --slowest 3` |
| Only the clients of one process | `tracestudio analyze trace.kev --client ts_pload/` |
| Machine-readable output | `tracestudio analyze trace.kev --json` (keys in `docs/JSON.md`) |
| Did a fix help: before and after | `tracestudio diff before.kev after.kev --deadline hi=5000` |
| Show it on a timeline | `tracestudio convert trace.kev`, then open `trace.perfetto-trace` in ui.perfetto.dev |
| Open it in Perfetto with the important tracks pinned | `tracestudio open trace.kev` (on a remote machine add `--no-browser`) |
| Fail a CI job on a missed deadline or an inversion | `tracestudio check trace.kev --deadline hi@ts_psrv=5000 --no-inversions --client ts_pload/ --junit report.xml` |

## Reading the verdicts

Every analysis uses only trustworthy exchanges. The first lines of `analyze` say how many exchanges were found and how many are trustworthy. A large gap between the two means lost events or contradictory data. Say so before you draw a conclusion.

A request in the inversion analysis gets one of three results:

* **suspected priority inversion**: the trace shows, for every instant counted, that the client waited on the server, the server was READY at a lower priority, and every CPU the server may use ran a thread above the server and at least one below the client. The `evidence` text names the threads and CPUs. Quote it.
* **insufficient evidence**: the trace cannot show why the server did not run. The reasons are listed with the time each covers. Report it as "not known from this trace", never as an inversion and never as "no inversion".
* no row: the request had no inversion time and no unexplained time above `--min-us`.

Common reasons for insufficient evidence and what to do:

* `run mask of ts_psrv/psrv-rx not traced`: the mask was set before the capture. If the user knows it from the program's code or configuration, run again with `--runmask ts_psrv/psrv-rx=0x1` or `--assume-unpinned ts_psrv`. Never guess a mask. Verdicts that use one say "Relies on run mask ... supplied by --runmask".
* `request handed off: received by ..., replied by ...`: another thread replied. The trace does not say which thread did the work. This is common for requests to procnto.
* other receive-capable threads, lost events, sporadic scheduling, fast mode: capture again in wide mode without losses, or accept that the trace cannot answer.

The critical path splits one request's latency into segments that add up to it exactly: client running, server busy with another request, server preempted (READY while higher-priority threads held its CPUs), nested request (the server waiting on another server), mutex owner chains, and "unknown" where the trace cannot tell. Lead the answer with the largest segment.

Mutex waits are visible only when a thread had to wait. The owner is named only when the trace shows the owner's successful unlock; otherwise it is "owner unknown" with the reason.

## The CI gate

`tracestudio check` exits 0 when every assertion passes, 1 when one fails or the trace cannot be read, and 2 on a usage error. Each assertion prints PASS or FAIL with its numbers and, on failure, the evidence. `--no-inversions` also fails when more than `--max-insufficient` requests (default 0) have insufficient evidence, so use `--client` to limit it to the clients the gate is about. `--junit FILE` writes one testcase per assertion.

## Rules

* Do not call something a priority inversion unless `analyze` or `check` reports it as suspected. Use the word "suspected": the verdict rests on the trace, not on the program's intent.
* Do not report a latency, a count or a priority that the output does not show.
* Without a capture there is nothing to analyse. The repository's `tests/data` holds sample captures for trying the commands.
