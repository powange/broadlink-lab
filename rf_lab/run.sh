#!/usr/bin/with-contenv bashio
set -e

# device_ip vide -> app.py bascule sur la découverte broadcast.
# bashio::config rend la chaîne "null" quand l'option est vide : la propager
# telle quelle ferait passer "null" pour une IP valide côté app.py
# (`os.environ.get("DEVICE_IP") or None`).
if bashio::config.has_value 'device_ip'; then
    export DEVICE_IP="$(bashio::config 'device_ip')"
    bashio::log.info "RM4 ciblé en direct : ${DEVICE_IP}"
else
    bashio::log.info "Pas de device_ip — découverte broadcast (host_network requis)"
fi

export FREQUENCY="$(bashio::config 'frequency')"
export LOG_LEVEL="$(bashio::config 'log_level')"

bashio::log.info "RF Lab démarre — ${FREQUENCY} MHz, log ${LOG_LEVEL}"

exec python3 /app/app.py
