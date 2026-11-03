/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_clients: concurrent client threads sending numbered requests to
 * ts_front with MsgSend(). Every fourth request asks ts_front to forward it
 * to ts_back. Client 0 also sends a MsgSendPulse() every tenth request.
 * Request id = (client index + 1) * 1000 + sequence number, unique per run.
 *
 * usage: ts_clients [-c clients] [-n requests_per_client] [-w work_us] [-q]
 *   -q  send QUIT to ts_front (and through it ts_back) when done
 */
#include "common.h"
#include <sys/dispatch.h>

static int nreq = 40;
static unsigned work_us = 200;
static pthread_barrier_t start_bar;

static void *client(void *arg)
{
    int idx = (int)(intptr_t)arg;
    char name[32];
    snprintf(name, sizeof name, "client-%d", idx);
    pthread_setname_np(pthread_self(), name);

    int coid = name_open("ts_front", 0);
    if (coid == -1) { perror("name_open(ts_front)"); return NULL; }
    ts_log("OPEN", 0, coid, 0, 0, 0);
    unsigned seed = (unsigned)idx * 7919u + 17u;
    pthread_barrier_wait(&start_bar);

    for (int s = 0; s < nreq; s++) {
        uint32_t id = (uint32_t)(idx + 1) * 1000u + (uint32_t)s;
        ts_msg_t m = { TS_MSG_REQ, (s % 4 == 3) ? TS_F_FORWARD : 0, id, work_us, (uint32_t)pthread_self() };
        ts_reply_t r = { 0, 0, 0, 0 };
        uint64_t t0 = ClockCycles();
        long st = MsgSend(coid, &m, sizeof m, &r, sizeof r);
        uint64_t t1 = ClockCycles();
        ts_log_at(t0, "SEND", id, coid, m.flags, 0, 0);
        ts_log_at(t1, "SEND_DONE", id, coid, st, r.server_pid, r.server_tid);
        if (st == -1) perror("MsgSend");
        if (idx == 0 && s % 10 == 9) {
            uint64_t tp = ClockCycles();
            if (MsgSendPulse(coid, -1, TS_PULSE_STATS, (int)id) == -1) perror("MsgSendPulse");
            ts_log_at(tp, "PULSE_SEND", id, coid, TS_PULSE_STATS, 0, 0);
        }
        seed = seed * 1103515245u + 12345u;
        usleep(200 + (seed >> 16) % 1300);
    }
    name_close(coid);
    return NULL;
}

int main(int argc, char **argv)
{
    int nclients = 4, quit = 0, opt;
    while ((opt = getopt(argc, argv, "c:n:w:q")) != -1) {
        switch (opt) {
        case 'c': nclients = atoi(optarg); break;
        case 'n': nreq = atoi(optarg); break;
        case 'w': work_us = (unsigned)atoi(optarg); break;
        case 'q': quit = 1; break;
        }
    }
    if (nclients > 32) nclients = 32;
    ts_role = "clients";

    pthread_barrier_init(&start_bar, NULL, (unsigned)nclients);
    pthread_t th[32];
    for (int i = 0; i < nclients; i++)
        pthread_create(&th[i], NULL, client, (void *)(intptr_t)i);
    for (int i = 0; i < nclients; i++)
        pthread_join(th[i], NULL);

    if (quit) {
        int coid = name_open("ts_front", 0);
        ts_msg_t q = { TS_MSG_QUIT, 0, 0, 0, 0 };
        if (coid != -1) { MsgSend(coid, &q, sizeof q, NULL, 0); name_close(coid); }
    }
    ts_dump();
    return 0;
}
