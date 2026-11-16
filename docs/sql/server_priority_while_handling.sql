-- The QNX priority each server thread ran at while it handled each client's request.
-- With priority inheritance the server runs at the client's priority; with a
-- fixed-priority channel it keeps its own.
SELECT
  ct.name AS client_thread,
  st.name AS server_thread,
  sc.priority AS server_prio,
  SUM(MIN(sc.ts + sc.dur, h.ts + h.dur) - MAX(sc.ts, h.ts)) / 1000.0 AS running_us
FROM slice h
JOIN thread_track ht ON h.track_id = ht.id
JOIN thread st ON st.utid = ht.utid
JOIN flow f ON f.slice_in = h.id
JOIN slice s ON s.id = f.slice_out
JOIN thread_track ctt ON s.track_id = ctt.id
JOIN thread ct ON ct.utid = ctt.utid
JOIN sched sc ON sc.utid = st.utid AND sc.ts < h.ts + h.dur AND sc.ts + sc.dur > h.ts
WHERE h.name LIKE 'handle %'
  AND s.name LIKE 'MsgSend%'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
