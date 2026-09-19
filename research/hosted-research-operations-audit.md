# Hosted Research Operations Audit

Date: 2026-09-19

Scope: the broker-free historical-research service on Render, its durable
Cloudflare R2 artifact store, and the failure boundaries between them. This
audit does not authorize trading and does not change strategy parameters.

## Verified production observations

- Render reports a 536,870,912-byte service memory limit.
- The Phase 002 streamed run completed on its first attempt, but one progress
  sample reached 536,739,840 bytes (99.98%). A successful run at that level is
  not evidence of safe memory headroom.
- The worker's maximum RSS was 386,338,816 bytes. The difference between worker
  RSS and whole-service memory exposed a large always-resident coordinator and
  Linux file-cache contribution.
- The coordinator took 103.791 seconds to restore durable state before binding
  its health route.
- September Render-to-R2 uploads totalled 669,934,703 tracked bytes at audit
  time: 496,365,735 dataset, 172,319,051 result, 784,956 status, 325,769
  checkpoint, and 139,192 job bytes.
- There were no active or queued jobs at audit time.

## Defects corrected in this change

1. **Coordinator imported the replay engine.** `research.app` imported helpers
   from `research.worker` and `research.historical_replay`, transitively loading
   all Tier 1 strategy and diagnostic modules into the API process. The helpers
   now live in dependency-free `research.runtime_io`. An import-isolation check
   confirms the coordinator no longer loads the worker, replay, or Tier 1
   modules.
2. **No pre-OOM boundary.** Workers now recheck whole-cgroup memory after
   garbage collection and stop with `ResearchMemoryLimitExceeded` at a
   configurable ceiling (90% by default). This error is deterministic and does
   not enter a wasteful automatic retry loop.
3. **Durable recovery blocked deployment health.** R2 restoration now begins in
   a background thread after the service binds. Health remains constant-time;
   mutating endpoints return 503 with `Retry-After` until queue recovery is
   complete. Recovery errors are visible in health diagnostics.
4. **Transfer circuit breaker consumed the entire allowance.** The default
   research upload budget is reduced from 5 GiB to 1 GiB. Coordinator and
   worker reservations are serialized with a cross-process lock so concurrent
   status/checkpoint uploads cannot undercount the ledger. The Render Hobby
   bandwidth allowance is shared across the workspace, so this is a project
   safety ceiling rather than a guarantee about the final invoice.
5. **Idle memory was opaque.** Authenticated runtime diagnostics now expose
   whole-cgroup current/limit/percentage data and Linux memory-event counters.

## Existing controls verified

- Research runs in a subprocess and only one job runs at a time.
- Source datasets, job definitions, status, checkpoints, and final archives are
  durable in R2; Render's local filesystem is treated as an ephemeral cache.
- Completed downloads redirect to signed R2 URLs rather than proxying archive
  bytes through Render.
- Immutable datasets and results use upload-if-missing behavior.
- Local datasets, expanded output, staging files, and archives are removed after
  durable finalization.
- Checkpoints are incremental, identity-bound, checksum-validated, capped by a
  separate transfer budget, and deleted after successful durable finalization.
- Restart retries require durable progress and have consecutive and lifetime
  ceilings. Deterministic input, storage-budget, and memory-limit failures do
  not loop automatically.
- Failed jobs preserve status/audit evidence and block later jobs until an
  explicit retry, supersession, or queue-resume decision.
- Result ZIP creation streams large CSV members and uses ZIP64.
- R2 storage is capped at 8 GiB, below the 10 GB-month Standard free allowance.

## Free-plan boundaries and residual risks

- Render can restart a Free service at any time and its filesystem is
  ephemeral. Correctness therefore depends on R2 durability, not uptime.
- Uptime pings keep the service running and consume the workspace's 750 monthly
  Free instance hours. Multiple continuously running Free services can exhaust
  that shared allowance.
- Render Hobby currently includes 5 GB of shared monthly outbound bandwidth.
  Uploading to R2 is outbound from Render even though R2 itself charges no
  egress. The 1 GiB application breaker leaves 80% headroom but cannot account for
  other services in the workspace; Render's Billing page remains authoritative.
- R2 Standard includes 10 GB-month storage, 1 million Class A operations, and
  10 million Class B operations monthly. Current request volume is far below
  the operation limits. Do not switch the bucket to Infrequent Access because
  its free-tier and retrieval economics differ.
- The upload ledger is deliberately conservative but is application-level
  accounting. It is not a substitute for provider billing alerts.
- A memory guard can preserve the service and diagnostic status, but it cannot
  make an arbitrarily large experiment fit. Jobs that hit it must be sharded or
  their specific diagnostic stage made streaming before retry.

## Gate before the next research phase

Do not start another large phase until all of the following are true on the
deployed commit:

1. Health binds promptly and reports `coordinator_ready=true` after background
   recovery.
2. Idle whole-service memory is recorded.
3. An exact Phase 002-equivalent canary completes on attempt one below 85%
   whole-service memory, leaving at least 15% headroom.
4. `service_memory_events_oom_kill` does not increase during the canary.
5. The result archive is durable, checksum-valid, and downloaded through an R2
   redirect.
6. The R2 upload ledger remains below 1 GiB and Render's workspace Billing page
   shows enough shared bandwidth for the estimated canary upload.
7. A restart/redeploy drill during a small checkpointable job restores queue
   state without duplicate jobs or loss of the durable audit record.

Passing unit tests is necessary but not sufficient. The 512 MiB cgroup and
provider transfer accounting can only be validated by one controlled deployed
canary; strategy research remains paused until that gate passes.
