# Production deployment and capacity validation

## Deployment topology

Run the same image as separate Kubernetes deployments behind a TLS API gateway. The gateway owns account and IP quotas, upload and JSON body limits, HTTP timeouts, bot controls, and overload rejection. Keep Prometheus, health probes, and database endpoints on private networks. Use one Uvicorn process per API pod with a concurrency limit and scale out. Run the migration as a release job with a dedicated database role before deploying compatible API and worker versions. Grant application database roles only the DML privileges required by their lane.

Deploy workers in separate resource pools using these commands:

| Pool | Command |
| --- | --- |
| Chat | `python -m annotation.worker --kind chat` |
| Audio | `python -m annotation.worker --kind audio` |
| Video | `python -m annotation.worker --kind video` |
| Text and legacy extraction | `python -m annotation.worker --kind index --kind image --kind document` |
| Orchestration | `python -m annotation.worker --kind workflow` |

Use API CPU, latency and rejection metrics for API scaling. Export oldest queued age, ready depth, leased depth and completion rate by kind for worker scaling. Bound replica counts by provider quotas and Postgres connection budget. Configure worker termination grace of at least 165 seconds. Run media pods with explicit CPU, memory, ephemeral storage, process and network egress limits. Isolate the FFmpeg pods from API processes and restrict worker egress to intended providers, adapters, storage and the database.

Use a private versioned S3 bucket with encryption, block public access, and IAM workload identity. S3 receives uploads directly and serves playback. Enable CORS only for approved frontend origins and expose the headers needed for playback. Apply retention policies that preserve pinned versions for the lifetime of their asset. Monitor storage left orphaned by abandoned or overwritten uploads.

Use multi-zone managed PostgreSQL, point-in-time recovery, periodic restore drills, TLS, query monitoring, connection pooling and failover testing. Set the summed process pool maxima below the configured connection budget. Add deletion and retention workflows before ingesting data governed by deletion requirements. Monitor query plans, GIN size, autovacuum, queue contention and WAL rates. Chat data and reference excerpts are retained in PostgreSQL until a retention policy is implemented.

The included deployment is a single PostgreSQL cluster. A larger topology would route tenants to cells with separate database, worker and provider quota budgets. An oversized tenant may need a dedicated cell and an external search or queue backend behind the same API. Cell routing, automated failover, infrastructure provisioning, and federated capacity management are outside the supplied implementation.

## Model the target

Define what “one million at the same time” means in terms of active sessions, uploads, requests and latencies. The following numbers are workload assumptions for planning:

| Assumption | Result |
| --- | --- |
| One million active sessions, one turn every 120 seconds | About 8,333 chat submissions per second |
| One million active sessions, one annotation every 5 minutes | About 3,333 annotation writes per second |
| 8,333 chat turns per second, 5 seconds to complete | About 41,665 chat jobs in flight |
| Poll every 2 seconds for each job in flight | About 20,833 status reads per second |
| 8,333 generated turns per second at 1,200 total tokens per turn | About 600 million tokens per minute |

Measure the actual input and output token volumes, file-size and duration distributions, project skew, and upstream latency. Running one million active sessions differs from issuing one million requests in the same instant. For the latter, define an admission window and acceptable rejection or retry behavior. At high concurrency, status notifications or adaptive polling can reduce database reads. The API currently uses polling and persistent HTTP connections can terminate at the edge.

## Validate in staging

1. Seed realistic projects with representative index sizes and provision enough test identities to exercise tenant routing and isolation. Begin with deterministic providers, then repeat with budgeted real upstream quotas.
2. Run `deploy/load.js` with k6 at a small constant arrival rate. Scale generators independently of the service. The included script exercises submissions and polling. Extend the workload with representative annotation writes, uploads, and a realistic distribution of tenants and asset sizes before using results for capacity sign-off.
3. Track end-to-end p95/p99, accepted versus completed work, queue age, dropped iterations, 429/503 rates, SQL saturation, storage latency and provider throttling. Suggested initial acceptance gates are 99% successful chat completion, p95 turn latency below 10 seconds and no dropped k6 iterations at the agreed steady-state workload.
4. Test rolling restarts, provider throttling, database failover, expired worker leases, burst recovery, and restore from backup. Run a soak that covers a full operational cycle. Sign off only on capacity actually measured for that scenario.
