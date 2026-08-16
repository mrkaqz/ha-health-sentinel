"""Multi-integration outage detection tests.

Run with: python tests/test_availability.py

The failure mode this guards against is a detector that fires constantly. On a
real instance a large share of entities are permanently dead, and every restart
re-reports them; if that counts as a drop, the alert is worthless.
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "health_sentinel", "rootfs", "opt", "sentinel"))

from collectors.availability import AvailabilityTracker  # noqa: E402

NOW = int(time.time())

MAPPING = {
    "light.wled_a": "wled", "light.wled_b": "wled", "light.wled_c": "wled",
    "camera.ezviz_a": "ezviz", "camera.ezviz_b": "ezviz",
    "sensor.esphome_a": "esphome", "sensor.esphome_b": "esphome",
    "switch.tplink_a": "tplink", "switch.tplink_b": "tplink",
    "sensor.zha_a": "zha", "sensor.zha_b": "zha",
}

results = []


def check(name, condition):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}")


def build(**kwargs):
    tracker = AvailabilityTracker(
        chronic_after_minutes=kwargs.pop("chronic_after_minutes", 60),
        cluster_window_seconds=kwargs.pop("cluster_window_seconds", 120),
        cluster_min_integrations=kwargs.pop("cluster_min_integrations", 3),
        cluster_min_entities=kwargs.pop("cluster_min_entities", 2),
    )
    tracker.set_mapping(dict(MAPPING), "test")
    for entity_id in MAPPING:
        tracker.seed(entity_id, "on", NOW - 86400)
    return tracker


def drop(tracker, entity_id, ts):
    return tracker.observe(entity_id, "unavailable", "on", ts=ts)


# --- three integrations dropping together must fire ------------------------
t = build()
for entity_id in ["light.wled_a", "light.wled_b", "camera.ezviz_a",
                  "camera.ezviz_b", "sensor.esphome_a", "sensor.esphome_b"]:
    drop(t, entity_id, NOW)
cluster = t.check_cluster(NOW)
check("three integrations dropping together fires", cluster is not None)
check("cluster names all three integrations",
      cluster and cluster["integrations"] == ["esphome", "ezviz", "wled"])
check("cluster counts six entities", cluster and cluster["entity_count"] == 6)

# --- only two integrations must not fire -----------------------------------
t = build()
for entity_id in ["light.wled_a", "light.wled_b",
                  "camera.ezviz_a", "camera.ezviz_b"]:
    drop(t, entity_id, NOW)
check("two integrations does not fire", t.check_cluster(NOW) is None)

# --- one entity each is below the per-integration floor --------------------
t = build()
for entity_id in ["light.wled_a", "camera.ezviz_a", "sensor.esphome_a"]:
    drop(t, entity_id, NOW)
check("one entity per integration does not fire", t.check_cluster(NOW) is None)

# --- drops spread beyond the window must not accumulate --------------------
t = build()
drop(t, "light.wled_a", NOW - 600)
drop(t, "light.wled_b", NOW - 600)
drop(t, "camera.ezviz_a", NOW - 300)
drop(t, "camera.ezviz_b", NOW - 300)
drop(t, "sensor.esphome_a", NOW)
drop(t, "sensor.esphome_b", NOW)
check("drops outside the window do not accumulate", t.check_cluster(NOW) is None)

# --- already-dead entities re-reporting is not a drop ----------------------
# This is the restart case: everything that was dead comes back as dead.
t = build()
for entity_id in ["light.wled_a", "light.wled_b", "camera.ezviz_a",
                  "camera.ezviz_b", "sensor.esphome_a", "sensor.esphome_b"]:
    t.seed(entity_id, "unavailable", NOW - 86400)
observed = [t.observe(e, "unavailable", "unavailable", ts=NOW) for e in MAPPING]
check("re-reporting a dead entity is not a drop",
      all(o is None for o in observed))
check("restart of a broken system does not fire", t.check_cluster(NOW) is None)

# --- chronic entities are excluded from clusters ---------------------------
# They flap, but they have been broken for a day; that is not a new event.
t = build(chronic_after_minutes=60)
for entity_id in ["light.wled_a", "light.wled_b", "camera.ezviz_a",
                  "camera.ezviz_b", "sensor.esphome_a", "sensor.esphome_b"]:
    t.seed(entity_id, "on", NOW - 86400)
    drop(t, entity_id, NOW)
    # Backdate so each looks long-dead.
    t.seed(entity_id, "unavailable", NOW - 86400)
check("chronic entities do not form a cluster", t.check_cluster(NOW) is None)

# --- one outage produces one alert, not one per event ----------------------
t = build()
for entity_id in ["light.wled_a", "light.wled_b", "camera.ezviz_a",
                  "camera.ezviz_b", "sensor.esphome_a", "sensor.esphome_b"]:
    drop(t, entity_id, NOW)
first = t.check_cluster(NOW)
second = t.check_cluster(NOW + 1)
check("a continuing outage does not re-alert", first is not None and second is None)

# --- unmapped entities cannot contribute -----------------------------------
t = build()
for entity_id in ["unknown.a", "unknown.b", "unknown.c",
                  "unknown.d", "unknown.e", "unknown.f"]:
    t.seed(entity_id, "on", NOW - 86400)
    t.observe(entity_id, "unavailable", "on", ts=NOW)
check("entities with no known integration cannot cluster",
      t.check_cluster(NOW) is None)

# --- reporting ------------------------------------------------------------
t = build()
for entity_id in ["light.wled_a", "light.wled_b"]:
    t.seed(entity_id, "unavailable", NOW - 86400)
health = {row["integration"]: row for row in t.integration_health(NOW)}
check("integration health counts unavailable",
      health["wled"]["unavailable"] == 2 and health["wled"]["total"] == 3)
check("integration health counts chronic", health["wled"]["chronic"] == 2)
check("integration health computes percentage",
      abs(health["wled"]["unavailable_pct"] - 66.7) < 0.1)
check("chronic list reports both entities", len(t.chronic_entities(NOW)) == 2)
check("metrics expose per-integration counts",
      t.metrics(NOW).get("integration.wled.unavailable") == 2.0)
check("metrics expose degraded count", t.metrics(NOW).get("integrations.degraded") == 1.0)

# --- recovery -------------------------------------------------------------
t = build()
t.seed("light.wled_a", "unavailable", NOW - 86400)
t.observe("light.wled_a", "on", "unavailable", ts=NOW)
health = {row["integration"]: row for row in t.integration_health(NOW)}
check("recovery clears unavailable", health["wled"]["unavailable"] == 0)

failed = [name for name, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
