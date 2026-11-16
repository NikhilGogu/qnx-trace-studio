# Changelog

## 0.4.0

- Inversions follow the server's blocking chain: a MUTEX wait to the owner, a REPLY wait to the nested server, up to 4 links. `check --no-inversions` now fails on `mutex_chain_nopi` and names the owner and the preempting thread. Links the trace cannot follow are "insufficient evidence" with the link named. Suspected rows gain `held_up`; the `server_*` fields are null when the server itself was never READY with evidence.
- Fixed: a malformed header field, a system page length outside the file, or corrupt gzip data gave a traceback, and `check` left an older JUnit report in place. These now fail with a message naming the field and a `TraceError` report. Gzip data is decompressed in chunks up to 4 GiB, or `TRACESTUDIO_MAX_KEV_BYTES`.
- Inversions: SEND-phase verdicts for multi-threaded servers. Every receive-capable thread of the server must be accounted for, or the result is "insufficient evidence".
- Fixed: `analyze --min-us 0` and `check --min-us 0` crashed on a request with no inversion time.
- `action.yml`: a GitHub Action that runs `tracestudio check` and writes a JUnit report.
- `skills/trace-studio/SKILL.md`: an agent skill for capturing and reading traces.
- `docs/JSON.md` lists every key of `analyze --json` and `diff --json`; a test keeps it in step with the output.
- `docs/sql`: example Perfetto SQL queries, run by the tests on converted captures.
- README: a quickstart that runs on the checked-in captures without QNX, a mutex owner chain example, a comparison with the System Profiler, and a table of what trace-studio adds with the code and tests behind each row.
- Tests run on Python 3.10 and 3.12 in CI, with the `check` gate and the action self-test.

## 0.3.1

aarch64le captures verified on QEMU virt.

## 0.3.0

Mutex owner chains with evidence, and critical paths across mutexes and message passing.

## 0.2.0

`open`: one command to Perfetto with the important tracks pinned. `check`: real-time assertions with exit status and JUnit XML.

## 0.1.1

Decoding of failed kernel-call exits, signed fields, stricter validation, diff and CLI fixes.

## 0.1.0

First release.
