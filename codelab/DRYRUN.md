# How the codelab was checked

`find-priority-inversion.md` was run from the top four times on 3 November 2026, each time in a new workspace on an Ubuntu 22.04.5 machine with QNX SDP 8.0, QEMU 6.2.0 with KVM and Python 3.10.12. Every pass used a new `CODELAB` directory, a new venv with trace-studio installed from a fresh clone, and a new QEMU target built by the codelab's own `mkqnximage` command.

## Method

A script extracted every `bash` block of the codelab in order, plus the commands written inline in the text (`python3 --version`, the `ssh-keygen` hint, the three fast-mode commands in the aside, the final `ls`). It ran them in one bash session, so that `export`, `source` and `cd` carry over as in a reader's terminal, and recorded the exit status of each block. Where the text says to retry (no address yet; QEMU still exiting), the script retried the block. A second script checked that every line of the codelab's `text` blocks occurs in the pass's log with digits masked.

## Differences from the text

These were the same in every pass:

* `CODELAB` and the target directory were other directories inside the pass's workspace (the text has `~/trace-codelab` and `$CODELAB/target`). The `mkqnximage` command itself ran as written.
* trace-studio came from a local copy of the repository, not from GitHub, because the codelab, the deadline line of `ts_pload` and the v0.1.1 output of `analyze` and `diff` were not released yet. Passes 1 and 2 used commit 39b5c1d, passes 3 and 4 used commit 4465ede (v0.1.1).
* `sudo apt install ...` ran as `apt-get -s install ...` (simulated, because the machine is shared); every package was already installed. The two `sudo adduser` lines were replaced by a check that the user is in the `kvm` and `libvirt` groups (it is). `ssh-keygen` wrote its key into the pass's workspace, since the machine already had `~/.ssh/id_ed25519`.
* `ssh` and `scp` ran through wrappers that accept the new host key into a known-hosts file of the pass. This stands in for typing `yes` once and keeps the machine's own `known_hosts` untouched. `ssh-keygen -R $TARGET` used the same file. The cleanup check looked for the target's own directory.
* Perfetto: the converted `fixed.perfetto-trace` of pass 2 was opened in ui.perfetto.dev in headless Chromium (Playwright 1.61.0). It loaded, the menu has **Open trace file**, and the `ts_pload` and `ts_psrv` process groups are listed just below the CPU tracks. Selecting a slice, following its arrow and the zoom keys were not tried. The four screenshots come from `docs/img`, taken on 3 November 2026 from the `tests/data/run1` captures of the same scenario, and the text says their numbers differ.

## Pass 1: harness error

All blocks returned 0 except the two expected failures of the cleanup checks. The substitution for the target directory also removed `mkdir -p $CODELAB`, so the following `cd $CODELAB` failed and steps 4 to 12 ran in the target's directory. The pass is not counted, but it showed three things about the text:

1. The draft's `analyze` and `diff` outputs came from a development run with v0.1.0. With v0.1.1, `--client` also filters the inversion check (`checked 21 exchanges`, no unrelated `insufficient evidence` entries), and `diff` prints a `client filter` and a `message exchanges` line. All outputs and their explanations were rewritten from pass 2, and the bullet about a procnto handoff no longer applied and was removed.
2. `export TARGET=...; ssh root@$TARGET uname -a` was slow right after `mkqnximage` returned (the target was still booting). The text now says to expect a short wait.
3. The cleanup check ran straight after `mkqnximage --stop` and listed the QEMU process, which was still exiting (it was gone a little later). The text now says to run the check again a few seconds later if a line appears straight away.

The script was fixed (`mkdir -p $CODELAB` kept).

## Pass 2: first full pass

