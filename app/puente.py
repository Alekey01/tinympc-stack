"""Puente entre el TERRA de la cocina y el ESP32 que decide con TinyMPC.

El ESP32 se conecta por WiFi al broker MQTT del stack y habla el mismo
protocolo de lineas que por USB (hil/servidor.h en mpc-termostato):

  tinympc/<PLACA_ID>/cmd     puente -> placa   una linea (TM, MOD, APR...)
  tinympc/<PLACA_ID>/resp    placa -> puente   la respuesta de esa linea
  tinympc/<PLACA_ID>/estado  placa -> puente   "OK,hil,listo" al arrancar

Cada minuto: lee los sensores del TERRA (API nova), se los pasa a la placa
(linea TM) y, si la placa decide un cambio y MODO_ENVIO=enviar, lo manda al
aire (API v2).

Protecciones:
  - Mientras controla, el apagado por vacio del TERRA esta desactivado (si
    no, apaga el aire que la placa encendio). Se restaura al detener el
    contenedor, durante una pausa manual y si la placa deja de responder
    PLACA_PERDIDA_MIN: el TERRA vuelve a mandar.
  - Si alguien cambia el aire desde el TERRA (pantalla o app) y no esta como
    lo dejo la placa, se le respeta PAUSA_MANUAL_MIN sin mandar nada. Con una
    sola lectura distinta ya no se manda: la decision podria deshacer el
    cambio antes de que la pausa (dos lecturas) se active.
  - Setpoint entre SP_MIN y SP_MAX; como mucho MAX_CMD_HORA comandos por hora.
  - Si la placa se reinicia, se le vuelve a cargar el modelo aprendido.

Guarda en DATOS: vivo_<fecha>.jsonl (una fila por minuto), puente.log,
modelo_cocina_aprendido.json (cada 30 min y al salir) y estado_puente.json
(ultimo comando mandado, para no confundirlo con uso manual tras reiniciar).
"""
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

import cuby

MX = timezone(timedelta(hours=-6))
DATOS = Path(os.environ.get("DATOS", "/datos"))
APP = Path(__file__).resolve().parent
MAC = os.environ.get("TERRA_MAC", "F09E9E1EB52C")
ENVIAR = os.environ.get("MODO_ENVIO", "enviar").lower() == "enviar"
PLACA_ID = os.environ.get("PLACA_ID", "cocina")
SP_USER = int(os.environ.get("SP_USUARIO", "24"))
MODO = os.environ.get("MODO_MPC", "normal")

LUZ_GENTE = 100                  # en la cocina la luz es la referencia de presencia
SP_MIN, SP_MAX = 22, 27
MAX_CMD_HORA = 6
PAUSA_MANUAL_MIN = 120
GRACIA_S = 180                   # tras un comando, lo que tarda el TERRA en reportarlo
PLACA_PERDIDA_MIN = 10
RESPUESTA_S = 30                 # un solve tarda < 1 s; 30 s es la placa caida
GUARDAR_S = 1800

MODOS = {"eco": 0, "normal": 1, "confort": 2}
# En el orden de termo_modelo_t (exportar_modelo.py en mpc-termostato)
ENTEROS = ["dt_min"]
BOOLEANOS = ["usa_t_ext", "enfria"]
REALES = ["alfa", "beta_luz", "gamma_pres", "c", "luz_escala",
          "kappa_ac", "b_ac", "u_sat", "offset_ac", "oscilacion_ac", "utc_offset_h"]
CLAVES_APRV = ["alfa", "beta_luz", "gamma_pres", "c", "kappa_ac", "b_ac", "u_sat", "offset_ac", "oscilacion_ac"]

DATOS.mkdir(parents=True, exist_ok=True)
_LOG = open(DATOS / "puente.log", "a", buffering=1)


def log(msg):
    linea = f"{datetime.now(MX):%Y-%m-%d %H:%M:%S} {msg}"
    print(linea, flush=True)
    _LOG.write(linea + "\n")


class PlacaPerdida(Exception):
    pass


