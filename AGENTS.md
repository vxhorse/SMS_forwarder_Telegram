# AGENTS.md

Instructions for coding agents working in this repository. This is the single
authoritative file — `CLAUDE.md` imports it rather than repeating it.

## What this is

A service that forwards SMS from a serial GSM modem to Telegram, and sends SMS
back out from Telegram. It runs in a container on small always-on boxes, x86 and
ARM, and the messages it carries are one-time verification codes and bank
notifications.

That last point drives most of the rules below. A message that is lost,
duplicated into uselessness, misdated, or leaked into a log is a real failure,
not a cosmetic one.

## Commands

```bash
uv venv                                                            # .venv/ is gitignored: a fresh clone has none
uv pip install --python .venv/bin/python -r requirements-dev.txt   # setup (no pip in this venv)
.venv/bin/python -m pytest                                          # run the suite
.venv/bin/python -m pytest tests/test_supervisor.py -v              # one file
```

Never invoke bare `pytest` or `python3` — the venv is uv-managed and the
interpreter must be the one in `.venv/`.

## Architecture

`main.py` builds four components — `HealthState`, `Supervisor`, `DeviceManager`
and `TelegramBot` — wires them to each other and then gets out of the way. The
last module below is reached through one of them, not from `main.py`:

- **`module/supervisor.py`** — `Supervisor` owns every component's lifecycle.
  It enforces a three-way split that the rest of the design depends on:
  a dependency that is not ready yet is retried forever and never terminates the
  process; something that broke while running is reconnected, also without
  terminating; only a `FatalConfigError` or the watchdog ends the process, and
  then deliberately, so the container restart policy takes over.
- **`module/health.py`** — `HealthState` is the single source of truth for
  component status, and writes the JSON snapshot that the container healthcheck
  reads. It is the only channel across that process boundary.
- **`module/device_manager.py`** and **`module/telegram_bot.py`** — the two
  `ManagedService` implementations. They talk to each other through callbacks
  passed in by `main.py`, not by importing each other.
- **`module/discovery.py`** — finds the modem's AT port by probing candidates.

`healthcheck.py` is a separate short-lived process, run by the compose file's
`healthcheck:` (`docker-compose.example.yml`) rather than by a `HEALTHCHECK`
instruction in the image.

## Rules

These are not style preferences. Each one exists because its absence caused a
real failure.

**Never log, store, or echo message bodies, credentials, or full phone
numbers.** Numbers are masked to a suffix. Log counts, lengths, status and
timestamps. This extends to exceptions: an exception's `str()` can carry a
request URL, and for this API that URL embeds the bot token — so failures leave
this codebase as types and status codes, never as a raw library exception.

**No startup deadline and no retry ceiling for a dependency that is not ready.**
The USB modem can appear well after the container does. Waiting is correct
behaviour, not a hang. `tests/test_main.py` has an AST guard that fails if a
timeout reappears around the supervisory wait.

**All durations use `time.monotonic()`.** These boards often have no RTC
battery, so the wall clock reads years in the past until NTP corrects it, then
jumps forward. The only wall-clock reading any duration is built from is the
health-file mtime comparison in `healthcheck.py`. A message timestamp is
message data, not a duration — that is not a violation: `device_manager.py`
prints the carrier's service-centre timestamp, and falls back to
`datetime.now()` for a PDU that carries none.

**`run()` must never return under normal operation.** A returning body is
treated as a failure. Loop inside it. How long a session lasts feeds two
separate mechanisms, and calling both of them "flapping" hides a whole class of
failure behind a word. A session that ends sooner than
`SERVICE_STABLE_SECONDS` never resets the reconnection backoff or the log
throttle, so a component that connects and fails immediately keeps looking
broken instead of looking busy. Separately, and only for sessions that lasted
`SERVICE_STABLE_SECONDS`, a component that keeps ending connected sessions is
counted, and the watchdog ends the process at `WATCHDOG_CHURN_SESSIONS` of them
inside `WATCHDOG_CHURN_WINDOW`. The second is what covers a failure slower to
raise than the stable window — which the first, on its own, reads as a
recovery.

**Any loop that can complete an iteration without awaiting something that
genuinely blocks is a bug.** A `StreamReader` at EOF returns `b""` with no
await, which will spin a core at 100% and starve the event loop.

