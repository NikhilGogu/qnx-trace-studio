-- The slowest trustworthy requests: client thread, server, message and MsgSend latency.
-- Run on a trace written by `tracestudio convert`.
SELECT
  extract_arg(s.arg_set_id, 'debug.txn') AS txn,
  t.name AS client_thread,
  extract_arg(s.arg_set_id, 'debug.client_qnx_prio') AS client_prio,
  extract_arg(s.arg_set_id, 'debug.server') AS server,
  extract_arg(s.arg_set_id, 'debug.message') AS message,
  s.dur / 1000.0 AS latency_us
FROM slice s
JOIN thread_track tt ON s.track_id = tt.id
JOIN thread t USING (utid)
WHERE s.name LIKE 'MsgSend%'
  AND extract_arg(s.arg_set_id, 'debug.trustworthy') = 1
ORDER BY s.dur DESC
LIMIT 20;