class Placa:
    """Una linea de ida y una de vuelta, por MQTT."""

    def __init__(self):
        self.t_cmd = f"tinympc/{PLACA_ID}/cmd"
        self.t_resp = f"tinympc/{PLACA_ID}/resp"
        self.t_estado = f"tinympc/{PLACA_ID}/estado"
        self.respuestas = queue.Queue()
        self.reiniciada = threading.Event()
        self.reiniciada.set()            # al arrancar el puente hay que cargarle el modelo
        self.lock = threading.Lock()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"puente-{PLACA_ID}")
        self.c.username_pw_set(os.environ["MQTT_USUARIO"], os.environ["MQTT_CLAVE"])
        self.c.on_connect = self._conectado
        self.c.on_message = self._mensaje
        self.c.connect(os.environ.get("MQTT_HOST", "mosquitto"), int(os.environ.get("MQTT_PUERTO_INTERNO", "1883")))
        self.c.loop_start()

    def _conectado(self, c, *_):
        c.subscribe([(self.t_resp, 1), (self.t_estado, 1)])

    def _mensaje(self, c, u, msg):
        txt = msg.payload.decode(errors="replace").strip()
        if msg.topic == self.t_estado:
            if txt.startswith("OK,hil,listo"):
                self.reiniciada.set()
        elif msg.topic == self.t_resp:
            self.respuestas.put(txt)

    def __call__(self, linea):
        with self.lock:
            while not self.respuestas.empty():
                self.respuestas.get_nowait()
            self.c.publish(self.t_cmd, linea, qos=1)
            try:
                r = self.respuestas.get(timeout=RESPUESTA_S)
            except queue.Empty:
                raise PlacaPerdida(f"la placa no respondio a {linea[:12]}") from None
        if r.startswith("ERR"):
            raise RuntimeError(f"{r} <- {linea[:60]}")
        return r.split(",")


def linea_modelo(m, modo):
    v = [str(int(m[k])) for k in ENTEROS] + [str(int(bool(m[k]))) for k in BOOLEANOS]
    v += [f"{float(m[k]):.9g}" for k in REALES] + [str(MODOS[modo])]
    v += [f"{x:.4g}" for x in m["horario"]["laboral"] + m["horario"]["finde"]]
    return "MOD," + ",".join(v)


def modelo_de_partida():
    aprendido = DATOS / "modelo_cocina_aprendido.json"
    ruta = aprendido if aprendido.exists() else APP / "modelo_inicial.json"
    m = json.loads(ruta.read_text())
    if not m.get("enfria"):
        # El TERRA de la cocina esta en la puerta, a ~5 m del aire: no ve su
        # efecto. Se parte de un aire prestado (mediana de 12 cuartos reales)
        # y el aprendizaje en linea lo corrige.
        m.update(kappa_ac=0.02, b_ac=-0.04, u_sat=0.5, offset_ac=0.75, oscilacion_ac=0.1, enfria=True)
        log("AVISO: el modelo dice que el aire no enfria en el sensor; parto de un aire prestado")
    return m, ruta.name


def registro(lat):
    m = lat["metrics"]
    return {
        "ts": int(time.time()) // 60 * 60,
        "t": m.get("temperature"),
        "luz": m.get("lux") or 0,
        "pres": 1.0 if (m.get("lux") or 0) > LUZ_GENTE else 0.0,
        "t_ext": 0,
        "ir": {"on": int(bool(m.get("ac_power"))), "sp": int(round(m.get("ac_setpoint") or SP_USER))},
        "sp_user": SP_USER,
    }


