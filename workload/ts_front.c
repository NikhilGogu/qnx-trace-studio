/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_front: front-end server registered with name_attach("ts_front").
 * Several receive threads share the channel (concurrent receivers). Requests
 * flagged TS_F_FORWARD are forwarded to ts_back with MsgSend() while the
 * client stays REPLY-blocked, so one request crosses two servers.
 *
 * usage: ts_front [-t receive_threads]
 */
#include "common.h"
#include <sys/dispatch.h>
#include <sys/iomsg.h>

static name_attach_t *att;
static int back_coid = -1;

static void *rx_thread(void *arg)
{
    char name[32];
    snprintf(name, sizeof name, "front-rx%d", (int)(intptr_t)arg);
    pthread_setname_np(pthread_self(), name);

    union { ts_msg_t m; struct _pulse p; struct _io_connect c; uint16_t type; } u;
    struct _msg_info info;
    for (;;) {
        int rcvid = MsgReceive(att->chid, &u, sizeof u, &info);
        uint64_t t_rx = ClockCycles();
        if (rcvid == -1) { perror("MsgReceive"); continue; }
        if (rcvid == 0) {
            ts_log_at(t_rx, "PULSE_RX", (uint32_t)u.p.value.sival_int, u.p.code, u.p.value.sival_int, u.p.scoid, 0);
            if (u.p.code == _PULSE_CODE_DISCONNECT) ConnectDetach(u.p.scoid);
            continue;
        }
        if (u.type == _IO_CONNECT) {  /* name_open() handshake */
            ts_log_at(t_rx, "CONNECT", 0, rcvid, info.pid, info.tid, info.coid);
            MsgReply(rcvid, EOK, NULL, 0);
            continue;
        }
        if (u.type >= _IO_BASE && u.type <= _IO_MAX) { MsgError(rcvid, ENOSYS); continue; }

        ts_log_at(t_rx, "RX", u.m.id, rcvid, info.pid, info.tid, info.coid);
        if (u.m.type == TS_MSG_QUIT) {
            ts_msg_t q = { TS_MSG_QUIT, 0, 0, 0, 0 };
            MsgSend(back_coid, &q, sizeof q, NULL, 0);
            MsgReply(rcvid, 0, NULL, 0);
            ts_log("QUIT", 0, 0, 0, 0, 0);
            ts_dump();
            exit(0);
        }
        if (u.m.type != TS_MSG_REQ) { MsgError(rcvid, ENOSYS); continue; }

        ts_burn_us(u.m.work_us / 2);
        ts_reply_t r = { u.m.id, 0, (uint32_t)getpid(), (uint32_t)pthread_self() };
        if (u.m.flags & TS_F_FORWARD) {
            ts_msg_t f = { TS_MSG_REQ, 0, u.m.id, u.m.work_us, u.m.origin_tid };
            ts_reply_t br;
            ts_log("FWD_SEND", u.m.id, back_coid, rcvid, 0, 0);
            long st = MsgSend(back_coid, &f, sizeof f, &br, sizeof br);
            ts_log("FWD_DONE", u.m.id, back_coid, st, br.server_pid, br.server_tid);
            r.status = (int32_t)br.server_tid;
        }
        ts_burn_us(u.m.work_us / 2);
        ts_log("REPLY", u.m.id, rcvid, info.pid, info.tid, 0);
        if (MsgReply(rcvid, 0, &r, sizeof r) == -1) perror("MsgReply");
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int nthreads = 2, opt;
    while ((opt = getopt(argc, argv, "t:")) != -1)
        if (opt == 't') nthreads = atoi(optarg);

    ts_role = "front";
    ts_calibrate();

    int bpid, bchid;
    if (ts_read_addr("/tmp/ts_back.addr", &bpid, &bchid)) { fprintf(stderr, "no ts_back\n"); return 1; }
    back_coid = ConnectAttach(0, bpid, bchid, _NTO_SIDE_CHANNEL, 0);
    if (back_coid == -1) { perror("ConnectAttach(back)"); return 1; }

    att = name_attach(NULL, "ts_front", 0);
    if (!att) { perror("name_attach"); return 1; }
    ts_log("CHANNEL", 0, att->chid, back_coid, bpid, bchid);

    pthread_t th[16];
    for (int i = 1; i < nthreads && i < 16; i++)
        pthread_create(&th[i], NULL, rx_thread, (void *)(intptr_t)i);
    rx_thread((void *)0);
    return 0;
}
