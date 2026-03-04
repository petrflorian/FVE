#!/usr/bin/with-contenv bashio

bashio::log.info "Starting FVE Solar Forecast Add-on..."
bashio::log.info "Log level: $(bashio::config 'log_level')"

# Export log level for Python application
export LOG_LEVEL="$(bashio::config 'log_level')"

# Ensure data directory exists
mkdir -p /data

exec python3 /app/main.py
