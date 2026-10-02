#!/bin/sh
# Crea el archivo de contrasenas desde las variables del stack y arranca el broker.
set -e
if [ -z "$MQTT_USUARIO" ] || [ -z "$MQTT_CLAVE" ]; then
  echo "faltan MQTT_USUARIO / MQTT_CLAVE en el entorno del stack" >&2
  exit 1
fi
mkdir -p /mosquitto/data
mosquitto_passwd -b -c /mosquitto/data/passwd "$MQTT_USUARIO" "$MQTT_CLAVE"
chmod 0700 /mosquitto/data/passwd
chown mosquitto:mosquitto /mosquitto/data/passwd 2>/dev/null || true
exec mosquitto -c /mosquitto/config/mosquitto.conf