| Step | Result |
|---|---|
| 2 Before you begin | `default` network active, `check-net` silent, Python 3.10.12 |
| 3 Start a QNX target | mkqnximage ran; the address and first ssh succeeded after a wait: `QNX noname 8.0.0 ... x86_64` |
| 4 trace-studio and build | clone, venv, `pip install`, `--help`; both `qcc` builds without warnings; `file`: QNX x86_64 PIE with `ldqnx-64.so.2` |
| 5 Run | `hi: 20 of 20 requests missed the 5000 us deadline` |
| 6 Capture | fixed capture: 20 of 20 missed; `info`: wide mode, no losses, 691 exchanges; fast-mode aside: 0 suspected inversions, 20 `priority unknown (fast-mode trace?)` |
| 7 Convert | `fixed.perfetto-trace` written |
| 8 analyze | 20 of 20 missed, 20 suspected inversions, `ts_psrv chid 1` fixed priority |
| 9 Critical path | txn 572: server preempted, busy and running segments as in the text |
| 10 Fix | 0 of 20 missed, inversions 20 to 0, server priority 10 to 30 while handling hi |
| 12 Clean up | target stopped, host key removed, workspace removed; QEMU check empty on the second try |

The `Duration` fields of the codelab are an estimate of reading, typing and Perfetto time, not a measurement. Target timings omitted. Run the example locally for full output.

Text fixed after pass 2:

1. All `text` blocks now hold pass 2's outputs (all 120 checked pieces of the fixed text occur in the pass 2 log).
2. Rendering: 20 lines in code blocks were wider than the block (see "Rendering" below). Outputs were wrapped and wide tables narrowed (the `excluded/unfinished` column is cut and the text says so); the `apt` and cleanup commands use line continuations.
3. Perfetto: the process groups are just below the CPU tracks, so "scroll down" became "below the CPU tracks", and the text says where the workload is in the trace (about two seconds in).
4. Claims tightened: "every request took more than twice its deadline" became "all 20 missed" (the minimum was not checked). The offset between ts_pload's own figure and the trace is no longer given a cause.

## Pass 3

All blocks as in pass 2, every result of the same kind (20 of 20 missed before; 0 of 20 after; inversions 20 to 0). The line check found 117 of 120 pieces. The three missing ones are the short entries of the critical path: in this run no clock or procnto thread preempted psrv-rx, but devb-eide's `eide_driver_thread` (priority 21) did, while it handled hi's request. Text fixed: the critical path step now says that these short entries differ from run to run, with this example, and that the long READY segment behind the hog was there in every run (it was, in the development run and passes 1 to 4).

## Pass 4: no changes needed

Same commands as pass 3 (compared with digits masked). Results: plain run 20 of 20 missed, fixed capture 20 of 20, 20 suspected inversions, fast-mode capture 0 suspected inversions, inherit capture 0 of 20, `diff` 20 to 0 and server priority 10 to 30. The line check found 116 of 120 pieces; the four missing ones are again the run-dependent short entries of the critical path, which the text now describes as such. No text change.

## Rendering

On 3 November the codelab was exported with `claat export` (QNX's fork of the tool) and served next to the QNX codelab site's own `js/` assets (from `qnx.github.io/codelabs/js/`). Each step was opened in headless Chromium at 1280 px: 13 steps, 4 images loaded, the table and both asides render, no console errors. At 412 px code blocks are wider than the screen and scroll. The site's highlighter colours `text` blocks too, so an apostrophe (`lo's request`) starts a string colour that runs to the next apostrophe; this is cosmetic and left as it is.

That check did not catch every wide line. On the hosted page at 1440 px a code block holds about 86 characters, and lines of 87 and 88 characters were clipped in several blocks, among them the `max` column of the latency table and the ends of the evidence lines. Every line in a code block of the codelab is now at most 80 characters (checked by line length; the page was not exported again for this change). The hosted page shows the change only after the site is built again with `claat export`.

## After the passes

The four passes ran against trace-studio v0.1.1. On 15 November 2026, with trace-studio v0.3.1 installed from the repository, the host-side commands of the codelab were run on the two traces in `tests/data/run1` (`prio_fixed.kev.gz` and `prio_inherit.kev.gz`, unpacked to `fixed.kev` and `inherit.kev`): `info`, `convert`, `analyze` with `--deadline` and `--client`, `analyze --slowest 1`, `diff`, and `check`. All exited with status 0, except `check` on the fixed trace, which exits with status 1 as intended. The numbers in those traces differ from the ones printed in the codelab, which come from the 3 November passes. The QNX steps were not run again.

## Clean-up

After pass 4 the target was stopped with `mkqnximage --stop`, its directory was removed, and no QEMU process was left running from it.
