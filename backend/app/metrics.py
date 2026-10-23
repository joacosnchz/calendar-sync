"""Prometheus metrics. Labelled by tenant so alerts can fire per tenant."""

from prometheus_client import Counter, Histogram

NOTIFICATIONS_RECEIVED = Counter(
    "calsync_notifications_received_total", "Change notifications accepted", ["tenant"]
)
NOTIFICATIONS_REJECTED = Counter(
    "calsync_notifications_rejected_total", "Change notifications rejected", ["reason"]
)
LIFECYCLE_RECEIVED = Counter(
    "calsync_lifecycle_notifications_total", "Lifecycle notifications accepted", ["tenant", "event"]
)
WEBHOOK_LATENCY = Histogram("calsync_webhook_seconds", "Time to acknowledge a webhook delivery", ["endpoint"])
FETCH_ERRORS = Counter("calsync_graph_fetch_errors_total", "Failed Graph reads", ["tenant", "kind"])
THROTTLED = Counter("calsync_graph_throttled_total", "Graph 429/503 responses", ["scope_kind"])
RENEWAL_FAILURES = Counter(
    "calsync_subscription_renewal_failures_total", "Subscription renewals that failed", ["tenant"]
)
SUBSCRIPTIONS_RECREATED = Counter(
    "calsync_subscriptions_recreated_total", "Subscriptions recreated after loss", ["tenant"]
)
DELTA_DRIFT = Counter(
    "calsync_delta_drift_total",
    "Changes found by a scheduled/recovery delta that notifications had not delivered",
    ["tenant"],
)
MIRROR_WRITES = Counter("calsync_mirror_writes_total", "Writes to primary calendars", ["op"])
JOB_FAILURES = Counter("calsync_job_failures_total", "Worker jobs that failed", ["job"])
