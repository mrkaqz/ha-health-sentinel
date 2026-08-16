# Health Sentinel — a black-box flight recorder for Home Assistant

Home Assistant crashed again and there is nothing in the logs. This add-on
exists to fix that.

## Why an add-on, and not an integration

An add-on runs in its own container, supervised by the Supervisor. **When Home
Assistant Core dies, this keeps running.**

That single property is the whole design. An integration lives inside Core, so
when Core hangs or is killed, the integration goes down with it and whatever it
knew about the seconds before the crash is lost. An add-on watches from outside:
it sees Core die in real time, timestamps the moment, and preserves the
preceding half hour of high-resolution metrics that Core never got the chance to
write to disk.

It also means alerts still work. Home Assistant's own notify services cannot
tell you that Home Assistant is down; this can, because it talks to Telegram
directly.

## What it records

**Live, from outside Core**
- Core liveness and real request latency — rising latency is the classic
  signature in the minutes before a hang
- Per-container CPU, memory, network and block I/O for Core, Supervisor and
  every add-on, with memory-growth slopes for leak detection
- Host PSI (pressure stall information), load, memory, swap, temperature
- Disk usage and SSD lifetime

**Host and kernel events, tailed continuously**
- OOM kills, including which process the kernel chose
- Machine check exceptions and ECC errors — failing RAM or CPU
- Filesystem errors and read-only remounts
- USB disconnects, so a Zigbee or Z-Wave coordinator dropping off is a
  timestamped fact rather than a mystery
- Kernel panics, soft lockups, thermal throttling, NIC flaps

**After a crash**
- Boot classification: clean restart, add-on restart, host reboot, or power loss
- The **previous boot's journal**, retrieved from the Supervisor — this is where
  an unexplained crash usually confesses
- The previous run's `home-assistant.log.1`, captured before it is overwritten
- A downloadable evidence bundle per incident

**Recorder pressure**
- Database size over time, and which entities generate the most state changes.
  Every state change is a recorder write, so this ranks what is actually filling
  your database — without needing any database credentials.

## Privileges

None beyond `hassio_role: manager`. No `full_access`, `privileged`, `host_pid`,
`host_network`, `kernel_modules`, `devices` or `docker_api`. **Protection mode
stays enabled** and there is nothing to toggle after install.

Host metrics come from plain reads of `/proc` and `/sys`, which are not
namespaced per container and so report the host's real values.

## Install

### As a local add-on (fastest to iterate)

Copy the `health_sentinel` directory into `/addons` on your Home Assistant
machine, using the Samba share, Studio Code Server, or SSH:

```bash
/addons/health_sentinel/
```

Then **Settings → Add-ons → ⋮ → Check for updates**, and install it from the
*Local add-ons* section.

### From this repository

**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, then add:

```
https://github.com/mrkaqz/ha-health-sentinel
```

Health Sentinel appears in the store like any other add-on.

## Configuration

| Option | Default | What it does |
| --- | --- | --- |
| `probe_interval` | 5 | Seconds between Core liveness probes |
| `sample_interval` | 15 | Seconds between metric samples |
| `slow_interval` | 300 | Seconds between disk/hardware/entity polls |
| `ring_buffer_minutes` | 30 | How much high-resolution history an incident freezes |
| `alert_core_down_after` | 90 | Seconds Core must be unreachable before an incident opens |
| `retention_raw_days` | 7 | How long raw samples are kept |
| `retention_rollup_days` | 90 | How long 1-minute rollups are kept |
| `disk_free_warn_pct` | 10 | Disk-free warning threshold |
| `memory_warn_pct` | 90 | Memory warning threshold |
| `telegram_enabled` | false | Turn on Telegram alerts |
| `telegram_bot_token` | — | From [@BotFather](https://t.me/botfather) |
| `telegram_chat_id` | — | Your chat or group id |
| `webhook_url` | — | Optional generic JSON webhook |
| `log_level` | info | Add-on log verbosity |

Incidents are never deleted. Only samples age out.

## Publishing your own build

The add-on builds locally by default, which needs no CI. To publish prebuilt
images for fast installs, run the included workflow and add an `image:` key to
`health_sentinel/config.yaml`:

```yaml
image: ghcr.io/mrkaqz/health-sentinel-{arch}
```

Nothing else changes and nobody has to reinstall.

## Licence

MIT.
