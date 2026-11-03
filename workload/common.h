/*
 * Copyright 2026 The trace-studio authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shared helpers for the trace-studio IPC workload: message layout,
 * ground-truth event log, calibrated CPU burn, priority and CPU pinning.
 *
 * Every process records what it did (request id, pid/tid, chid/coid/rcvid,
 * ClockCycles()) into an in-memory log and writes it as CSV at exit, so the
 * decoder's message-passing links can be checked against ground truth
 * without the logging itself adding I/O to the trace window.
 */
#ifndef TS_COMMON_H
#define TS_COMMON_H

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>

/* First word of every request: type in the low 16 bits, like an io message
 * header, so msg[0] in a trace identifies the protocol. The request id is
 * the second word (visible as msg[1] in wide-mode traces only). */
#define TS_MSG_REQ   0x7101u  /* client -> front, or front -> back (forward) */
#define TS_MSG_PRIO  0x7102u  /* priority scenario request */
#define TS_MSG_QUIT  0x71ffu

#define TS_F_FORWARD 0x0001u  /* front must forward this request to back */

#define TS_PULSE_STATS (_PULSE_CODE_MINAVAIL + 1)  /* client -> front */
#define TS_PULSE_TICK  (_PULSE_CODE_MINAVAIL + 2)  /* back's own timer */

typedef struct {
    uint16_t type;
    uint16_t flags;
    uint32_t id;
    uint32_t work_us;   /* CPU work the receiving server performs */
    uint32_t origin_tid;
} ts_msg_t;

typedef struct {
    uint32_t id;
    int32_t  status;
    uint32_t server_pid;
    uint32_t server_tid;
} ts_reply_t;

/* ---- ground-truth log ---------------------------------------------------- */

typedef struct {
    uint64_t cycles;
    const char *ev;
    uint32_t id;
    int32_t tid;
    int64_t a, b, c, d;
} ts_rec_t;

#define TS_MAX_REC 65536
static ts_rec_t ts_rec[TS_MAX_REC];
static unsigned ts_nrec;
static const char *ts_role = "?";

static inline void ts_log(const char *ev, uint32_t id, int64_t a, int64_t b, int64_t c, int64_t d)
{
    uint64_t now = ClockCycles();
    unsigned i = __atomic_fetch_add(&ts_nrec, 1u, __ATOMIC_RELAXED);
    if (i < TS_MAX_REC) {
        ts_rec_t *r = &ts_rec[i];
        r->cycles = now; r->ev = ev; r->id = id; r->tid = (int32_t)pthread_self();
        r->a = a; r->b = b; r->c = c; r->d = d;
    }
}

/* Same as ts_log but with a caller-supplied timestamp. */
static inline void ts_log_at(uint64_t cyc, const char *ev, uint32_t id, int64_t a, int64_t b, int64_t c, int64_t d)
{
    unsigned i = __atomic_fetch_add(&ts_nrec, 1u, __ATOMIC_RELAXED);
    if (i < TS_MAX_REC) {
        ts_rec_t *r = &ts_rec[i];
        r->cycles = cyc; r->ev = ev; r->id = id; r->tid = (int32_t)pthread_self();
        r->a = a; r->b = b; r->c = c; r->d = d;
    }
}

static void ts_dump(void)
{
    char path[128];
    snprintf(path, sizeof path, "/tmp/ts_logs/%s-%d.csv", ts_role, getpid());
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); return; }
    unsigned n = ts_nrec < TS_MAX_REC ? ts_nrec : TS_MAX_REC;
    fprintf(f, "# role=%s pid=%d cycles_per_sec=%llu nrec=%u\n", ts_role, getpid(),
            (unsigned long long)SYSPAGE_ENTRY(qtime)->cycles_per_sec, n);
    fprintf(f, "cycles,role,pid,tid,ev,id,a,b,c,d\n");
    for (unsigned i = 0; i < n; i++) {
        ts_rec_t *r = &ts_rec[i];
        fprintf(f, "%llu,%s,%d,%d,%s,%u,%lld,%lld,%lld,%lld\n",
                (unsigned long long)r->cycles, ts_role, getpid(), r->tid, r->ev, r->id,
                (long long)r->a, (long long)r->b, (long long)r->c, (long long)r->d);
    }
    fclose(f);
}

/* ---- calibrated CPU work ------------------------------------------------- */

static double ts_loops_per_us;

static inline void ts_spin(uint64_t n)
{
    volatile uint64_t x = 0;
    for (uint64_t i = 0; i < n; i++) x += i;
}

/* Calibrate a loop count, so "work" is CPU consumed by the thread and is not
 * shortened by preemption (a wall-clock spin would finish early). */
static void ts_calibrate(void)
{
    const uint64_t cps = SYSPAGE_ENTRY(qtime)->cycles_per_sec;
    const uint64_t n = 2000000;
    uint64_t best = UINT64_MAX;
    for (int k = 0; k < 5; k++) {
        uint64_t t0 = ClockCycles();
        ts_spin(n);
        uint64_t dt = ClockCycles() - t0;
        if (dt < best) best = dt;
    }
    ts_loops_per_us = (double)n / ((double)best * 1e6 / (double)cps);
}

static inline void ts_burn_us(uint32_t us)
{
    ts_spin((uint64_t)(ts_loops_per_us * us));
}

static inline uint64_t ts_us_to_cycles(uint64_t us)
{
    return us * SYSPAGE_ENTRY(qtime)->cycles_per_sec / 1000000u;
}

/* ---- scheduling helpers -------------------------------------------------- */

static inline void ts_pin_cpu0(void)
{
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)(uintptr_t)0x1) == -1)
        perror("ThreadCtl(_NTO_TCTL_RUNMASK)");
}

static inline void ts_set_prio(int policy, int prio)
{
    struct sched_param sp = { .sched_priority = prio };
    int rc = pthread_setschedparam(pthread_self(), policy, &sp);
    if (rc) fprintf(stderr, "pthread_setschedparam(%d): %s\n", prio, strerror(rc));
}

static inline int ts_cur_prio(void)
{
    struct sched_param sp; int pol;
    pthread_getschedparam(pthread_self(), &pol, &sp);
    return sp.sched_curpriority;
}

static int ts_read_addr(const char *path, int *pid, int *chid)
{
    for (int i = 0; i < 200; i++) {
        FILE *f = fopen(path, "r");
        if (f) {
            int n = fscanf(f, "%d %d", pid, chid);
            fclose(f);
            if (n == 2) return 0;
        }
        usleep(10000);
    }
    return -1;
}

static void ts_write_addr(const char *path, int chid)
{
    char tmp[128];
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    if (!f) { perror(tmp); exit(1); }
    fprintf(f, "%d %d\n", getpid(), chid);
    fclose(f);
    rename(tmp, path);
}

#endif
