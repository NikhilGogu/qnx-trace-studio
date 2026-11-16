/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_pool: multi-threaded server for the thread-pool priority scenario. It
 * serves the same protocol as ts_psrv, so ts_pload drives it unchanged, but N
 * threads receive on one channel. Every thread runs at SCHED_FIFO priority 10
 * with the run mask given by -c (default CPU 0 only). The masks are set after
 * the channel is created, so a trace started before ts_pool records them.
 * With -f the channel has _NTO_CHF_FIXED_PRIORITY.
 *
 * With -a an extra thread that never receives sleeps in a loop. The inversion
 * analysis must account for every thread of the server process, so this
 * thread checks that a thread blocked in another kernel call is accepted.
 *
 * The log role starts with "psrv" so that tracestudio validate pairs ts_pload's
 * requests with this server, as it does for ts_psrv.
 *
 * usage: ts_pool [-f] [-n receivers] [-a] [-c cpumask]
 */
#include "common.h"

#define TS_POOL_MAX 8

static int chid;
static uintptr_t runmask = 0x1;

static void set_runmask(void)
{
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)runmask) == -1)
        perror("ThreadCtl(_NTO_TCTL_RUNMASK)");
}

static void *receiver(void *arg)
{
    char name[16];
    snprintf(name, sizeof name, "pool-rx%d", (int)(intptr_t)arg);
    pthread_setname_np(pthread_self(), name);
    set_runmask();
    ts_set_prio(SCHED_FIFO, 10);

    ts_msg_t m;
    struct _msg_info info;
    for (;;) {
        int rcvid = MsgReceive(chid, &m, sizeof m, &info);
        uint64_t t_rx = ClockCycles();
        if (rcvid == -1) { perror("MsgReceive"); continue; }
        if (rcvid == 0) continue;
        /* a = rcvid, b = client pid, c = client tid, d = our priority now */
        ts_log_at(t_rx, "RX", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        if (m.type == TS_MSG_QUIT) {
            MsgReply(rcvid, 0, NULL, 0);
            ts_dump();
            exit(0);              /* ends the other threads too */
        }
        ts_burn_us(m.work_us);
        ts_reply_t r = { m.id, 0, (uint32_t)getpid(), (uint32_t)pthread_self() };
        ts_log("REPLY", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        MsgReply(rcvid, 0, &r, sizeof r);
    }
}

static void *sleeper(void *arg)
{
    pthread_setname_np(pthread_self(), "pool-aux");
    set_runmask();
    ts_set_prio(SCHED_FIFO, 10);
    for (;;)
        usleep(100000);
}

int main(int argc, char **argv)
{
    int fixed = 0, n = 2, aux = 0, opt;
    while ((opt = getopt(argc, argv, "fn:ac:")) != -1) {
        switch (opt) {
        case 'f': fixed = 1; break;
        case 'n': n = atoi(optarg); break;
        case 'a': aux = 1; break;
        case 'c': runmask = (uintptr_t)strtoul(optarg, NULL, 0); break;
        }
    }
    if (n < 1 || n > TS_POOL_MAX || runmask == 0) {
        fprintf(stderr, "want 1 to %d receivers and a nonzero run mask\n", TS_POOL_MAX);
        return 2;
    }

    ts_role = fixed ? "psrv_pool_fixed" : "psrv_pool_inherit";
    pthread_setname_np(pthread_self(), "pool-main");
    set_runmask();
    ts_calibrate();

    chid = ChannelCreate(fixed ? _NTO_CHF_FIXED_PRIORITY : 0);
    if (chid == -1) { perror("ChannelCreate"); return 1; }
    ts_log("CHANNEL", 0, chid, fixed, n, (int64_t)runmask);

    pthread_t th[TS_POOL_MAX + 1];
    for (int i = 0; i < n; i++)
        pthread_create(&th[i], NULL, receiver, (void *)(intptr_t)i);
    if (aux)
        pthread_create(&th[n], NULL, sleeper, NULL);
    ts_write_addr("/tmp/ts_psrv.addr", chid);
    pthread_join(th[0], NULL);    /* never returns: the QUIT request exits the process */
    return 0;
}
