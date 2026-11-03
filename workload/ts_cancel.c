/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_cancel: message exchanges that end without a reply, to check how the
 * decoder handles cancellation. A server thread receives every request and
 * replies only 30 ms later. Each round three client threads send one request:
 *   "ok"  waits for the reply,
 *   "tmo" arms TimerTimeout(SEND|REPLY, 5 ms) and returns ETIMEDOUT,
 *   "sig" is hit by SIGUSR1 from the main thread after 5 ms and returns EINTR.
 * The server's late MsgReply to the cancelled clients fails. Everything is
 * logged (SEND, SEND_DONE with status and errno, RX, REPLY with MsgReply's
 * result and errno).
 *
 * usage: ts_cancel [-n rounds]
 */
#include "common.h"
#include <signal.h>

static int chid;
static pthread_barrier_t bar;

static void on_usr1(int sig) { (void)sig; }

static void *server(void *arg)
{
    pthread_setname_np(pthread_self(), "csrv");
    ts_msg_t m;
    for (;;) {
        int rcv[3], n = 0;
        while (n < 3) {
            int rcvid = MsgReceive(chid, &m, sizeof m, NULL);
            if (rcvid <= 0) continue;
            ts_log("RX", m.id, rcvid, 0, 0, 0);
            if (m.type == TS_MSG_QUIT) { MsgReply(rcvid, 0, NULL, 0); return NULL; }
            rcv[n++] = rcvid;
        }
        usleep(30000);
        for (int i = 0; i < n; i++) {
            int rc = MsgReply(rcv[i], 0, NULL, 0);
            ts_log("REPLY", 0, rcv[i], rc, rc == -1 ? errno : 0, 0);
        }
    }
}

typedef struct { const char *name; int kind; pthread_t th; } cl_t;

static void *client(void *arg)
{
    cl_t *c = arg;
    pthread_setname_np(pthread_self(), c->name);
    signal(SIGUSR1, on_usr1);
    int coid = ConnectAttach(0, 0, chid, _NTO_SIDE_CHANNEL, 0);
    for (int k = 0;; k++) {       /* runs until main returns */
        pthread_barrier_wait(&bar);
        ts_msg_t m = { TS_MSG_REQ, 0, (uint32_t)(c->kind * 1000 + k), 0, 0 };
        if (c->kind == 1) {
            uint64_t ns = 5000000;
            TimerTimeout(CLOCK_MONOTONIC, _NTO_TIMEOUT_SEND | _NTO_TIMEOUT_REPLY, NULL, &ns, NULL);
        }
        uint64_t t0 = ClockCycles();
        long st = MsgSend(coid, &m, sizeof m, NULL, 0);
        int err = st == -1 ? errno : 0;
        uint64_t t1 = ClockCycles();
        ts_log_at(t0, "SEND", m.id, coid, c->kind, 0, 0);
        ts_log_at(t1, "SEND_DONE", m.id, coid, st, err, 0);
        pthread_barrier_wait(&bar);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int rounds = 5, opt;
    while ((opt = getopt(argc, argv, "n:")) != -1)
        if (opt == 'n') rounds = atoi(optarg);
    ts_role = "cancel";
    chid = ChannelCreate(0);
    if (chid == -1) { perror("ChannelCreate"); return 1; }
    pthread_t srv;
    pthread_create(&srv, NULL, server, NULL);
    static cl_t cl[3] = { { "ok", 0, 0 }, { "tmo", 1, 0 }, { "sig", 2, 0 } };
    pthread_barrier_init(&bar, NULL, 4);
    for (int i = 0; i < 3; i++) pthread_create(&cl[i].th, NULL, client, &cl[i]);
    for (int k = 0; k < rounds; k++) {
        pthread_barrier_wait(&bar);           /* clients send */
        usleep(5000);
        pthread_kill(cl[2].th, SIGUSR1);
        pthread_barrier_wait(&bar);           /* all three returned */
        usleep(40000);                        /* let the late replies happen */
    }
    int coid = ConnectAttach(0, 0, chid, _NTO_SIDE_CHANNEL, 0);
    ts_msg_t q = { TS_MSG_QUIT, 0, 0, 0, 0 };
    MsgSend(coid, &q, sizeof q, NULL, 0);
    ts_dump();
    return 0;
}
