# Deferred: netconsole / off-box log forwarding

**Status: considered and deliberately not built.** Shelved 2026-08-16.

Boot forensics (shipped in v0.1.0) already captures logs from before a crash and
shows them after restart, which is what was actually wanted. netconsole closes
only one narrow remaining gap — the kernel panic message itself — and there is
no evidence this system has ever had a kernel panic. Every boot verdict so far
has been "clean restart" or "first run".

This document exists so that revisiting the decision later costs nothing. The
research is done; don't repeat it.

---

## Revisit if — and only if — all three hold

1. A real crash occurs (host stops unexpectedly, not a clean restart), **and**
2. Health Sentinel classifies it `host_power_loss` or `host_reboot`, **and**
3. The previous boot's journal it retrieves ends with **no explanation** — no
   `oom-killer`, no `mce`/`EDAC`, no `EXT4-fs error`, no thermal event; the log
   simply stops mid-normal-operation.

That combination is the signature of a kernel panic whose message was lost, and
the only case where netconsole would have said something new. Any other outcome
means local capture did its job and this stays shelved.

---

## Why the obvious design does not work

netconsole is **not** a man-in-the-middle. It is a kernel transmitter: the
kernel pushes `printk` out as UDP from inside the panic path, bypassing
userspace, journald and disk entirely. It is configured on the *sending* side
and requires a receiver somewhere else.

**An add-on cannot be that receiver.** HAOS builds with
`CONFIG_PANIC_TIMEOUT=5` — on panic, all userspace freezes instantly and the box
reboots five seconds later. A container on that host is frozen too. The packet
physically leaves the NIC, but nothing on that machine is alive to read it.

So a same-box receiver is:

- **useless** for the panic case it exists for, and
- **redundant** otherwise — the journal tail already catches OOM kills, USB
  enumeration failures and filesystem faults.

Loading netconsole *from* the add-on would need `kernel_modules: true` →
`CAP_SYS_MODULE` (i.e. load arbitrary kernel code), ending protection mode and
dropping the security rating from 6 to the floor — to run a receiver that cannot
work anyway.

---

## Verified findings — do not re-research

| Finding | Source | Consequence |
|---|---|---|
| `grub.cfg` runs `file_env -f ($root)/cmdline.txt cmdline` | HAOS `board/pc/grub.cfg` | Kernel parameters **can** be added via `cmdline.txt` on the boot partition — the supported route |
| `CONFIG_PANIC_TIMEOUT=5` | HAOS `kernel/v6.18.y/haos.config` | Userspace is dead at panic; same-box receiver not viable |
| `CONFIG_IKCONFIG_PROC=y` | same | `/proc/config.gz` exists, so the add-on can check netconsole availability with **zero privileges** |
| `Storage=auto`, `SystemMaxUse=500M` | HAOS `rootfs-overlay/etc/systemd/journald.conf` | Journal **persists across reboots** (`SystemMaxUse` governs persistent storage only; volatile uses `RuntimeMaxUse`). This is what boot forensics already relies on. |
| Boot partition is separate from the A/B rootfs slots | live `/os/info` | `cmdline.txt` survives HAOS OS updates |
| `eno1`, `192.168.42.5/24`, gw `192.168.42.1`, primary | live `/api/host` | Exact values for the netconsole parameter |
| No `CONFIG_NETCONSOLE` in either HAOS kernel-config fragment | HAOS repo | **Inconclusive** — the fragments overlay a base defconfig that wasn't located. Must be checked on the running kernel. |

### Prerequisites, one command each (from the SSH add-on)

```bash
# 1. Is netconsole compiled into this kernel? If absent, Part B is impossible
#    without rebuilding HAOS — stop there.
zcat /proc/config.gz | grep -i netconsole

# 2. Is the eno1 driver built-in, or a module loaded later? If it is a module,
#    boot-time netconsole fails with "device eno1 not found", because netconsole
#    initialises before udev loads the driver.
basename "$(readlink /sys/class/net/eno1/device/driver)"
grep -E "^(e1000e|igc|igb|r8169)" /proc/modules   # empty output = built-in = good
```

---

## The design, if it is ever needed

Receiver is the **Synology NAS** — always on, and crucially not the HA box. Two
halves, deliberately using *different* receivers because they speak different
protocols.

### Part A — add-on forwards to the NAS (no new privileges)

New `rootfs/opt/sentinel/forwarder.py`, following the `notify.py` shape: async,
best-effort, never blocks or crashes a collector.

- **RFC5424 syslog** over UDP/TCP → **Synology Log Center**, which expects
  exactly this format
- Forwards, in rising volume: incidents → classified events (the `events` table)
  → alerts → raw journal lines (opt-in, off by default; this host's journal
  carries heavy Sentry retry noise)
- Hooks the paths `main.py` already has — `_on_host_event`,
  `_poll_addon_states`, `_poll_network`, `Notifier.alert` — rather than adding a
  parallel one
- Options: `syslog_enabled`, `syslog_host`, `syslog_port` (514),
  `syslog_protocol`, `syslog_forward_journal`

**Bundles need no code at all.** The Samba add-on already shares `/share`, and
`bundle.export_to_share()` already writes incident bundles there. A scheduled
pull from the Synology gets them off-box for free.

### Part B — netconsole at host level

The add-on plays **no part in transmitting**; it only verifies.

Append to `cmdline.txt` on the boot partition (label `hassos-boot`), then reboot:

```
netconsole=+6666@192.168.42.5/eno1,6666@<NAS_IP>/<NAS_MAC>
```

- `+` selects extended format (adds priority and timestamp)
- the target **MAC is mandatory** — the kernel cannot rely on ARP resolution in
  panic context

**Receiver: a small Docker UDP listener on the NAS, not Log Center.** netconsole
emits raw kernel lines (`priority,seq,timestamp;message` in extended mode),
which is not RFC5424 — Log Center would reject or mangle it. This is why the two
halves target different receivers on the same NAS.

**The add-on's only role** would be a `netconsole_status` check in
`collectors/host_psi.py`, which already owns unprivileged `/proc` reads: read
`/proc/config.gz` for availability and `/proc/cmdline` for whether it is
actually configured. Neither file is namespaced, so the container sees the
host's real values. Surface it in the Host view and the diagnostic export, so
the dashboard can state whether the safety net is armed rather than the user
assuming it.

### Verification, if built

- RFC5424 formatter unit-tested against the spec. A malformed PRI or timestamp
  is silently dropped by most syslog servers, which looks identical to "not
  working".
- Delivery tested against a local UDP socket bound inside the test.
- **Failure must be non-fatal**: point it at a black-holed address and confirm
  collectors keep running and the local `events` table still fills. Log
  forwarding must never become a way to *lose* logs.
- `echo "test" > /dev/kmsg` should reach the NAS listener within milliseconds.
- Only a real panic (`echo c > /proc/sysrq-trigger`) truly proves the panic
  path — and it deliberately crashes the machine.

---

## Related, still outstanding

**Boot forensics has never actually fired on an unclean boot.** It has only ever
seen clean restarts. This project found real bugs in three separate features
that looked fine until they were tested against reality — the chart canvas
sizing, the recorder's websocket subscription routing, and the live-metrics
merge. The feature that matters most on crash day deserves the same scrutiny
before being relied upon.

The closest safe simulation is `echo b > /proc/sysrq-trigger`, which reboots
immediately without syncing or shutting down cleanly. Offered and declined for
now.
