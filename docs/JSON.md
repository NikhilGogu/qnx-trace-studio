# JSON output

`tracestudio analyze --json` and `tracestudio diff --json` print one JSON object. Scripts, CI jobs and coding agents can read it instead of the text output. This page lists every key. The test `test_analyze_json_contract` checks that the output has exactly these keys, so a change to the output has to change this page too.

Times are in microseconds (`_us`). Thread labels are `process/thread`. A verdict never rests on an exchange that is not trustworthy; those are only counted.

## analyze

### Top level

| key | type | meaning |
|---|---|---|
| `trace` | string | the trace path as given |
| `client_filter` | string or null | the `--client` text; every per-client part below is filtered by it |
| `transactions` | int | message exchanges found |
| `trustworthy` | int | exchanges complete and without warnings; the analyses use only these |
| `ambiguous_parent` | int | trustworthy exchanges whose parent request is ambiguous; kept in latency, left out of critical paths and inversions |
| `losses` | list | lost events: `cpu`, `kind`, `count`, `detail` per loss record; empty when nothing was lost |
| `ipc_latency` | list | MsgSend latency per client role and server (below) |
| `inversions` | object | suspected priority inversions and insufficient evidence (below) |
| `fixed_priority_channels` | list | channels created with `_NTO_CHF_FIXED_PRIORITY`: `process`, `pid`, `chid`, `flags` |
| `deadlines` | object | one entry per `--deadline`: `deadline_us` and `by_server` (below) |
| `server_priority_while_handling` | list | `server`, `client`, `prio_us` and `prio_share` per QNX priority the server ran at while it handled that client's requests |
| `mutex_waits` | list | contended mutex waits of at least `--min-us` (below) |
| `critical_paths` | list | one entry per `--request` or `--slowest` request (below); empty otherwise |

### ipc_latency[]

| key | type | meaning |
|---|---|---|
| `client` | string | client role: `process/thread@priority` |
| `server` | string | server process |
| `n` | int | trustworthy requests |
| `excluded` | int | requests left out as untrustworthy, including a request that returned but whose MsgSendv entry is not in the trace |
| `unfinished` | int | requests whose MsgSendv had not returned when the trace ended |
| `client_threads` | int | client threads merged into the role |
| `min_us` | float | latency statistics over the trustworthy requests |
| `p50_us` | float | |
| `p90_us` | float | |
| `p99_us` | float | |
| `max_us` | float | |

### inversions

| key | type | meaning |
|---|---|---|
| `suspected` | list | requests with evidence of a priority inversion (below) |
| `insufficient_evidence` | list | requests where the trace cannot show whether the server was held up (below) |
| `checked` | int | trustworthy exchanges the analysis looked at |
| `excluded_untrustworthy` | int | exchanges left out because they are not trustworthy |
| `excluded_ambiguous_parent` | int | exchanges left out because their parent is ambiguous |
| `suspected_using_assumptions` | int | suspected rows that rely on a run mask given with `--runmask` or `--assume-unpinned` |
| `min_us` | float | the threshold: a row needs at least this much evidence or unexplained time |

### inversions.suspected[]

| key | type | meaning |
|---|---|---|
| `txn` | int | the exchange, as the `txn` argument in Perfetto |
| `client` | string | client thread |
| `client_prio` | int | client priority at the send |
| `server` | string | server thread |
| `latency_us` | float | the client's MsgSend latency |
| `verdict` | string | always `suspected priority inversion` |
| `inversion_us` | float | time with evidence of an inversion |
| `while` | object | that time split by the client's state (`SEND`, `REPLY`) |
| `server_prio_while_ready` | int or null | lowest server priority seen while it was READY with evidence; null when all the evidence is on threads the server waited for |
| `server_run_mask` | string or null | run masks of the server during that evidence, hex; null as above |
| `eligible_cpus` | list of int or null | CPUs the server could run on; null as above |
| `held_up` | list | every thread found READY and held up, longest first: `thread`, `prio` (lowest seen), `run_mask`, `eligible_cpus`, `us`, and `chain`, the blocking chain from the server to that thread (null for the server itself) |
| `preempted_by` | list | `thread`, `prio`, `cpu`, `us` for each thread that held the CPUs of a thread in `held_up`, longest first |
| `server_busy_with` | list | `txn`, `client` of other requests the server handled during the wait |
| `insufficient_evidence_us` | float | time in the same wait that the trace cannot explain |
| `assumptions` | list of string | supplied run masks the verdict relies on |
| `receivers` | list | for a multi-threaded server: `thread`, `tid`, `states` of every receive-capable thread |
| `evidence` | string | the verdict and its evidence in one paragraph, as in the text output |

