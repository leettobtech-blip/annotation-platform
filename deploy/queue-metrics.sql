-- Run through an internal database metrics exporter using a read-only role.
SELECT kind,
       count(*) FILTER (WHERE status='queued' AND available<=now()) AS ready,
       count(*) FILTER (WHERE status='queued' AND available>now()) AS delayed,
       count(*) FILTER (WHERE status='running' AND available>now()) AS leased,
       count(*) FILTER (WHERE status='running' AND available<=now()) AS expired_leases,
       COALESCE(extract(epoch FROM now()-min(available) FILTER
           (WHERE status='queued' AND available<=now())),0) AS oldest_ready_seconds
FROM jobs
WHERE status IN ('queued','running')
GROUP BY kind;
