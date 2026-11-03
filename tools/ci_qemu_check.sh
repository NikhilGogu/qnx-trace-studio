#!/bin/bash
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
#
# A CI job that gates on a QNX 8.0 real-time budget: build the workload with the SDP, boot a fresh QEMU
# target with mkqnximage, capture SCENARIO with tracelogger, fetch the .kev and run `tracestudio check`.
#
#   tools/ci_qemu_check.sh SCENARIO [tracestudio check options]
#   tools/ci_qemu_check.sh prio_fixed --deadline hi=5000 --no-inversions
#
# Needs QNX SDP 8.0 ($QNX_SDP, default ~/qnx800), KVM, ~/.ssh/id_ed25519.pub, python3 with venv and network
# access for pip. Work files (venv, target image, .kev, JUnit XML) go to $CI_WORK (default: a new temp dir).
# The target is stopped on exit. Exit status: that of tracestudio check (0 pass, 1 an assertion failed,
# 2 usage), or that of the step that failed.
set -euo pipefail
SCEN=$1
shift
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=${CI_WORK:-$(mktemp -d)}
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes"
mkdir -p "$WORK/target"
set -x
source "${QNX_SDP:-$HOME/qnx800}/qnxsdp-env.sh" > /dev/null
make -C "$REPO/workload"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "$REPO"
cd "$WORK/target"
mkqnximage --type=qemu --arch=x86_64 --ssh-ident="$HOME/.ssh/id_ed25519.pub" --noprompt --run=-h > "$WORK/mkqnximage.log" 2>&1
trap 'cd "$WORK/target" && mkqnximage --stop > /dev/null 2>&1' EXIT
IP=$(mkqnximage --getip | tail -1)
for i in $(seq 1 60); do ssh $SSHO "root@$IP" true && break; sleep 2; done
ssh $SSHO "root@$IP" mkdir -p /tmp/ts/bin
scp -q $SSHO "$REPO"/workload/build/x86_64/ts_* "$REPO/workload/run_scenarios.sh" "root@$IP:/tmp/ts/bin/"
ssh $SSHO "root@$IP" sh /tmp/ts/bin/run_scenarios.sh /tmp/ts/bin /tmp/ts/out "$SCEN"
scp -q $SSHO "root@$IP:/tmp/ts/out/$SCEN.kev" "$WORK/"
"$WORK/venv/bin/tracestudio" check "$WORK/$SCEN.kev" --junit "$WORK/$SCEN.junit.xml" "$@"
