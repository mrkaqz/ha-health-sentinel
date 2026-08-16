#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

export SENTINEL_PROBE_INTERVAL="$(bashio::config 'probe_interval')"
export SENTINEL_SAMPLE_INTERVAL="$(bashio::config 'sample_interval')"
export SENTINEL_SLOW_INTERVAL="$(bashio::config 'slow_interval')"
export SENTINEL_RING_BUFFER_MINUTES="$(bashio::config 'ring_buffer_minutes')"
export SENTINEL_ALERT_CORE_DOWN_AFTER="$(bashio::config 'alert_core_down_after')"
export SENTINEL_RETENTION_RAW_DAYS="$(bashio::config 'retention_raw_days')"
export SENTINEL_RETENTION_ROLLUP_DAYS="$(bashio::config 'retention_rollup_days')"
export SENTINEL_DISK_FREE_WARN_PCT="$(bashio::config 'disk_free_warn_pct')"
export SENTINEL_MEMORY_WARN_PCT="$(bashio::config 'memory_warn_pct')"
export SENTINEL_LOG_LEVEL="$(bashio::config 'log_level')"

export SENTINEL_TELEGRAM_ENABLED="$(bashio::config 'telegram_enabled')"
export SENTINEL_TELEGRAM_BOT_TOKEN="$(bashio::config 'telegram_bot_token' '')"
export SENTINEL_TELEGRAM_CHAT_ID="$(bashio::config 'telegram_chat_id' '')"
export SENTINEL_WEBHOOK_URL="$(bashio::config 'webhook_url' '')"

# Supplied by the Supervisor to every add-on with hassio_api enabled.
export SENTINEL_SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Starting Health Sentinel..."

exec python3 -u /opt/sentinel/main.py
