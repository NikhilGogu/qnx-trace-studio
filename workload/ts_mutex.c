/* SPDX-License-Identifier: Apache-2.0 */
/* ts_mutex: a mutex owner chain mixed with message passing, all on CPU 0, period -p us, -n periods.
 *   holder (prio 10) locks M at the start of each period and, while it owns M, sends ts_mslow a request
 *            for -w us of work (so it is REPLY-blocked on ts_mslow while owning M);
 *   hi     (prio 30) sends this process's channel a request -d us later;
 *   mrx    (prio 10, inherits hi's 30 from the message) receives it and locks M: MUTEX-blocked on holder,
 *            whose priority QNX's mutex priority inheritance raises; then it replies;
 *   hog    (prio 20, with -g) burns -b us of CPU from -h us after the period start;
 *   -N     initialises M with PTHREAD_PRIO_NONE instead of QNX's default priority inheritance.
 * Logged: holder LOCKED (a = &M, b = its TLS __owner, c = pid), MSEND/MSEND_DONE, UNLOCK; mrx RX, WAIT, ACQUIRED,
 * REPLY; hi SEND, SEND_DONE; hog BURN. */
#include "common.h"
#include <sys/storage.h>

static pthread_mutex_t M = PTHREAD_MUTEX_INITIALIZER;
static int nper = 20, period_us = 25000, work_us = 3000, delay_us = 1000, hog = 0, hog_at = 1500, hog_us = 5000;
static int no_inherit = 0;
static int chid, slow_coid;
static struct timespec t0;

static void at(int k, int off_us)       /* sleep until period k + off_us */
{
    struct timespec ts = t0;
    long long ns = (long long)k * period_us * 1000LL + (long long)off_us * 1000LL;
    ts.tv_sec += ns / 1000000000LL;
    ts.tv_nsec += ns % 1000000000LL;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
}

static void *holder(void *arg)
{
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 10);
    pthread_setname_np(pthread_self(), "holder");
    for (int k = 0; k < nper; k++) {
        at(k, 0);
        pthread_mutex_lock(&M);
        ts_log("LOCKED", k + 1, (int64_t)(uintptr_t)&M, __tls()->__owner, getpid(), ts_cur_prio());
        ts_msg_t m = { TS_MSG_REQ, 0, 1000 + k + 1, (uint32_t)work_us, (uint32_t)pthread_self() };
        ts_reply_t r;
        ts_log("MSEND", k + 1, slow_coid, 0, 0, 0);
        int st = MsgSend(slow_coid, &m, sizeof m, &r, sizeof r);
        ts_log("MSEND_DONE", k + 1, slow_coid, st, r.server_pid, r.server_tid);
        ts_log("UNLOCK", k + 1, (int64_t)(uintptr_t)&M, ts_cur_prio(), 0, 0);
        pthread_mutex_unlock(&M);
    }
    return NULL;
}

static void *mrx(void *arg)
{
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 10);
    pthread_setname_np(pthread_self(), "mrx");
    for (;;) {
        ts_msg_t m;
        struct _msg_info info;
        int rcvid = MsgReceive(chid, &m, sizeof m, &info);
        if (rcvid <= 0) continue;
        ts_log("RX", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        if (m.type == TS_MSG_QUIT) { MsgReply(rcvid, 0, NULL, 0); break; }
        ts_log("WAIT", m.id, (int64_t)(uintptr_t)&M, ts_cur_prio(), 0, 0);
        pthread_mutex_lock(&M);
        ts_log("ACQUIRED", m.id, (int64_t)(uintptr_t)&M, ts_cur_prio(), 0, 0);
        ts_burn_us(200);
        pthread_mutex_unlock(&M);
        ts_reply_t r = { m.id, 0, (uint32_t)getpid(), (uint32_t)pthread_self() };
        ts_log("REPLY", m.id, rcvid, info.pid, info.tid, ts_cur_prio());
        MsgReply(rcvid, 0, &r, sizeof r);
    }
    return NULL;
}