**Tunables live in `config.py`,** with a floor where a zero value would disable
the guard the setting exists for. Every relationship between two settings is
either enforced by a clamp or reported by a startup notice — never neither. One
that lives only in a comment is one the first operator to tune either side of it
breaks silently.
`tests/test_config.py::test_every_invariant_is_either_enforced_or_reported`
walks the list of those relationships against configurations an operator might
plausibly reach for, and fails if one holds neither way. A ceiling that is
deliberately absent says so where the setting is documented, so an absent bound
cannot be read as a forgotten one.

**Comments, docstrings and log messages are English.** Telegram-facing user text
stays in its current language — users read it. Comments explain the mechanism,
never a specific deployment or past incident.

**Documentation that describes the same fact in more than one file moves
together.** A user-facing change moves `README.md` and its three translations
(`README_CN.md`, `README_JP.md`, `README_FA.md`) together; a settings change
also moves `config.py`, `.env.example` and `docker-compose.example.yml`, whose
`stop_grace_period` and `start_period` comments state the relationships between
`STOP_BUDGET_SECONDS`, `NOTIFY_TIMEOUT`, `SERIAL_CLOSE_TIMEOUT` and
`SERVICE_STABLE_SECONDS` in the terms an operator acts on — `config.py`
delegates half of one invariant to that file by name; a change to the AT
command set also moves `doc/AT_COMMANDS.md`, whose tables claim to cover every
command this project issues. Nothing checks this, so it is on the author.

## Testing

Tests must not touch a real serial device, a real `/dev`, or the network, and
must not sleep a real duration to synchronise — inject the seams
(`_sleep`, `_port_exists`, `opener`, `clock`, `rng`). Every test terminates on a
condition the test controls.

Write the test first, watch it fail for the reason you expect, then implement.
Before trusting a new test, break the code it covers and confirm it fails —
several tests in this repo's history passed against the bug they named.

Test output must be pristine. A warning, an unretrieved task exception, or a
"coroutine was never awaited" is a defect.

## Commits

Conventional Commits (`feat:`/`fix:`/`refactor:`/`docs:`/`test:`/`chore:`). The
body explains *why*, not what the diff already shows. **No tool or assistant
attribution of any kind** — no co-author trailers, no "generated with" lines.

## Releasing

Publishing is a git tag push and nothing else:

```bash
git tag 2026-08-05b && git push origin 2026-08-05b
```

CI runs the suite on both interpreters, builds `linux/amd64` and `linux/arm64`,
pushes the image as that tag and as `latest`, then reads the published index
back and fails if either platform is missing. The tag name is the image tag, so
every published image maps to a commit without a lookup.

**Never build and push by hand.** The tag list is easy to get right once and
lose silently later: a machine missing the emulator for the other architecture
still produces a correct multi-platform image for as long as every foreign
layer is a cache hit, and publishes a single-platform one the first time it is
not. That is how `latest` became `arm64`-only while four README files went on
promising `amd64`. The check in CI reads its wanted platforms from its own
list rather than from the build's, so editing the build to drop one fails
rather than publishing quietly.

Publishing needs `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as repository
secrets. Without them a tag push fails at the login step, having already run
the tests and the build — it does not publish half of anything.

## Things that will catch you out

- `.env` holds the live credentials — `BOT_TOKEN`, `CHAT_ID`, `PROXY_URL`. It is
  gitignored; only `.env.example` is tracked. Never read it into output.
- `docker-compose.yml` is gitignored too, and a local copy may diverge from the
  example in ways specific to one machine. Only `docker-compose.example.yml` is
  tracked. Never read the local one into output either.
- The compose file bind-mounts `/dev` to `/hostdev` instead of using `devices:`.
  See "Why not `devices:`" in `README.md` for the reason. Do not "simplify"
  this back.
- `device_cgroup_rules` is load-bearing, not decoration. Without it the container
  gets `EPERM` opening the device.
- To check the process really holds the serial port, look at the **Python
  child**, not the container's main PID — the entrypoint is `tini`:
  ```bash
  PY=$(pgrep -P "$(docker inspect -f '{{.State.Pid}}' sms-forwarder)" | head -1)
  ls -l /proc/$PY/fd | grep -E 'ttyUSB|ttyACM'
  ```
- `doc/AT_COMMANDS.md` maps every AT command this project issues to its purpose, so
  you can find the right section of the vendor manual without reading all of it.
- The `Dockerfile` copies a named allow-list, not `COPY . .`. A new file at the
  repository root must be added to
  `COPY main.py logger.py config.py healthcheck.py LICENSE ./` or it is absent
  from the image. Nothing catches that: `docker build` never runs Python, and
  the suite runs against the source tree, so the first sign is an `ImportError`
  in a container someone has already pulled. Anything new under `module/` is
  already covered by `COPY module/ ./module/`.
