/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_back: back-end server. ChannelCreate() channel published through
 * /tmp/ts_back.addr, one receive thread, a periodic timer that delivers a
 * pulse to its own channel. Serves requests forwarded by ts_front.
 *
 * usage: ts_back [-p timer_period_us]
 */
#include "common.h"
#include <signal.h>

int main(int argc, char **argv)
{
    unsigned period_us = 5000;
    int opt;
    while ((opt = getopt(argc, argv, "p:")) != -1)
        if (opt == 'p') period_us = (unsigned)atoi(optarg);

    ts_role = "back";
    ts_calibrate();
    pthread_setname_np(pthread_self(), "back-rx");

    int chid = ChannelCreate(0);
    if (chid == -1) { perror("ChannelCreate"); return 1; }
    int self_coid = ConnectAttach(0, 0, chid, _NTO_SIDE_CHANNEL, 0);
    if (self_coid == -1) { perror("ConnectAttach(self)"); return 1; }
    ts_log("CHANNEL", 0, chid, self_coid, 0, 0);

    struct sigevent ev;
    SIGEV_PULSE_INIT(&ev, self_coid, SIGEV_PULSE_PRIO_INHERIT, TS_PULSE_TICK, 0);
    timer_t tm;
    if (timer_create(CLOCK_MONOTONIC, &ev, &tm) == -1) { perror("timer_create"); return 1; }
    struct itimerspec its = {
        .it_value    = { 0, (long)period_us * 1000L },
        .it_interval = { 0, (long)period_us * 1000L },
    };
    if (timer_settime(tm, 0, &its, NULL) == -1) { perror("timer_settime"); return 1; }

    ts_write_addr("/tmp/ts_back.addr", chid);

    union { ts_msg_t m; struct _pulse p; } u;
    struct _msg_info info;
    unsigned ticks = 0;
    for (;;) {
        int rcvid = MsgReceive(chid, &u, sizeof u, &info);
        uint64_t t_rx = ClockCycles();
        if (rcvid == -1) { perror("MsgReceive"); continue; }
        if (rcvid == 0) {
            if (u.p.code == TS_PULSE_TICK) ticks++;
            ts_log_at(t_rx, "PULSE_RX", 0, u.p.code, u.p.value.sival_int, u.p.scoid, ticks);
            continue;
        }
        ts_log_at(t_rx, "RX", u.m.id, rcvid, info.pid, info.tid, info.coid);
        if (u.m.type == TS_MSG_QUIT) {
            MsgReply(rcvid, 0, NULL, 0);
            timer_delete(tm);
            ts_log("QUIT", 0, ticks, 0, 0, 0);
            ts_dump();
            return 0;
        }
        if (u.m.type != TS_MSG_REQ) { MsgError(rcvid, ENOSYS); continue; }
        ts_burn_us(u.m.work_us);
        ts_reply_t r = { u.m.id, 0, (uint32_t)getpid(), (uint32_t)pthread_self() };
        ts_log("REPLY", u.m.id, rcvid, info.pid, info.tid, 0);
        if (MsgReply(rcvid, 0, &r, sizeof r) == -1) perror("MsgReply");
    }
}
