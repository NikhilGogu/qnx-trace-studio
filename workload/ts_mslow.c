/* SPDX-License-Identifier: Apache-2.0 */
/* ts_mslow: a server that burns -w microseconds of CPU per request at priority 10 on CPU 0 (the "C" of the
 * mutex scenario: ts_mutex's holder thread sends to it while it owns the mutex). */
#include "common.h"

int main(int argc, char **argv)
{
    int opt;
    ts_role = "mslow";
    while ((opt = getopt(argc, argv, "")) != -1) {}
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 10);
    ts_calibrate();
    pthread_setname_np(pthread_self(), "slow-rx");
    int chid = ChannelCreate(0);
    if (chid == -1) { perror("ChannelCreate"); return 1; }
    ts_write_addr("/tmp/ts_mslow.addr", chid);
    for (;;) {
        ts_msg_t m;
        struct _msg_info info;
        int rcvid = MsgReceive(chid, &m, sizeof m, &info);
        if (rcvid <= 0) continue;
        ts_log("RX", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        if (m.type == TS_MSG_QUIT) { MsgReply(rcvid, 0, NULL, 0); break; }
        ts_burn_us(m.work_us);
        ts_reply_t r = { m.id, 0, (uint32_t)getpid(), (uint32_t)pthread_self() };
        ts_log("REPLY", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        MsgReply(rcvid, 0, &r, sizeof r);
    }
    ts_dump();
    return 0;
}
