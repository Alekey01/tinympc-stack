# tinympc-stack

El ESP32 decide con TinyMPC qué hacer con el aire de la cocina; este stack es
su puente con las APIs de Cuby y sirve el panel para verlo.

```
APIs de Cuby  ←HTTPS→  stack (este repo)  ←MQTT por WiFi→  ESP32
                       · puente: lee el TERRA,        · decide con TinyMPC
                         manda al aire                · aprende en línea
                       · mosquitto: broker MQTT
                       · panel: la página
```

Las credenciales de Cuby viven aquí (variables del stack), nunca en la placa.
El código de la placa y del controlador está en el proyecto `mpc-termostato`
(firmware ESP-IDF, `hil/servidor.cpp`, `firmware/components/mpc_termo`).

## Servicios

| servicio | qué hace |
|---|---|
| `mosquitto` | broker MQTT con usuario y contraseña. El ESP32 se conecta aquí. |
| `puente` | cada minuto lee los sensores del TERRA (API nova), se los pasa a la placa y manda al aire lo que decide (API v2). |
| `panel` | la página: entradas, decisiones del ESP32 y modelo aprendido. Pide usuario y contraseña. |

## Variables

Ver `.env.example`. En Komodo van en **Environment** del stack; nunca en el repo.

| variable | |
|---|---|
| `CUBY_USUARIO`, `CUBY_CLAVE` | cuenta de Cuby dueña del TERRA |
| `TERRA_MAC` | el TERRA que se controla |
| `MODO_ENVIO` | `enviar` (por defecto) manda al aire; `sombra` solo registra |
| `MQTT_USUARIO`, `MQTT_CLAVE`, `MQTT_PUERTO` | acceso del ESP32 al broker |
| `PLACA_ID` | topics `tinympc/<PLACA_ID>/cmd`, `/resp`, `/estado` |
| `PANEL_USUARIO`, `PANEL_CLAVE`, `PANEL_PUERTO` | acceso al panel |

**Solo un sistema debe controlar el aire a la vez.** Antes de desplegar con
`MODO_ENVIO=enviar`, detener cualquier otro lazo que le hable a la misma placa
o al mismo TERRA.

## Protecciones del puente

- Mientras controla desactiva el apagado por vacío del TERRA, y lo restaura al
  detener el contenedor, durante una pausa manual y si la placa no responde
  10 min. En esos casos el TERRA vuelve a mandar.
- Si alguien cambia el aire desde el TERRA, se le respeta 2 h sin mandar nada.
- Setpoint entre 22 y 27 °C; como mucho 6 comandos por hora.
- Si la placa se reinicia, le vuelve a cargar el modelo aprendido.

## Datos

El volumen `datos` guarda `vivo_<fecha>.jsonl`, `puente.log`,
`modelo_cocina_aprendido.json` y `estado_puente.json`. Son datos de la
oficina: no se suben al repo.

## Probar sin la placa

`mpc-termostato/hil/placa_mqtt.py` se conecta al broker como lo haría el ESP32
y responde con el mismo código C++ de la placa (`hil_host`).
