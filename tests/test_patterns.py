"""Kernel journal classification tests.

Run with: python tests/test_patterns.py

These are the lines the sentinel has to recognise for a crash to be
explainable. The first case is the one actually observed in dmesg during a live
investigation, which fell through unclassified before the usb_enumeration_error
pattern existed.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "health_sentinel", "rootfs", "opt", "sentinel"))

from collectors import host_events  # noqa: E402

CASES = [
    # --- USB enumeration failures: early warning that a dongle is failing ---
    ("usb 2-4.2: device not accepting address 3, error -71", "usb_enumeration_error"),
    ("usb 1-1.4: unable to enumerate USB device", "usb_enumeration_error"),
    ("usb 2-4: device descriptor read/64, error -32", "usb_enumeration_error"),
    ("hub 2-0:1.0: over-current change on port 4", "usb_enumeration_error"),
    ("hub 1-0:1.0: Cannot enable port 2. Maybe the USB cable is bad?",
     "usb_enumeration_error"),
    # A reset is milder than a failure to enumerate, and must not be conflated.
    ("usb 1-4: reset high-speed USB device number 7 using xhci_hcd", "usb_reset"),
    # The new patterns must not steal these from the existing ones.
    ("usb 1-4: USB disconnect, device number 7", "usb_disconnect"),
    ("usb 1-4: new full-speed USB device number 9 using xhci_hcd", "usb_connect"),

    # --- memory ---
    ("kernel: Out of memory: Killed process 4127 (frigate.detector)", "oom"),
    # Without the "Out of memory:" prefix the victim pattern wins — it is the
    # one that names the process and its footprint.
    ("Killed process 4127 (frigate.detector) total-vm:8123456kB", "oom_victim"),

    # --- hardware, filesystem, kernel, network ---
    ("mce: [Hardware Error]: Machine check events logged", "hardware_error"),
    ("EDAC MC0: 1 CE memory read error on CPU_SrcID#0", "hardware_error"),
    ("EXT4-fs error (device sda1): ext4_find_entry:1663", "filesystem"),
    ("Remounting filesystem read-only", "filesystem"),
    ("Kernel panic - not syncing: Fatal exception", "kernel_fault"),
    ("watchdog: BUG: soft lockup - CPU#2 stuck for 22s!", "kernel_fault"),
    ("e1000e 0000:00:1f.6 eno1: NIC Link is Down", "network"),
    ("blk_update_request: I/O error, dev sda, sector 123456", "disk_io"),

    # --- ordinary chatter must stay unclassified ---
    ("systemd[1]: Started Session 12 of user root.", None),
    ("kernel: usbcore: registered new interface driver usbfs", None),
    ("Accepted publickey for root from 192.168.42.10", None),
]


def main() -> int:
    failures = 0
    for line, expected in CASES:
        match = host_events.match_line(line)
        actual = match.kind if match else None
        if actual != expected:
            failures += 1
            print(f"FAIL  expected={expected}  got={actual}\n      {line}")
        else:
            print(f"PASS  {str(actual):<22} {line[:62]}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