static void *hogger(void *arg)
{
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 20);
    pthread_setname_np(pthread_self(), "hog");
    for (int k = 0; k < nper; k++) {
        at(k, hog_at);
        ts_log("BURN", k + 1, hog_us, 0, 0, 0);
        ts_burn_us(hog_us);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int opt;
    ts_role = "mutex";
    while ((opt = getopt(argc, argv, "n:p:w:d:gh:b:N")) != -1) {
        switch (opt) {
        case 'n': nper = atoi(optarg); break;
        case 'p': period_us = atoi(optarg); break;
        case 'w': work_us = atoi(optarg); break;
        case 'd': delay_us = atoi(optarg); break;
        case 'g': hog = 1; break;
        case 'h': hog_at = atoi(optarg); break;
        case 'b': hog_us = atoi(optarg); break;
        case 'N': no_inherit = 1; break;
        default: fprintf(stderr, "usage: ts_mutex [-n N] [-p US] [-w US] [-d US] [-g] [-h US] [-b US] [-N]\n"); return 2;
        }
    }
    if (no_inherit) {
        pthread_mutexattr_t ma;
        pthread_mutexattr_init(&ma);
        pthread_mutexattr_setprotocol(&ma, PTHREAD_PRIO_NONE);
        pthread_mutex_init(&M, &ma);
    }
    ts_pin_cpu0();
    ts_set_prio(SCHED_FIFO, 30);
    pthread_setname_np(pthread_self(), "hi");
    ts_calibrate();
    int spid, schid;
    if (ts_read_addr("/tmp/ts_mslow.addr", &spid, &schid)) { fprintf(stderr, "no ts_mslow\n"); return 1; }
    slow_coid = ConnectAttach(0, spid, schid, _NTO_SIDE_CHANNEL, 0);
    chid = ChannelCreate(0);
    int self = ConnectAttach(0, getpid(), chid, _NTO_SIDE_CHANNEL, 0);
    if (slow_coid == -1 || chid == -1 || self == -1) { perror("connect"); return 1; }
    clock_gettime(CLOCK_MONOTONIC, &t0);
    t0.tv_sec += 1;                                   /* start all threads on the same period grid */
    pthread_t th, tr, tg;
    pthread_create(&tr, NULL, mrx, NULL);
    pthread_create(&th, NULL, holder, NULL);
    if (hog) pthread_create(&tg, NULL, hogger, NULL);
    for (int k = 0; k < nper; k++) {
        at(k, delay_us);
        ts_msg_t m = { TS_MSG_REQ, 0, (uint32_t)(k + 1), 0, (uint32_t)pthread_self() };
        ts_reply_t r;
        uint64_t c0 = ClockCycles();
        int st = MsgSend(self, &m, sizeof m, &r, sizeof r);
        uint64_t c1 = ClockCycles();
        ts_log_at(c0, "SEND", k + 1, self, 30, 0, 0);
        ts_log_at(c1, "SEND_DONE", k + 1, self, st, r.server_pid, r.server_tid);
    }
    pthread_join(th, NULL);
    if (hog) pthread_join(tg, NULL);
    ts_msg_t q = { TS_MSG_QUIT, 0, 0, 0, 0 };
    MsgSend(self, &q, sizeof q, NULL, 0);
    MsgSend(slow_coid, &q, sizeof q, NULL, 0);
    pthread_join(tr, NULL);
    unsigned missed = 0;
    for (unsigned i = 0; i < ts_nrec && i < TS_MAX_REC; i++)
        if (!strcmp(ts_rec[i].ev, "SEND_DONE")) {
            for (unsigned j = 0; j < i; j++)
                if (!strcmp(ts_rec[j].ev, "SEND") && ts_rec[j].id == ts_rec[i].id &&
                    ts_rec[i].cycles - ts_rec[j].cycles > ts_us_to_cycles(4000)) { missed++; break; }
        }
    printf("hi: %u of %d requests took longer than 4000 us\n", missed, nper);
    ts_dump();
    return 0;
}
