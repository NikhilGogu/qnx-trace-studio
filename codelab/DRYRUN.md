# Dry runs of the codelab

Date: 3 November 2026. Each pass follows `find-priority-inversion.md` from the top in a new workspace on the build box (Ubuntu 22.04.5, QNX SDP 8.0 in `~/qnx800`, QEMU 6.2.0 with KVM, Python 3.10.12): a new `CODELAB` directory, a new venv with trace-studio installed from a fresh clone, and a new QEMU target built by the codelab's own `mkqnximage` command. Times are UTC.

Method. A script (`dryrun.py`, kept on the box in `~/tcl-dryrun/`) extracts every `bash` block of the codelab in order, plus the commands written inline in the text (`python3 --version`, the `ssh-keygen` hint, the three fast-mode commands in the aside, the final `ls`), and runs them in one bash session, so that `export`, `source` and `cd` carry over as in the reader's terminal. It records the exit status and time of each block. Where the text says to retry (no address yet; QEMU still exiting), the script retries the block. A second script (`cmpout.py`) checks that every line of the codelab's `text` blocks occurs in the pass's log with digits masked, so that each output shown is the real shape of the output. Logs: `~/tcl-dryrun/pass<N>/log.txt` on the box.

Deviations from the text, the same in every pass:

* `CODELAB` was `~/tcl-dryrun/pass<N>/trace-codelab` instead of `~/trace-codelab`, and the target directory was `~/targets/tcl1` instead of `$CODELAB/target` (the build box keeps targets there under names reserved per agent). The mkqnximage command itself ran as written.
* `git clone https://github.com/NikhilGogu/qnx-trace-studio.git` was redirected (`url.<bundle>.insteadOf` in a private `GIT_CONFIG_GLOBAL`) to a git bundle of the local repository, because the codelab, the deadline line of `ts_pload` and the v0.1.1 output of `analyze` and `diff` are not pushed yet. The public repository answered (`git ls-remote`: `main` at c9161f6, v0.1.0). Passes 1 and 2 used the bundle at 39b5c1d, passes 3 and 4 at 4465ede (v0.1.1 from the trace-studio agent).
* `sudo apt install ...` ran as `apt-get -s install ...` (simulated: the box is shared); every package was already installed. The two `sudo adduser` lines were replaced by a check that the user is in `kvm` and `libvirt` (it is). `ssh-keygen -q -t ed25519 -N '' -f ...` wrote to the pass directory, since the box already has `~/.ssh/id_ed25519`.
* `ssh` and `scp` were wrappers adding `-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<pass>/known_hosts`, standing in for the reader typing `yes` once and keeping the box's `~/.ssh/known_hosts` untouched; `ssh-keygen -R $TARGET` got `-f <pass>/known_hosts` for the same reason. The cleanup check greps for `targets/tcl1` instead of `trace-codelab`, the directory the target really ran in.
* Perfetto: the text's clicks were not made by a person. The converted `fixed.perfetto-trace` of pass 2 was opened in ui.perfetto.dev in headless Chromium (Playwright 1.61.0): it loaded, the menu has **Open trace file**, and the `ts_pload` and `ts_psrv` process groups are listed just below the CPU tracks. Selecting a slice, following its arrow and the zoom keys were not exercised; the four screenshots are reused from `docs/img` (taken on 3 Nov 2026 from the `tests/data/run1` captures of the same scenario) and the text says their numbers differ.

## Pass 1: 20:10:08 to 20:10:46, harness error

All blocks returned 0 except the two expected failures of the cleanup checks. The substitution for the target directory also removed `mkdir -p $CODELAB`, so the following `cd $CODELAB` failed and steps 4 to 12 ran in the target's directory. The pass is not counted, but it showed three things about the text:

1. The draft's `analyze` and `diff` outputs came from a development run with v0.1.0. With v0.1.1, `--client` also filters the inversion check (`checked 21 exchanges`, no unrelated `insufficient evidence` entries), and `diff` prints a `client filter` and a `message exchanges` line. All outputs and their explanations were rewritten from pass 2; the bullet about a procnto handoff no longer applied and was removed.
2. `export TARGET=...; ssh root@$TARGET uname -a` took 13 s right after mkqnximage returned (the target was still booting): the text now says it can take about 15 seconds.
3. The cleanup check ran 11 ms after `mkqnximage --stop` and listed the QEMU process, which was still exiting (it was gone a little later). The text now says to run the check again a few seconds later if a line appears straight away.

Harness fixed (`mkdir -p $CODELAB` kept).

## Pass 2: 20:12:09 to 20:12:50, first full pass

