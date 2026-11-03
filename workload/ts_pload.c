/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ts_pload: load for the priority scenario, all threads pinned to CPU 0,
 * SCHED_FIFO. Every period P:
 *   t+0      lo  (prio 10) sends a request needing lo_us of server CPU
 *   t+2 ms   hog (prio 20) burns hog_us of CPU, no IPC
 *   t+4 ms   hi  (prio 30) sends a request needing 500 us, deadline dl_us
 * With priority inheritance the server is boosted to 30 when hi sends and
 * finishes lo's request ahead of the hog; with a fixed-priority channel
 * (ts_psrv -f) the hog runs first and hi misses its deadline.
 * At exit it prints how many of hi's requests missed the deadline.
 * -o LO,HOG,HI moves the three start offsets (us); for example -o 0,4200,4000
 * with a short lo request makes the hog preempt the server while it already
 * handles hi's request (inversion after the receive).
 *
 * usage: ts_pload [-n periods] [-P period_us] [-l lo_us] [-m hog_us] [-d dl_us] [-o lo,hog,hi]
 */
#include "common.h"

static int nper = 20;
static unsigned period_us = 25000, lo_us = 4000, hog_us = 10000, hi_us = 500, dl_us = 5000;
static unsigned off_lo = 0, off_hog = 2000, off_hi = 4000;
static struct timespec t_base;
static int srv_pid, srv_chid;
static pthread_barrier_t bar;
static unsigned hi_n, hi_missed;    /* written by the hi thread only, read after join */
static uint64_t hi_max_cyc;

static void at(int k, unsigned off_us)
{
    struct timespec ts = t_base;
    uint64_t ns = (uint64_t)k * period_us * 1000u + (uint64_t)off_us * 1000u + (uint64_t)ts.tv_nsec;
    ts.tv_sec += (time_t)(ns / 1000000000u);
    ts.tv_nsec = (long)(ns % 1000000000u);
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
}

typedef struct { const char *name; int prio; unsigned off_us; unsigned work_us; int role; } spec_t;

static void *worker(void *arg)
{
    spec_t *s = arg;
    pthread_setname_np(pthread_self(), s->name);
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, s->prio);
    int coid = -1;
    if (s->role != 1) {
        coid = ConnectAttach(0, srv_pid, srv_chid, _NTO_SIDE_CHANNEL, 0);
        if (coid == -1) { perror("ConnectAttach"); return NULL; }
        ts_log("OPEN", 0, coid, s->prio, 0, 0);
    }
    pthread_barrier_wait(&bar);
    uint64_t dl_cyc = ts_us_to_cycles(dl_us);
    for (int k = 0; k < nper; k++) {
        at(k, s->off_us);
        if (s->role == 1) {           /* hog */
            ts_log("HOG_BEGIN", (uint32_t)k, s->prio, 0, 0, 0);
            ts_burn_us(s->work_us);
            ts_log("HOG_END", (uint32_t)k, s->prio, 0, 0, 0);
            continue;
        }
        uint32_t id = (uint32_t)(s->prio * 1000 + k);
        ts_msg_t m = { TS_MSG_PRIO, 0, id, s->work_us, (uint32_t)pthread_self() };
        ts_reply_t r;
        uint64_t t0 = ClockCycles();
        long st = MsgSend(coid, &m, sizeof m, &r, sizeof r);
        uint64_t t1 = ClockCycles();
        ts_log_at(t0, "SEND", id, coid, s->prio, 0, 0);
        /* c = latency in cycles, d = 1 if the deadline was missed (hi only) */
        ts_log_at(t1, "SEND_DONE", id, coid, st, (int64_t)(t1 - t0),
                  (s->role == 2 && (t1 - t0) > dl_cyc) ? 1 : 0);
        if (s->role == 2) {
            hi_n++;
            if (t1 - t0 > dl_cyc) hi_missed++;
            if (t1 - t0 > hi_max_cyc) hi_max_cyc = t1 - t0;
        }
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int opt;
    while ((opt = getopt(argc, argv, "n:P:l:m:d:o:")) != -1) {
        switch (opt) {
        case 'n': nper = atoi(optarg); break;
        case 'P': period_us = (unsigned)atoi(optarg); break;
        case 'l': lo_us = (unsigned)atoi(optarg); break;
        case 'm': hog_us = (unsigned)atoi(optarg); break;
        case 'd': dl_us = (unsigned)atoi(optarg); break;
        case 'o':
            if (sscanf(optarg, "%u,%u,%u", &off_lo, &off_hog, &off_hi) != 3) {
                fprintf(stderr, "-o wants LO,HOG,HI offsets in us\n");
                return 2;
            }
            break;
        }
    }
    ts_role = "pload";
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 40);
    ts_calibrate();
    if (ts_read_addr("/tmp/ts_psrv.addr", &srv_pid, &srv_chid)) { fprintf(stderr, "no ts_psrv\n"); return 1; }

    spec_t sp[3] = {
        { "lo", 10, off_lo, lo_us, 0 },
        { "hog", 20, off_hog, hog_us, 1 },
        { "hi", 30, off_hi, hi_us, 2 },
    };
    pthread_barrier_init(&bar, NULL, 4);
    pthread_t th[3];
    for (int i = 0; i < 3; i++) pthread_create(&th[i], NULL, worker, &sp[i]);
    clock_gettime(CLOCK_MONOTONIC, &t_base);
    t_base.tv_sec += 1;   /* first period starts one second from now */
    t_base.tv_nsec = 0;
    pthread_barrier_wait(&bar);
    for (int i = 0; i < 3; i++) pthread_join(th[i], NULL);

    int coid = ConnectAttach(0, srv_pid, srv_chid, _NTO_SIDE_CHANNEL, 0);
    ts_msg_t q = { TS_MSG_QUIT, 0, 0, 0, 0 };
    MsgSend(coid, &q, sizeof q, NULL, 0);
    ts_dump();
    printf("hi: %u of %u requests missed the %u us deadline, slowest %.0f us\n", hi_missed, hi_n, dl_us,
           (double)hi_max_cyc * 1e6 / (double)SYSPAGE_ENTRY(qtime)->cycles_per_sec);
    return 0;
}
