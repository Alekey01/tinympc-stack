#!/usr/bin/env python3
"""Panel web del stack: entradas y salidas del TinyMPC de la cocina.

    python panel.py                       # puerto 8765 dentro del contenedor

Solo lee los archivos que escribe puente.py en DATOS (nunca escribe nada ni
llama a ninguna API). Exige usuario y contrasena (PANEL_USUARIO,
PANEL_CLAVE): muestra datos de la oficina. Sin ellas no arranca.

Rutas:
  /                          tools/panel.html
  /api/datos?fecha=AAAAMMDD  JSON con prueba, vivo, modelo, modelo_aprendido
                             y log (por defecto, hoy en hora de Ciudad de
                             Mexico)

Los archivos se leen en cada peticion: cambian mientras corre el sistema.
"""
import argparse
import base64
import hmac
import json
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MX = timezone(timedelta(hours=-6))
DATOS = Path(os.environ.get("DATOS", "/datos"))
USUARIO = os.environ.get("PANEL_USUARIO", "")
CLAVE = os.environ.get("PANEL_CLAVE", "")
HTML = Path(__file__).resolve().parent / "panel.html"
LINEAS_LOG = 40


def leer_jsonl(ruta):
    """Filas de un .jsonl; salta lineas vacias o a medio escribir."""
    if not ruta.exists():
        return []
    filas = []
    with open(ruta, encoding="utf-8", errors="replace") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                filas.append(json.loads(linea))
            except ValueError:
                continue  # la ultima puede estar a medio escribir
    return filas


def leer_json(ruta):
    if not ruta.exists():
        return None
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except ValueError:
        return None  # se esta reescribiendo justo ahora


def cola_log(ruta, n=LINEAS_LOG):
    if not ruta.exists():
        return []
    with open(ruta, encoding="utf-8", errors="replace") as f:
        return [l.rstrip("\n") for l in deque(f, maxlen=n)]


def datos(fecha):
    return {
        "fecha": fecha,
        "ahora": int(datetime.now(MX).timestamp()),
        "prueba": leer_jsonl(DATOS / f"prueba_{fecha}.jsonl"),
        "vivo": leer_jsonl(DATOS / f"vivo_{fecha}.jsonl"),
        "modelo": leer_json(DATOS / "modelo_cocina.json"),
        # lo que la placa aprendio en linea (cocina_vivo.py lo guarda cada 30 min)
        "modelo_aprendido": leer_json(DATOS / "modelo_cocina_aprendido.json"),
        "log": cola_log(DATOS / "puente.log"),
    }


class Manejador(BaseHTTPRequestHandler):
    def responder(self, codigo, cuerpo, tipo):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def autorizado(self):
        cab = self.headers.get("Authorization", "")
        if not cab.startswith("Basic "):
            return False
        try:
            u, _, c = base64.b64decode(cab[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, USUARIO) and hmac.compare_digest(c, CLAVE)

    def do_GET(self):
        if not self.autorizado():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="tinympc", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                self.responder(200, HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self.responder(500, b"no encuentro panel.html", "text/plain; charset=utf-8")
        elif url.path == "/api/datos":
            fecha = (parse_qs(url.query).get("fecha") or [""])[0] or f"{datetime.now(MX):%Y%m%d}"
            if not re.fullmatch(r"\d{8}", fecha):  # evita rutas raras en el nombre del archivo
                self.responder(400, b'{"error": "fecha debe ser AAAAMMDD"}', "application/json")
                return
            cuerpo = json.dumps(datos(fecha), ensure_ascii=False).encode("utf-8")
            self.responder(200, cuerpo, "application/json; charset=utf-8")
        else:
            self.responder(404, b"no existe", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass  # sin ruido en la terminal cada 30 s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--puerto", type=int, default=8765)
    a = ap.parse_args()
    if not USUARIO or not CLAVE:
        raise SystemExit("faltan PANEL_USUARIO / PANEL_CLAVE: el panel no arranca sin contrasena")
    srv = ThreadingHTTPServer(("0.0.0.0", a.puerto), Manejador)
    print(f"panel escuchando en el puerto {a.puerto} (con usuario y contrasena)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