| Step | Time | Result |
|---|---|---|
| 2 Before you begin | 0.5 s | `default` network active, `check-net` silent, Python 3.10.12 |
| 3 Start a QNX target | 14.9 s | mkqnximage 1.6 s; address and first ssh 13.2 s: `QNX noname 8.0.0 ... x86_64` |
| 4 trace-studio and build | 4.7 s | clone, venv, `pip install`, `--help`; both `qcc` builds without warnings; `file`: QNX x86_64 PIE with `ldqnx-64.so.2` |
| 5 Run | 2.7 s | `hi: 20 of 20 requests missed the 5000 us deadline, slowest 11276 us` |
| 6 Capture | 9.0 s | fixed capture: 20 of 20 missed; `info`: wide mode, no losses, 691 exchanges; fast-mode aside: 0 suspected inversions, 20 `priority unknown (fast-mode trace?)` |
| 7 Convert | 0.5 s | `fixed.perfetto-trace` written |
| 8 analyze | 0.2 s | 20 of 20 missed (max 10967.0 us), 20 suspected inversions, `ts_psrv chid 1` fixed priority |
| 9 Critical path | 0.2 s | txn 572: server preempted 8215.0 us, busy 2254.0 us, running 496.4 us |
| 10 Fix | 5.3 s | 0 of 20 missed (max 2790.8 us), inversions 20 to 0, server priority 10 to 30 while handling hi |
| 12 Clean up | 3.1 s | target stopped, host key removed, workspace removed; QEMU check empty on the second try |

The commands of the whole pass took 41.1 s. The `Duration` fields (42 minutes in total) are our estimate of reading, typing and Perfetto time, not a measurement with a reader.

Text fixed after pass 2:

1. All `text` blocks now hold pass 2's outputs (checked with `cmpout.py`: all 120 checked pieces of the fixed text occur in the pass 2 log).
2. Rendering (claat export, see below): 20 lines in code blocks were wider than the block at 1280 px. Outputs are wrapped at 88 columns and wide tables narrowed (the `excluded/unfinished` column is cut and the text says so); the `apt` and cleanup commands use line continuations.
3. Perfetto: the process groups are just below the CPU tracks, so "scroll down" became "below the CPU tracks", and the text says where the workload is in the trace (about two seconds in).
4. Claims tightened: "every request took more than twice its deadline" became "all 20 missed" (the minimum was not checked); the 15 us difference between ts_pload's figure and the trace is no longer given a cause (the README says the cause is unknown); durations trimmed from 45 to 42 minutes.

## Pass 3: 20:21:28 to 20:22:08

All blocks as in pass 2, commands 39.4 s, every result of the same kind (20 of 20 missed before, max 11229.1 us; 0 of 20 after, max 2796.7 us; inversions 20 to 0). `cmpout.py`: 117 of 120 pieces found. The three missing ones are the short entries of the critical path: in this run no clock or procnto thread preempted psrv-rx, but devb-eide's `eide_driver_thread` (priority 21) did, for 291.7 us while it handled hi's request. Text fixed: the critical path step now says that these short entries differ from run to run, with this example, and that the long READY segment behind the hog was there in every run (it was: 8.1 to 8.4 ms in the development run and passes 1 to 4).

## Pass 4: 20:22:52 to 20:23:35, no changes needed

Same commands as pass 3 (compared with digits masked), commands 43.2 s (target step 17.0 s, of which 15.2 s for the address and first ssh). Results: plain run 20 of 20 missed (slowest 10975 us), fixed capture 20 of 20 (max 11344.0 us in the trace), 20 suspected inversions, fast-mode capture 0 suspected inversions, inherit capture 0 of 20 (max 2808.5 us), `diff` 20 to 0 and server priority 10 to 30. `cmpout.py`: 116 of 120 pieces found; the four missing ones are again the run-dependent short entries of the critical path, which the text now describes as such. No text change.

## Rendering

`~/.local/bin/claat export` (QNX's fork) of the final file, served next to the QNX codelab site's own `js/` assets (downloaded from `qnx.github.io/codelabs/js/`), opened step by step in headless Chromium at 1280 px: 13 steps, 4 images loaded, the table and both asides render, no code block overflows, no console errors. At 412 px code blocks are wider than the screen and scroll. The site's highlighter colours `text` blocks too, so an apostrophe (`lo's request`) starts a string colour that runs to the next apostrophe; this is cosmetic and left as it is, since the outputs must stay real.

## Clean-up

Target `tcl1` stopped with `mkqnximage --stop` and `~/targets/tcl1` removed after pass 4; no QEMU process is left in that directory. The pass directories stay on the box under `~/tcl-dryrun/`.
