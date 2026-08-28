# SLOs
ADAPT. Each objective has an indicator, a target, a window, and an alert tied to burn rate.
| SLO | Indicator | Target | Window | Alert |
|---|---|---|---|---|
| Availability | successful non-5xx ratio | 99.5% | 30d | burn rate > 2x |
| Latency | p95 of request duration | < ADAPT s | 30d | p95 over target 15 min |
Error budget policy: when the budget is spent, reliability work preempts features until it recovers.
