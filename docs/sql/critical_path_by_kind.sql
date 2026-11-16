-- Where the time of each client's requests went, by critical-path segment kind
-- (client running, server preempted, server busy, nested request, ...).
-- Only top-level segments of each request are counted, so nothing is counted twice.
SELECT
  REPLACE(tr.name, ' critical path', '') AS client_thread,
  extract_arg(seg.arg_set_id, 'debug.kind') AS kind,
  COUNT(*) AS segments,
  SUM(seg.dur) / 1000.0 AS total_us
FROM slice req
JOIN track tr ON req.track_id = tr.id
JOIN slice seg ON seg.parent_id = req.id
WHERE tr.name LIKE '% critical path'
  AND req.depth = 0
GROUP BY 1, 2
ORDER BY client_thread, total_us DESC;
