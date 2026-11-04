/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_psrv: single-threaded server for the priority scenario. Pinned to CPU 0
 * at SCHED_FIFO priority 10. With -f the channel is created with
 * _NTO_CHF_FIXED_PRIORITY, so the receive thread keeps priority 10 whatever the
 * priority of its clients.
 *
 * usage: ts_psrv [-f]
 */
#include "common.h"

int main(int argc, char **argv)
{
    int fixed = 0, opt;
    while ((opt = getopt(argc, argv, "f")) != -1)
        if (opt == 'f') fixed = 1;

    ts_role = fixed ? "psrv_fixed" : "psrv_inherit";
    pthread_setname_np(pthread_self(), "psrv-rx");
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 10);
    ts_calibrate();

    int chid = ChannelCreate(fixed ? _NTO_CHF_FIXED_PRIORITY : 0);
    if (chid == -1) { perror("ChannelCreate"); return 1; }
    ts_log("CHANNEL", 0, chid, fixed, 0, 0);
    ts_write_addr("/tmp/ts_psrv.addr", chid);

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
            return 0;
        }
        ts_burn_us(m.work_us);
        ts_reply_t r = { m.id, 0, (uint32_t)getpid(), (uint32_t)pthread_self() };
        ts_log("REPLY", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        MsgReply(rcvid, 0, &r, sizeof r);
    }
}
