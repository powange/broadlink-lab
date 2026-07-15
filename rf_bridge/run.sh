#!/usr/bin/with-contenv bashio
set -e

if bashio::config.has_value 'device_ip'; then
    export DEVICE_IP="$(bashio::config 'device_ip')"
    bashio::log.info "RM4 ciblé en direct : ${DEVICE_IP}"
else
    bashio::log.info "Pas de device_ip — découverte broadcast (host_network requis)"
fi

# services: mqtt:need -> le superviseur nous donne les identifiants du broker.
# Rien à saisir : c'est tout l'intérêt de déclarer le service.
export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USER="$(bashio::services mqtt 'username')"
export MQTT_PASSWORD="$(bashio::services mqtt 'password')"

export PROFILES="$(bashio::config 'profiles | join(",")')"
export LOG_LEVEL="$(bashio::config 'log_level')"
export PORT=8098

bashio::log.info "Broker MQTT ${MQTT_HOST}:${MQTT_PORT}"
exec python3 /app/bridge.py
