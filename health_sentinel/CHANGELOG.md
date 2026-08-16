# Changelog

## 0.2.0

- Log export from the Logs view. **Download this log** saves the current source
  with its filter applied; **Full diagnostic** produces one plain-text file
  combining every log with the context needed to interpret it — versions,
  current metrics, incident history, classified kernel events, container memory
  and growth rates, and the logs of any add-on in a bad state.
- The export opens with a reading guide, including the point that Core's log
  ends when Core dies, so a hard crash is usually explained by the host journal
  at the same timestamp rather than by Core itself.
- Plain text rather than an archive, so the file can go straight to an AI
  assistant with no extraction step.

## 0.1.0

First release.

- Records Core liveness and real request latency every 5 seconds, so a hang is
  visible as rising latency before it becomes an outage.
- Samples host PSI, load, memory, swap, temperature and per-container CPU/memory
  for Core, Supervisor and every add-on, with memory-growth slopes for leak
  detection.
- Tails the host journal continuously and classifies kernel events: OOM kills,
  machine check and ECC errors, filesystem faults, USB disconnects, panics, soft
  lockups, thermal throttling and NIC flaps.
- Diffs the hardware inventory each cycle, so a Zigbee or Z-Wave coordinator
  disappearing is recorded explicitly rather than inferred.
- Opens an incident when Core stops responding, immediately freezing the last 30
  minutes of high-resolution metrics along with Core's final log output.
- Classifies every restart on startup — clean restart, add-on restart, host
  reboot or power loss — by comparing boot id, host uptime and a fsynced
  heartbeat, then retrieves the previous boot's journal for evidence.
- Captures `home-assistant.log.1` before it is overwritten.
- Ranks entities by state-change rate to show what is filling the recorder
  database, without needing database credentials.
- Ingress dashboard with seven views, self-contained and dependency-free so it
  works while Core is down.
- Telegram and generic webhook alerting, independent of Home Assistant, with
  rate limiting and de-duplication.
- Runs at `hassio_role: manager` with protection mode enabled.
