#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Load converted traces in Perfetto TraceProcessor and run the analysis SQL.

    python3 tools/tp_queries.py out.perfetto-trace [more traces...]
"""
import sys

from perfetto.trace_processor import TraceProcessor

TABLES = """
SELECT 'sched' AS t, COUNT(*) AS n FROM sched UNION ALL
SELECT 'thread_state', COUNT(*) FROM thread_state UNION ALL
SELECT 'slice', COUNT(*) FROM slice UNION ALL
SELECT 'flow', COUNT(*) FROM flow UNION ALL
SELECT 'counter', COUNT(*) FROM counter UNION ALL
SELECT 'thread', COUNT(*) FROM thread UNION ALL
SELECT 'process', COUNT(*) FROM process UNION ALL
SELECT 'thread_state waker known', COUNT(*) FROM thread_state WHERE waker_utid IS NOT NULL
"""

# Q1: end-to-end latency of every request, client MsgSend slice to its reply, by client thread and server
LATENCY = """
SELECT t.name AS client, p.name AS client_proc,
       REPLACE(s.name, 'MsgSend -> ', '') AS server,
       COUNT(*) AS n, ROUND(MIN(s.dur) / 1e3, 1) AS min_us, ROUND(AVG(s.dur) / 1e3, 1) AS avg_us,
       ROUND(MAX(s.dur) / 1e3, 1) AS max_us,
       SUM(s.dur > 5e6) AS over_5ms
FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t USING (utid) LEFT JOIN process p USING (upid)
WHERE s.name LIKE 'MsgSend -> ts_%'
GROUP BY 1, 2, 3 ORDER BY max_us DESC
"""

# Q2: blocking chains. For each request, the server thread that handled it (flow send -> handle),
# how long that server sat Runnable inside the request window, and which threads ran instead.
CHAIN = """
WITH req AS (
  SELECT s.id, s.ts, s.dur, t.name AS client, t.utid AS cutid
  FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t USING (utid)
  WHERE s.name LIKE 'MsgSend -> %'),
handled AS (
  SELECT req.*, h.ts AS h_ts, ht.utid AS sutid, st.name AS server
  FROM req JOIN flow f ON f.slice_out = req.id JOIN slice h ON h.id = f.slice_in
  JOIN thread_track ht ON h.track_id = ht.id JOIN thread st ON st.utid = ht.utid),
ready AS (
  SELECT handled.id, MAX(ts.ts, handled.ts) AS r0, MIN(ts.ts + ts.dur, handled.ts + handled.dur) AS r1
  FROM handled JOIN thread_state ts ON ts.utid = handled.sutid
  WHERE ts.state IN ('R', 'R+') AND ts.ts < handled.ts + handled.dur AND ts.ts + ts.dur > handled.ts),
culprit AS (
  SELECT ready.id, sc.utid, sc.priority,
         SUM(MIN(sc.ts + sc.dur, ready.r1) - MAX(sc.ts, ready.r0)) AS ran
  FROM ready JOIN sched sc ON sc.ts < ready.r1 AND sc.ts + sc.dur > ready.r0 AND sc.utid != 0
  GROUP BY 1, 2, 3)
, ranked AS (SELECT handled.*, ROW_NUMBER() OVER (PARTITION BY client ORDER BY dur DESC) AS rk FROM handled)
SELECT h.client, h.server, ROUND(h.dur / 1e3, 1) AS latency_us,
       ROUND((SELECT SUM(r1 - r0) FROM ready WHERE ready.id = h.id) / 1e3, 1) AS server_runnable_us,
       (SELECT t.name || ' prio ' || c.priority || ' ran ' || ROUND(c.ran / 1e3, 1) || ' us'
          FROM culprit c JOIN thread t USING (utid) WHERE c.id = h.id AND c.utid NOT IN (h.sutid, h.cutid)
          ORDER BY c.ran DESC LIMIT 1) AS ran_instead
FROM ranked h
WHERE h.rk = 1 AND h.client IN ('hi', 'lo', 'client-0', 'client-1', 'client-2', 'client-3', 'front-rx0', 'front-rx1')
ORDER BY h.dur DESC
"""

# Q3: server priority while handling each client's request (inheritance vs fixed priority)
SERVER_PRIO = """
SELECT ct.name AS client, st.name AS server, sc.priority AS server_prio_running, COUNT(*) AS sched_slices
FROM slice h JOIN thread_track ht ON h.track_id = ht.id JOIN thread st ON st.utid = ht.utid
JOIN flow f ON f.slice_in = h.id JOIN slice s ON s.id = f.slice_out
JOIN thread_track ctt ON s.track_id = ctt.id JOIN thread ct ON ct.utid = ctt.utid
JOIN sched sc ON sc.utid = st.utid AND sc.ts < h.ts + h.dur AND sc.ts + sc.dur > h.ts
WHERE h.name LIKE 'handle %' AND st.name = 'psrv-rx'
GROUP BY 1, 2, 3 ORDER BY 1, 3
"""


def show(tp, title, q):
    rows = list(tp.query(q))
    print("--", title)
    if not rows:
        print("   (no rows)")
        return
    cols = list(rows[0].__dict__.keys())
    print("   " + " | ".join(cols))
    for r in rows:
        print("   " + " | ".join(str(getattr(r, c)) for c in cols))


for path in sys.argv[1:]:
    print("==", path)
    tp = TraceProcessor(trace=path)
    show(tp, "table population", TABLES)
    show(tp, "Q1 per-request end-to-end latency (MsgSend slices)", LATENCY)
    show(tp, "Q2 longest blocking chains", CHAIN)
    if "prio" in path:
        show(tp, "Q3 server priority while handling each client", SERVER_PRIO)
    tp.close()
