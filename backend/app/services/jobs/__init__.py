"""Asynchronous background job queue.

NexusFlow runs heavy enterprise work -- re-parsing a 500-page audit, embedding
its chunks, running a Nemotron completion -- away from the request path, so an
upload returns immediately and the caller polls for the outcome.

The queue lives in the ``background_jobs`` table, which makes it durable across
restarts and observable while work is in flight. See :mod:`.worker` for the
dispatcher and :mod:`.handlers` for the workloads.
"""

from app.services.jobs.handlers import (
    HANDLERS,
    JobCancelledError,
    JobHandlerError,
    run_document_extraction,
    run_embedding,
    run_llm_task,
)
from app.services.jobs.store import (
    TERMINAL_STATES,
    count_jobs,
    create_job,
    get_job,
    reap_stale_jobs,
    request_cancel,
    trim_logs,
)
from app.services.jobs.telemetry import JobTelemetry, flush, record_cancelled
from app.services.jobs.worker import JobWorker, WorkerStats, claim_next_job

__all__ = [
    "HANDLERS",
    "TERMINAL_STATES",
    "JobCancelledError",
    "JobHandlerError",
    "JobTelemetry",
    "JobWorker",
    "WorkerStats",
    "claim_next_job",
    "count_jobs",
    "create_job",
    "flush",
    "get_job",
    "reap_stale_jobs",
    "record_cancelled",
    "request_cancel",
    "run_document_extraction",
    "run_embedding",
    "run_llm_task",
    "trim_logs",
]