class Puente:
    def __init__(self):
        self.placa = Placa()
        self.nova = cuby.Nova()
        self.ctl = cuby.Control() if ENVIAR else None
        self.modelo, self.origen = modelo_de_partida()
        self.occ0 = None             # como estaba el apagado por vacio del TERRA
        self.tomado = False          # el apagado por vacio esta desactivado por nosotros
        est = self._leer_estado()
        self.ultimo_enviado = est.get("ultimo_enviado")
        self.t_enviado = est.get("t_enviado", 0.0)
        self.enviados = []
        self.distinto = 0
        self.pausa_hasta = est.get("pausa_hasta", 0.0)
        self.ultimo_guardado = time.time()
        self.placa_ok_desde = time.time()

    # ---- estado persistente ----
    def _leer_estado(self):
        try:
            return json.loads((DATOS / "estado_puente.json").read_text())
        except (OSError, ValueError):
            return {}

    def _guardar_estado(self):
        (DATOS / "estado_puente.json").write_text(json.dumps(
            {"ultimo_enviado": self.ultimo_enviado, "t_enviado": self.t_enviado, "pausa_hasta": self.pausa_hasta}))

    # ---- TERRA: tomar y soltar el control ----
    def tomar(self):
        if not self.ctl or self.tomado:
            return
        if self.occ0 is None:
            self.occ0 = self.ctl.config(MAC)["config"]["automation"]["occupancy"]["enabled"]
        r = self.ctl.config_parche(MAC, {"automation": {"occupancy": {"enabled": False}}})
        self.tomado = cuby.ocupacion_efectiva(r) is False
        log(f"control tomado: apagado por vacio del TERRA = {cuby.ocupacion_efectiva(r)}")

    def soltar(self, motivo):
        if not self.ctl or not self.tomado:
            return
        r = self.ctl.config_parche(MAC, {"automation": {"occupancy": {"enabled": bool(self.occ0 if self.occ0 is not None else True)}}})
        self.tomado = False
        log(f"control devuelto al TERRA ({motivo}): apagado por vacio = {cuby.ocupacion_efectiva(r)}")

    # ---- placa ----
    def preparar_placa(self):
        self.placa.reiniciada.clear()
        log(f"placa: {','.join(self.placa('HOLA'))}")
        self.placa(linea_modelo(self.modelo, MODO))
        self.placa("APR,1")
        log(f"modelo cargado en la placa ({self.origen}, modo {MODO}); aprendizaje en linea; "
            f"{'MANDANDO al aire' if ENVIAR else 'modo sombra'}")

    def aprendido(self):
        v = self.placa("APRV")
        d = dict(zip(CLAVES_APRV, map(float, v[1:10])))
        d["enfria"] = bool(int(v[10]))
        d["cuenta"] = dict(zip(["pasivos", "encendido", "arranques", "estables", "descartados"], map(int, v[11:16])))
        return d

    def guardar_aprendido(self):
        d = self.aprendido()
        h = [float(x) for x in self.placa("APRH")[1:]]
        nuevo = dict(self.modelo)
        nuevo.update({k: v for k, v in d.items() if k != "cuenta"})
        nuevo["horario"] = {"laboral": h[:48], "finde": h[48:96]}
        nuevo["aprendizaje"] = {"cuenta": d["cuenta"], "guardado": datetime.now(MX).isoformat(timespec="seconds"),
                                "origen": self.origen}
        tmp = DATOS / "modelo_cocina_aprendido.json.tmp"
        tmp.write_text(json.dumps(nuevo, indent=1))
        tmp.replace(DATOS / "modelo_cocina_aprendido.json")
        self.modelo = nuevo                  # si la placa se reinicia, parte de aqui

    # ---- un minuto ----
    def minuto(self):
        if self.placa.reiniciada.is_set():
            self.preparar_placa()
        lat = self.nova.get(f"/devices/{MAC}/endpoints/climate/latest")
        rec = registro(lat)
        r = self.placa(f"TM,{rec['ts']},{rec['t']},{rec['luz']},{rec['pres']},0,"
                       f"{rec['ir']['on']},{rec['ir']['sp']},{rec['sp_user']}")
        self.placa_ok_desde = time.time()
        on, sp, resolvio, us, iters, nivel = int(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5]), float(r[6])

        cmd = None
        if on >= 0 or sp >= 0:
            cmd = {"power": "on" if (on == 1 or (on < 0 and rec["ir"]["on"])) else "off"}
            if cmd["power"] == "on":
                cmd.update(mode="cool", fan="auto",
                           temperature=int(min(SP_MAX, max(SP_MIN, sp if sp >= 0 else rec["ir"]["sp"]))))

        ahora = time.time()
        en_pausa = ahora < self.pausa_hasta
        if self.ctl and self.ultimo_enviado and not en_pausa and ahora - self.t_enviado > GRACIA_S:
            quiere_on = self.ultimo_enviado["power"] == "on"
            igual = rec["ir"]["on"] == int(quiere_on) and (
                not quiere_on or rec["ir"]["sp"] == self.ultimo_enviado.get("temperature"))
            self.distinto = 0 if igual else self.distinto + 1
            if self.distinto >= 2:
                self.pausa_hasta = ahora + PAUSA_MANUAL_MIN * 60
                en_pausa = True
                self.distinto = 0
                log(f"alguien cambio el aire desde el TERRA (aire {'on' if rec['ir']['on'] else 'off'}/"
                    f"{rec['ir']['sp']}; la placa habia dejado {self.ultimo_enviado}): pausa de {PAUSA_MANUAL_MIN} min")
                self.soltar("pausa por uso manual")
                self._guardar_estado()
        if self.ctl and self.pausa_hasta and not en_pausa:
            log("fin de la pausa: la placa retoma el control")
            self.pausa_hasta = 0.0
            self.ultimo_enviado = None
            self._guardar_estado()

        mandado = False
        if cmd and self.ctl and not en_pausa and self.distinto == 0:
            self.tomar()
            self.enviados = [t for t in self.enviados if t > ahora - 3600]
            if len(self.enviados) < MAX_CMD_HORA:
                self.ctl.mandar(MAC, {"type": "power", **cmd})
                self.enviados.append(ahora)
                self.ultimo_enviado = cmd
                self.t_enviado = ahora
                mandado = True
                self._guardar_estado()
        elif self.ctl and not en_pausa and not self.tomado:
            self.tomar()                  # controlar tambien es no dejar que el TERRA apague

        fila = {"rec": rec, "m": lat["metrics"],
                "placa": {"on": on, "sp": sp, "resolvio": resolvio, "us": us, "iter": iters, "nivel": nivel},
                "cmd": cmd, "mandado": mandado, "pausa_manual": en_pausa}
        if resolvio:
            fila["aprendido"] = self.aprendido()
        if time.time() - self.ultimo_guardado > GUARDAR_S:
            self.guardar_aprendido()
            self.ultimo_guardado = time.time()
        with open(DATOS / f"vivo_{datetime.now(MX):%Y%m%d}.jsonl", "a") as f:
            f.write(json.dumps(fila) + "\n")
        if resolvio:
            estado = " (MANDADO)" if mandado else (" (pausa: lo cambio una persona)" if cmd and en_pausa else
                                                   (" (sombra)" if cmd else ""))
            log(f"T={rec['t']} luz={rec['luz']:.0f} aire={'on' if rec['ir']['on'] else 'off'}/{rec['ir']['sp']}  "
                f"placa: nivel {nivel:.2f} C, {iters} iter, {us / 1000:.0f} ms -> {cmd if cmd else 'sin cambio'}{estado}")

    def correr(self):
        log(f"puente arrancado: TERRA {MAC}, placa '{PLACA_ID}', {'MANDANDO al aire' if ENVIAR else 'modo sombra'}")
        while True:
            try:
                self.minuto()
            except PlacaPerdida as e:
                caida = (time.time() - self.placa_ok_desde) / 60
                log(f"{e} (sin respuesta hace {caida:.0f} min)")
                self.placa.reiniciada.set()          # cuando vuelva, recargarle el modelo
                if caida >= PLACA_PERDIDA_MIN:
                    self.soltar(f"la placa no responde hace {caida:.0f} min")
            except Exception as e:                   # API caida, red, etc.: seguir intentando
                log(f"ERROR: {e}")
            time.sleep(max(1, 60 - time.time() % 60))

    def salir(self):
        try:
            self.guardar_aprendido()
            log("modelo aprendido guardado")
        except Exception as e:
            log(f"no se pudo guardar lo aprendido: {e}")
        try:
            self.soltar("el puente se detiene")
        except Exception as e:
            log(f"ERROR devolviendo el control al TERRA: {e}  <-- REVISAR")


def main():
    p = Puente()

    def terminar(*_):
        log("deteniendo")
        p.salir()
        sys.exit(0)

    signal.signal(signal.SIGTERM, terminar)
    signal.signal(signal.SIGINT, terminar)
    p.correr()


if __name__ == "__main__":
    main()