### inversions.insufficient_evidence[]

| key | type | meaning |
|---|---|---|
| `txn` | int | the exchange |
| `client` | string | client thread |
| `client_prio` | int or null | client priority at the send (null in fast mode) |
| `server` | string | server thread |
| `latency_us` | float | the client's MsgSend latency |
| `verdict` | string | always `insufficient evidence` |
| `unexplained_us` | float | time the trace cannot explain |
| `reasons` | object | that time split by reason |

### deadlines.NAME.by_server.SERVER

| key | type | meaning |
|---|---|---|
| `requests` | int | trustworthy requests of the named client threads to that server |
| `missed` | int | requests over the deadline |
| `max_us` | float | slowest of them |
| `excluded` | int | requests left out as untrustworthy, including a request that returned but whose MsgSendv entry is not in the trace |
| `unfinished` | int | requests whose MsgSendv had not returned when the trace ended |

### mutex_waits[]

| key | type | meaning |
|---|---|---|
| `wait` | int | index of the wait |
| `waiter` | string | thread that waited |
| `waiter_prio` | int | its priority when it blocked |
| `mutex` | string | mutex address, hex |
| `blocked_us` | float | MUTEX-blocked time |
| `owner` | string or null | owner thread, when the trace shows it |
| `owner_value` | string or null | owner word the kernel read, hex |
| `owner_known_us` | float | part of the wait with a known owner |
| `owner_not_traced_us` | float | part after the owner's unlock, when the next owner is not traced |
| `until_return_us` | float | time from the wake to the return of SyncMutexLock |
| `ceiling` | bool | a priority-ceiling mutex |
| `result` | string | `acquired`, `acquired, previous owner died (EOWNERDEAD)`, `still waiting` or `failed (errno N)` |
| `owner_unknown` | string | only when the owner is unknown: the reason |
| `owner_prio_before` | int | only with a known owner: its priority before the wait |
| `owner_prio_max` | int or null | only with a known owner: its highest priority during the wait |
| `owner_states_us` | object | only with a known owner: its time per state during the wait |
| `chain` | string | only with a known owner: the blocking chain from the waiter |

### critical_paths[]

| key | type | meaning |
|---|---|---|
| `txn` | int | the request |
| `client` | string | client thread |
| `server` | string | server process |
| `message` | string or null | message name, when known |
| `latency_us` | float | MsgSend latency; the segments add up to it |
| `trustworthy` | bool | |
| `warnings` | list of string | |
| `by_kind_us` | object | time per segment kind |
| `segments` | list | consecutive segments (below) |

A request whose MsgSend entry or exit is not in the trace has only `txn` and `error`.

### critical_paths[].segments[]

| key | type | meaning |
|---|---|---|
| `start_us` | float | offset from the MsgSend entry |
| `us` | float | length |
| `kind` | string | `client running`, `server preempted`, `nested request`, ... (README, Critical path) |
| `what` | string | the segment in words |
| `txn` | int or null | the request this segment belongs to |
| `thread` | string | the thread the segment is about, when there is one |
| `prio` | int or null | its priority, when there is a thread |
| `cpu_held_by_us` | object | for READY time: who held the CPUs, and for how long |
| `nested` | list | segments of a nested request or mutex owner, same keys |

## diff

`diff --json` prints `before` and `after`, each with the top-level keys of `analyze` except `critical_paths`, and `comparison` with `latency` and `server_priority` (the rows the text output ranks).

## check

`check` has no JSON output. Its result is the exit status (0 all passed, 1 a failure or an unreadable trace, 2 a usage error) and, with `--junit FILE`, a JUnit XML report: `testsuites` > `testsuite` > one `testcase` per assertion, with a `failure` element (type `DeadlineMissed`, `Untrustworthy`, `PriorityInversion`, `InsufficientEvidence`, `LostEvents` or `NoData`) holding the evidence. A trace that cannot be read gives one `testcase` with an `error` of type `TraceError`.
