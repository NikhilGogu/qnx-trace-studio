#!/bin/sh
# Run on the QNX 8.0 target as root. Captures one kernel event trace per
# scenario with tracelogger and keeps each process's ground-truth CSV log.
#   run_scenarios.sh <bindir> <outdir> [scenario...]
# scenarios: ipc_wide ipc_fast prio_inherit prio_fixed (default: all four)
B=${1:-/tmp/ts/bin}
O=${2:-/tmp/ts/out}
shift 2
SCEN=${*:-"ipc_wide ipc_fast prio_inherit prio_fixed"}
mkdir -p /tmp/ts_logs "$O"

# -k 32: 32 kernel buffers per CPU (16 KB each) so short bursts are not
# dropped; -b 128: tracelogger-side buffers; -s: time-based capture.
start_trace() {   # <name> <mode-flag> <seconds>
    rm -f /tmp/ts_logs/*.csv
    tracelogger $2 -k 32 -b 128 -s $3 -A SCENARIO=$1 -f "$O/$1.kev" > "$O/$1.tracelogger.txt" 2>&1 &
    TL=$!
    sleep 1
}

finish() {        # <name>
    wait $TL
    mkdir -p "$O/$1.logs"
    cp /tmp/ts_logs/*.csv "$O/$1.logs/"
    echo "$1: $(ls -l "$O/$1.kev" | awk '{print $5}') bytes, logs: $(ls "$O/$1.logs" | tr '\n' ' ')"
}

run_ipc() {       # <name> <mode-flag>
    rm -f /tmp/ts_back.addr
    start_trace $1 "$2" 4
    "$B/ts_back" &
    sleep 1
    "$B/ts_front" -t 2 &
    sleep 1
    "$B/ts_clients" -c 4 -n 40 -q
    finish $1
}

run_prio() {      # <name> <psrv-flag>
    rm -f /tmp/ts_psrv.addr
    start_trace $1 -w 4
    "$B/ts_psrv" $2 &
    sleep 1
    "$B/ts_pload" -n 20
    finish $1
}

for s in $SCEN; do
    case $s in
    ipc_wide)     run_ipc ipc_wide -w ;;
    ipc_fast)     run_ipc ipc_fast "" ;;
    prio_inherit) run_prio prio_inherit "" ;;
    prio_fixed)   run_prio prio_fixed -f ;;
    esac
done
