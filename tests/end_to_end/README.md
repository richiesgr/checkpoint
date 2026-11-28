# End-to-end test (task 13)

Everything for the end-to-end run lives in this directory:

| File | Purpose |
|---|---|
| `settings.py` | Counts, seed, tenants, topic names, run directory layout |
| `generator.py` | Writes one run directory: config, landing-zone files, `expected.json` |
| `kafka_helpers.py` | Test-side topic inspection with kafka-python |
| `test_end_to_end_kafka.py` | The test (marked `kafka`) |
| `run-end-to-end.ps1` | Start script: broker, topics, test |

## Flow

```
generator -> landing zone (5 JSONL files)
  -> processing job (run_processing)  -> topic delivery.file
  -> File_Sink_Worker (run_worker, sink_transform) -> out/<tenant_id>/<destination_id>/*.jsonl
```

Topics are shared per destination kind (`delivery.file`, `delivery.file.dlq`), never per tenant.
Only the file destination is exercised.

## Run

```powershell
# Full run: starts the broker if needed, creates the topics, runs the test
.\tests\end_to_end\run-end-to-end.ps1

# Only generate a run directory (no broker, no Spark), to look at the data
.\tests\end_to_end\run-end-to-end.ps1 -GenerateOnly
```

The broker is left running. Stop it with `docker compose down`.

## Where the results go

Each run writes to a new directory under the system temp directory (the Windows equivalent of
`/tmp`): `%TEMP%\e2e-<run_id>`, for example `C:\Users\<you>\AppData\Local\Temp\e2e-718294a5`.
Set `E2E_RUN_ROOT` to use another root. The run id is printed in the test log.

```
config.yaml        generated config (10 tenants)
expected.json      expected counts per destination and rejected reasons
landing\           events-1.jsonl .. events-5.jsonl
ckpt-processing\   processing checkpoint
ckpt-sink\         sink checkpoint
out\               customer files: out\<tenant_id>\<destination_id>\*.jsonl
audit\             processing and delivery audit, run_summary, worker_summary
rejected\          rejected lines with their reason
```

The directory is outside the repository because Spark's staging paths under it come close to the
Windows 260-character path limit (long paths are off by default). Run directories are not
deleted automatically. Because of the long staging paths, delete one with
`cmd /c rd /s /q "\\?\<full path>"` if `Remove-Item` fails.

## Generated data

1,170 lines per run, shuffled with a fixed seed and split across 5 files:

- **1,000 matched events.** Valid and matched by at least one enabled rule, so they are delivered.
- **150 unmatched events.** Valid, but no enabled rule matches, so they are dropped as `no_matching_rule`.
- **20 bad lines.** Malformed JSON, an empty or non-UUID `tenant_id`, a missing envelope field, or a non-object `data`. They are rejected with their reason.

Tenants are 10 fixed UUIDs (uuid5 from the tenant index), the same in every run. Products and
schemas are the fixed set from the assignment: `sales/purchase`, `security/alert`, `identity/login`.

| Tenant | Rules |
|---|---|
| T0 to T3 | `sales/purchase` keeps only `order_id` and `amount` (drops `internal_note`). `security/alert` adds the constant `region: "eu"`. Both go to `dest-tN-file`. |
| T4, T5 | `sales/purchase`, all fields, to `dest-tN-file` |
| T6 | `sales/purchase` fans out to `dest-t6-file-a` and `dest-t6-file-b` |
| T7 | `sales/purchase` rule with `enabled: false`. `dest-t7-file` is never written. |
| T8 | No rule. `dest-t8-file` is never written. |
| T9 | No rule, no destination |

No tenant forwards `identity/login`.

Matched events go round-robin over the 11 enabled (tenant, product, schema) keys, and unmatched
events go round-robin over the other keys. The expected output therefore follows from the counts:

| Destination | Records |
|---|---|
| `dest-t0-file` .. `dest-t3-file` | 182 each |
| `dest-t4-file`, `dest-t5-file` | 91 each |
| `dest-t6-file-a`, `dest-t6-file-b` | 90 each (the same 90 events, fan-out) |
| **Total** | **1,090** (1,000 events + 90 fan-out copies) |

## What the test checks

- The generated config passes validation.
- **Processing job:**
  - It publishes exactly 1,090 messages to `delivery.file`, with the expected count per key `<tenant_id>|<destination_id>`.
  - The Run_Summary shows 1,170 read, 20 rejected and 1,090 published.
- **Unmatched events:** every one of them is in the processing audit as `no_matching_rule`.
- **Bad lines:** every one of them is in the Rejected_Output with its expected reason.
- **File_Sink_Worker output:**
  - Each destination's files hold exactly the expected lines, with the configured shaping.
  - Every line in a directory belongs to that directory's tenant.
  - No staging files are left behind.
- **Never delivered:**
  - `dest-t7-file` and `dest-t8-file` are never written.
  - `internal_note` never appears for T0 to T3.
  - No unmatched event and no bad line reaches any file.
- The Worker_Summary shows 1,090 delivered and 0 dead-lettered.
- Nothing from this run reaches `delivery.file.dlq`.

The topics keep messages from earlier runs. The test records the topic end offsets before the
run, reads only what was written after them, and starts the worker at those offsets
(`run_worker(..., starting_offsets=...)`).
