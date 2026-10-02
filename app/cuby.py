"""Cliente minimo de las APIs de Cuby.

  - nova.cuby.mx/v1: LECTURA (sensores del TERRA).
  - cuby.cloud/api/v2: comandos al aire y config del TERRA.

La cuenta viene de las variables CUBY_USUARIO y CUBY_CLAVE del stack. Este
modulo nunca imprime credenciales ni tokens. Los tokens se renuevan solos
antes de vencer.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

NOVA = "https://nova.cuby.mx/v1"
V2 = "https://cuby.cloud/api/v2"


def credenciales():
    u, c = os.environ.get("CUBY_USUARIO"), os.environ.get("CUBY_CLAVE")
    if not u or not c:
        raise SystemExit("faltan CUBY_USUARIO / CUBY_CLAVE en el entorno del stack")
    return u, c


def _pedir(metodo, url, cuerpo=None, token=None, timeout=30):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    req.add_header("Accept", "application/json")
    if datos is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{metodo} {url.split('?')[0]} -> HTTP {e.code}: "
                           f"{e.read().decode(errors='replace')[:300]}") from None


class Nova:
    """Lectura. POST /v1/session da un token Bearer; se renueva cada 6 h."""

    RENOVAR_S = 6 * 3600

    def __init__(self):
        self._entrar()

    def _entrar(self):
        u, c = credenciales()
        r = _pedir("POST", f"{NOVA}/session", {"account": u, "password": c})
        self.token = (r or {}).get("token")
        if not self.token:
            raise RuntimeError("la sesion de nova no devolvio token")
        self.desde = time.time()

    def get(self, ruta, **params):
        if time.time() - self.desde > self.RENOVAR_S:
            self._entrar()
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        try:
            return _pedir("GET", f"{NOVA}{ruta}{q}", token=self.token)
        except RuntimeError as e:
            if "HTTP 401" not in str(e):
                raise
            self._entrar()           # sesion vencida: entrar otra vez y reintentar
            return _pedir("GET", f"{NOVA}{ruta}{q}", token=self.token)


class Control:
    """Escritura por la v2. El token va en la cabecera, nunca en la URL."""

    DURACION_S = 6 * 3600

    def __init__(self):
        self._entrar()

    def _entrar(self):
        u, c = credenciales()
        r = _pedir("POST", f"{V2}/token/{urllib.parse.quote(u)}", {"password": c, "expiration": self.DURACION_S})
        self.token = (r or {}).get("token")
        if not self.token:
            raise RuntimeError("/token de la v2 no devolvio token")
        self.desde = time.time()

    def _pedir(self, metodo, ruta, cuerpo=None):
        if time.time() - self.desde > self.DURACION_S - 600:
            self._entrar()
        return _pedir(metodo, f"{V2}{ruta}", cuerpo, token=self.token)

    def estado(self, mac):
        return self._pedir("GET", f"/state/{mac}")

    def mandar(self, mac, acstate):
        return self._pedir("POST", f"/state/{mac}", acstate)

    def config(self, mac):
        return self._pedir("GET", f"/devices/{mac}/config")

    def config_parche(self, mac, cambio):
        """cambio: arbol parcial. Responde con lo aplicado en 'effective'."""
        return self._pedir("PATCH", f"/devices/{mac}/config", {"config": cambio})


def ocupacion_efectiva(r):
    arbol = (r or {}).get("effective") or (r or {}).get("config") or {}
    return arbol.get("automation", {}).get("occupancy", {}).get("enabled")
