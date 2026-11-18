#!/bin/sh
# Run on the QNX 8.0 target as root. Captures one kernel event trace per
# scenario with tracelogger and keeps each process's ground-truth CSV log.
#   run_scenarios.sh <bindir> <outdir> [scenario...]
# scenarios: ipc_wide ipc_fast prio_inherit prio_fixed (default: all four),
#            prio_inherit_rx prio_fixed_rx (hog preempts the server after it received hi's request),
#            cancel (timeouts, signals and late replies), rm_traffic (ls, find, cat, wc on qnx6 file systems),
#            rm_small (a short capture of a few known file reads, used as a test fixture),
#            mutex_chain, mutex_chain_hog (a mutex owner REPLY-blocked on another server; with a hog at 20),
#            mutex_chain_nopi (as mutex_chain_hog with PTHREAD_PRIO_NONE: no mutex priority inheritance),
#            pool_fixed, pool_inherit (a two-thread server pool, both threads busy when hi sends; with
#            inheritance a busy thread runs at hi's priority and hi is served in time),
#            seeded regressions for tools/seeded_regressions.py, each a healthy parent with one change:
#            prio_inherit_late (lo 6 ms, hog 1 ms after hi sends; healthy), prio_hog_high (hog raised to 35,
#            above hi), prio_hi_low (hi lowered to 15, below the hog), mutex_chain_slowfixed (ts_mslow's channel
#            fixed-priority), pool_wide (one fixed-priority receiver that may use CPU 0 and 1; healthy),
#            pool_narrow (its run mask narrowed to CPU 0), pool_long (two inheriting receivers, lo asks for 9 ms;
#            healthy), pool_long_one (the pool cut to one receiver), pool_long_fixed (the pool's channel
#            fixed-priority)
B=${1:-/tmp/ts/bin}
O=${2:-/tmp/ts/out}
shift 2
SCEN=${*:-"ipc_wide ipc_fast prio_inherit prio_fixed"}
mkdir -p /tmp/ts_logs "$O"

# -k 32: 32 kernel buffers per CPU (16 KB each) so short bursts are not
# dropped; -b 128: tracelogger-side buffers; -s: time-based capture (TS_SECONDS overrides the length,
# for slow emulated targets).
start_trace() {   # <name> <mode-flag> <seconds>
    rm -f /tmp/ts_logs/*.csv
    tracelogger $2 -k 32 -b 128 -s ${TS_SECONDS:-$3} -A SCENARIO=$1 -f "$O/$1.kev" > "$O/$1.tracelogger.txt" 2>&1 &
    TL=$!
    sleep 1
}

finish() {        # <name>
    wait $TL
    mkdir -p "$O/$1.logs"
    cp /tmp/ts_logs/*.csv "$O/$1.logs/" 2>/dev/null
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

run_prio() {      # <name> <psrv-flag> [ts_pload options]
    rm -f /tmp/ts_psrv.addr
    start_trace $1 -w 4
    "$B/ts_psrv" $2 &
    sleep 1
    "$B/ts_pload" -n 20 $3
    finish $1
}

run_mutex() {     # <name> [ts_mutex options] [ts_mslow options]
    rm -f /tmp/ts_mslow.addr
    start_trace $1 -w 4
    "$B/ts_mslow" $3 &
    sleep 1
    "$B/ts_mutex" -n 20 $2
    finish $1
}

run_pool() {      # <name> <ts_pool options> <ts_pload options>
    rm -f /tmp/ts_psrv.addr
    start_trace $1 -w 4
    "$B/ts_pool" $2 &
    sleep 1
    "$B/ts_pload" -n 20 $3
    finish $1
}

run_cancel() {
    start_trace cancel -w 3
    "$B/ts_cancel" -n 5
    finish cancel
}

run_rm() {
    start_trace rm_traffic -w 3
    ls -l /system/bin /data > /dev/null
    find /system/lib -name '*.so*' > /dev/null
    cat /system/lib/*.so* > /dev/null 2>&1
    dd if=/dev/zero of=/data/ts_rm.bin bs=65536 count=8 2>/dev/null
    cat /data/ts_rm.bin > /dev/null
    rm -f /data/ts_rm.bin
    wc -c /system/etc/* > /dev/null 2>&1
    finish rm_traffic
}

run_rm_small() {   # known sizes: libxo.so.0 as on the target, ts_rm.bin 65536 bytes
    start_trace rm_small -w 2
    cat /system/lib/libxo.so.0 > /dev/null
    dd if=/dev/zero of=/data/ts_rm.bin bs=4096 count=16 2>/dev/null
    cat /data/ts_rm.bin > /dev/null
    ls -l /data > /dev/null
    rm -f /data/ts_rm.bin
    finish rm_small
}

for s in $SCEN; do
    case $s in
    ipc_wide)     run_ipc ipc_wide -w ;;
    ipc_fast)     run_ipc ipc_fast "" ;;
    prio_inherit) run_prio prio_inherit "" ;;
    prio_fixed)   run_prio prio_fixed -f ;;
    mutex_chain)     run_mutex mutex_chain ;;
    mutex_chain_hog) run_mutex mutex_chain_hog -g ;;
    mutex_chain_nopi) run_mutex mutex_chain_nopi "-g -N" ;;
    prio_inherit_rx) run_prio prio_inherit_rx "" "-l 500 -o 0,4000,4000" ;;
    prio_fixed_rx)   run_prio prio_fixed_rx -f "-l 500 -o 0,4000,4000" ;;
    pool_fixed)   run_pool pool_fixed "-f -n 2 -a" "-k 2 -L 11,12 -s 1000" ;;
    pool_inherit) run_pool pool_inherit "-n 2 -a" "-k 2 -L 11,12 -s 1000" ;;
    prio_inherit_late) run_prio prio_inherit_late "" "-l 6000 -o 0,5000,4000" ;;
    prio_hog_high)     run_prio prio_hog_high "" "-l 6000 -o 0,5000,4000 -H 35" ;;
    prio_hi_low)       run_prio prio_hi_low "" "-l 6000 -o 0,5000,4000 -I 15" ;;
    mutex_chain_slowfixed) run_mutex mutex_chain_slowfixed -g -f ;;
    pool_wide)       run_pool pool_wide "-f -n 1 -c 0x3" "" ;;
    pool_narrow)     run_pool pool_narrow "-f -n 1 -c 0x1" "" ;;
    pool_long)       run_pool pool_long "-n 2 -a" "-l 9000" ;;
    pool_long_one)   run_pool pool_long_one "-n 1 -a" "-l 9000" ;;
    pool_long_fixed) run_pool pool_long_fixed "-f -n 2 -a" "-l 9000" ;;
    cancel)       run_cancel ;;
    rm_traffic)   run_rm ;;
    rm_small)     run_rm_small ;;
    esac
done
