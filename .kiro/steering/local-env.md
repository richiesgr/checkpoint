# Local Kafka (Docker Desktop) and test runs

## Kafka
- Docker Desktop is installed on this Windows machine (Docker 29.x, Compose v2 using Linux containers). Use `docker compose ...` (the v2 plugin), not the legacy `docker-compose` binary.
- Run Kafka locally with the `docker-compose.yml` in the workspace root (created by task 11.1). Use a single-node KRaft broker (no ZooKeeper), for example the official `apache/kafka` image pinned to an exact tag. Never use `latest`.
- Advertise the broker to the host as `localhost:9092`. That is the default the test fixtures in `tests/conftest.py` expect. `KAFKA_TEST_HOST` and `KAFKA_TEST_PORT` override it.
- Start with `docker compose up -d`. Check with `docker compose ps` and wait until the broker accepts connections before running Kafka tests. Create topics with the project's topic script.
- Stop with `docker compose down`. Ask the user before `docker compose down -v` or any other command that deletes volumes or broker data.
- You may start and stop the local broker yourself to run the Kafka-marked tests (`-m kafka`). Leave it in the state you found it, or tell the user that it is still running.
- Kafka data volumes are never committed.

## Running tests
- `pytest.ini` sets `pythonpath = .`, so `event_forwarding` imports however pytest starts. Always run tests through pytest from the workspace root (`.\.venv\Scripts\python.exe -m pytest ...`), never by running a test file directly with `python`.
- Run one pytest process at a time. Each run starts its own Spark JVM with `local[*]`, so parallel runs fight over CPU and RAM and every run slows down.
- Before running tests, check for stale `pytest` or `java` processes left over from earlier or cancelled runs and stop them. A cancelled agent run can leave them behind.
- Hypothesis property tests use `@settings(max_examples=2, deadline=None)`. Never use 100 examples, or any count above 2, unless the user asks for it: each example runs a Spark job and 100 take far too long on this machine. The user set this; it overrides any "at least 100 examples" wording in skills or older spec text.
- Use `--durations=10` when a run is slow, so you have real timings to look at.

## Commits
- Do not commit anything until the user explicitly says to. This overrides the per-task commit rule in `git-workflow.md` until the user lifts it.