# Health Sentinel

A black-box flight recorder for Home Assistant. It records health data, host
kernel events and crash forensics **from outside Core**, so the evidence
survives the crash.

## The idea in one paragraph

This add-on runs in its own container. When Home Assistant Core hangs, gets
OOM-killed, or the whole machine loses power, the add-on is either still running
and watching, or it comes back afterwards and reconstructs what happened from
the previous boot's journal. Either way you end up with a timestamped verdict
instead of an empty log.

## First run

Install, start, and open **Sentinel** in the sidebar. There is nothing to
configure to get value out of it — the defaults record everything.

Two things worth doing straight away:

1. **Set up Telegram** (below) so you find out about a crash without checking.
2. **Leave it running.** It cannot explain a crash that happened before it was
   installed. The first incident it records is the first one it can explain.

## The dashboard

| View | What it is for |
| --- | --- |
| **Now** | Live state. Core latency, memory pressure, load, disk, temperature. |
| **Timeline** | Every incident, with a verdict. Click one for the full report. |
| **Host** | HAOS version and boot slot, serial devices, and the live kernel event feed. |
| **Containers** | Per-add-on CPU and memory, sorted by memory, with growth slopes and restart counts. |
| **Recorder** | Database size and the entities generating the most writes. |
| **Logs** | Core, Supervisor, host journal and per-add-on logs with a filter, plus file export. |

### Reading the "Now" view

**Core latency** is the important one. It is the round-trip time of a real
request through to Core's event loop, not a ping. A gently rising line over
minutes is what a hang looks like before it becomes a hang.

**Memory pressure (PSI)** is not the same as "memory used". It is the share of
time processes spent *stalled waiting* for memory. A machine at 85% memory with
0% pressure is fine. A machine at 85% memory with 20% pressure is dying. PSI
climbing is the clearest early warning of an OOM kill.

### Reading an incident report

Each incident carries a classification:

| Verdict | What happened |
| --- | --- |
| `core_unreachable` | Core stopped answering while the add-on kept running. The full metric window before the event is preserved. |
| `host_power_loss` | The host stopped without a shutdown sequence — power cut, hard reset, panic or thermal cutoff. |
| `host_reboot` | The host rebooted, and the previous boot's journal shows a normal shutdown. |
| `addon_restart` | The add-on restarted but the host stayed up. |
| `clean_restart` | Orderly stop. Not an incident. |

Below the verdict is **Evidence from the logs** — lines matched out of the
journal, each with an explanation. This is where an OOM kill or an ECC error
shows up.

**Download evidence bundle** produces a `.tar.gz` containing the metric window,
classified events, add-on states at the moment of the incident, and the relevant
log excerpts including the previous boot's journal. It is self-contained, so it
stays useful after the machine has been rebuilt.

## Exporting logs for AI analysis

The **Logs** view has two download buttons.

**Download this log** saves whatever is currently selected — the chosen source,
with your filter applied — as a `.log` file.

**Full diagnostic** is the one to reach for when something has gone wrong. It
produces a single plain-text file containing:

- a short reading guide, so an AI knows what it is looking at
- system facts: HAOS version, boot slot, kernel, disk, disk wear, uptime
- every current metric, including PSI
- incident history with the verdict reached for each
- all classified kernel, hardware and add-on events
- container CPU, memory and memory-growth rates
- the Core log, Supervisor log and host journal
- the logs of any add-on currently in a bad state

Plain text rather than an archive, deliberately — you can upload the file
straight into a chat with an AI assistant and ask what killed Home Assistant, no
extraction step.

The reason the extra context matters: **Core's log ends at the moment Core
dies**, so for a hard crash the cause is usually not in it. The answer is
normally in the host journal or the kernel events at the same timestamp, and an
AI given only the Core log cannot see that. The export puts them side by side.

Logs of add-ons that are running normally are left out on purpose. Including all
thirty-odd would bury the signal.

## Telegram alerts

1. Message [@BotFather](https://t.me/botfather), send `/newbot`, follow the
   prompts, and copy the token.
2. Send your new bot a message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id`.
3. In the add-on **Configuration** tab set `telegram_enabled` to on, paste the
   token and chat id, and restart.
4. Press **Send test alert** at the bottom of the dashboard.

Alerts fire for: Core unreachable, Core recovered (with the outage duration), an
add-on entering `error`, disk or memory thresholds crossed, a serial device
disappearing, critical kernel events, and a boot summary after every restart
that was not clean.

Everything is rate-limited and de-duplicated. Alerts are always written to the
local event log first, so a failed delivery never loses the record.

## Privileges

The add-on asks for `hassio_role: manager` and nothing more. It does **not** use
`full_access`, `privileged`, `host_pid`, `host_network`, `kernel_modules`,
`devices` or `docker_api`, so **protection mode stays enabled**.

Docker socket access would have allowed reading container exit codes directly.
That was not worth an escalation, so killed-versus-clean is derived from the
kernel's OOM messages correlated with Supervisor state transitions instead.

On startup the add-on probes each Supervisor endpoint once and logs a capability
report, so a permissions problem appears in the log rather than as silently
empty charts.

## Storage

Everything lives in `/data/sentinel.db`, deliberately separate from Home
Assistant's recorder — the sentinel must not become a victim of the problem it
is diagnosing.

Raw samples are kept for 7 days and 1-minute rollups for 90 days, both
configurable. **Incidents and their bundles are never purged.** Expect a few
hundred megabytes in steady state.

## Troubleshooting

**"PSI is unavailable on this kernel"** — the kernel lacks `CONFIG_PSI`.
Everything else works; the pressure tiles show `n/a`. Home Assistant OS ships
PSI enabled.

**Charts are empty right after install** — there is no history yet. The 6-hour
charts fill in as samples accumulate.

**Event bus shows disconnected** — the add-on cannot reach Core's WebSocket API.
Normal while Core is restarting. If it persists while Core is healthy, check the
add-on log for an auth error.

**No incidents listed** — nothing has gone wrong since installation. That is the
good outcome.
