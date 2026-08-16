# Changelog

## 0.3.6

**Fixed**

- The "memory some" line on the Memory pressure (PSI) chart was invisible.
  Series paint in array order, each one drawn over the last, and "memory
  full" was listed after "memory some" — so on a lightly-loaded system, where
  full (mathematically always <= some) sits at or near the same near-zero
  value, the opaque red "full" line completely painted over the blue "some"
  line beneath it. Reordered so "some" (the earlier warning indicator) draws
  last, and "full" is now dashed and unfilled so it reads as a reference line
  rather than silently vanishing under whichever series is nearest. `chart.js`
  gained a per-series `dash` option, and always resets line-dash state after
  each stroke — `setLineDash` is canvas-context state, not per-stroke, so a
  dash left set would otherwise leak into every gridline and series drawn
  after it.

## 0.3.5

**Added**

- Load average chart on the Now tab: 1m/5m/15m in one chart, so a spike versus
  a sustained load is visible at a glance instead of only the 1m figure.
- Temperature chart on the Now tab, with the same 80°C threshold line the
  tile already warns at.

## 0.3.4

**Fixed**

- Recorder database size still never appeared, even after 0.3.2's fix. The
  0.3.2 defensive logging did its job and pointed straight at the real cause:
  the confirming `result` for `system_health/info` was empty (`shape=[]`) on
  every call. Turns out `system_health/info` is not a request/response
  command — it is a subscription, exactly like `subscribe_events`. Home
  Assistant confirms it with `send_result(msg["id"])` and no payload; the
  actual data streams in afterward as an `event` message reusing the same id.
  `_handle()` routed every incoming `event` straight to the state_changed
  handler regardless of which subscription produced it, so system_health's
  real data was silently discarded no matter how correctly the empty result
  was parsed. Incoming events are now routed by which subscription their id
  belongs to, and the subscription is unsubscribed once its one-shot info
  request finishes, so a long-running connection doesn't accumulate open
  subscriptions on Core's side.

## 0.3.3

**Fixed**

- "Disk Free" and "Unavailable entities" showed `—` most of the time. The fast
  loop (every ~15s) replaced the entire live-metrics dict on every tick, while
  the slow loop (every `slow_interval`, default 300s) merged its own keys in —
  disk usage, entity census, network state, integration health. The next fast
  tick, at most 15s later, discarded them, leaving them missing for roughly
  95% of every 5-minute cycle. Both loops now go through one shared merge
  function, so they cannot diverge back into this.
- The entity-census template (source of "Unavailable entities") failed
  silently on a non-200 response, a request error, or an unexpected render
  shape. All three now log what actually happened.
- Tables overflowed the page on narrow screens instead of scrolling in place.
  Every table is now wrapped in its own horizontally-scrollable container, and
  a CSS Grid trap that defeated it on the Host tab — a bare `1fr` track has an
  implicit `auto` minimum, so a long unbroken string (a `/dev/serial/by-id`
  path) forced the whole track, and the page, wider than the viewport — is
  fixed with `minmax(0, 1fr)` plus a defensive `min-width: 0` on cards.

## 0.3.2

**Fixed**

- Recorder database size and its history chart were always empty, on every
  backend (SQLite and MariaDB alike — this had nothing to do with which one is
  in use). `system_health/info` doesn't return component data at the top level;
  it wraps everything as `{"type": "initial", "data": {...}}`, and the code was
  reading `result["recorder"]` instead of `result["data"]["recorder"]`. That
  key is always absent in the real shape, so the parser returned silently on
  every single call — no exception, connection stayed healthy, nothing in the
  logs to explain it. Every branch of this path now logs, and a regression
  test uses the exact payload captured from a live instance while diagnosing
  this, so the same wrong-shape bug can't hide silently again.
- A websocket request failing outright is now logged for every request kind,
  not only the one kind that happened to already have a fallback path.

## 0.3.1

- The dashboard's JavaScript and CSS are now served with version-stamped URLs
  and revalidation headers. Previously a stale copy could shadow a new build —
  either in the browser or in a proxy — which is indistinguishable from "the
  bug was never fixed".
- The running add-on version is shown in the dashboard header, so which build
  you are actually looking at is answerable at a glance rather than by guessing.

## 0.3.0

**Fixed**

- Charts grew taller on every refresh until the dashboard tab crashed.
  Assigning `canvas.height` also rewrites the height attribute, which the code
  then read back as if it were CSS pixels, multiplying the canvas by
  `devicePixelRatio` every 5 seconds. Only visible on displays with scaling,
  which is why it was not caught before release.
- Every successful WebSocket result was fed to the system-health handler
  regardless of what had been requested. Results are now routed by message id.

**Added**

- **Per-integration availability tracking.** Entities are mapped to their
  integration via the entity registry, or via a template fallback when the
  registry is not permitted. The Integrations view shows total, unavailable and
  chronic counts per integration.
- **Multi-integration outage detection.** When several unrelated integrations
  each lose entities within a short window, that is recorded as a critical event
  and alerted — the signature of a shared cause rather than one broken device.
  Chronically dead entities are excluded, so a restart on a system with many
  long-dead entities does not trigger it.
- **Standing problems panel**, separating entities that have been broken for
  hours from those that just dropped.
- **USB enumeration failure classification** — `device not accepting address`,
  `unable to enumerate`, descriptor read errors, over-current and bus resets.
  These precede a device vanishing, so they are early warning rather than
  after-the-fact confirmation.
- **Network state tracking** via `/network/info`: link up/down, DHCP address
  changes, gateway and DNS changes, and internet reachability. Deliberately no
  traffic counters — those are namespaced per container and reading the host's
  would require privileges this add-on does not take.
- Integration health and network state in the full diagnostic export.
- A `tests/` directory: 54 assertions covering kernel pattern classification,
  outage-cluster logic and export contents.

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
