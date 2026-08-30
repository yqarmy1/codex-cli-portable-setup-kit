## Long-running asynchronous tools

- For `functions.exec` work expected to exceed 10 seconds, use the first line `// @exec: {"yield_time_ms": 60000}` unless a higher-priority or tool-specific rule differs.
- Continue a yielded cell with the continuation tool in the current schema (`functions.wait` here). Empty/status-only polls use `yield_time_ms: 60000`; never poll every 1–10 seconds, and stop on completion without reporting “still running.”
- Let the outer `exec` outlast nested waits where runtime limits allow; never shorten the task timeout. Long waits apply only to empty polls—send interactive input, termination, and non-empty legacy `write_stdin` calls immediately.
- Bound model-visible output: prefer `rg`, filters, aggregates, and log tails; never print unbounded recursive listings or full logs/JSON. If output may exceed 200 lines or 32 KiB, write it to a file and return only its path plus a concise summary.
- Before wide or costly commands, verify the working directory, target paths, and required tools, then probe narrowly. After a failure, do not rerun the identical command unchanged; revise the assumption or parameters first.
