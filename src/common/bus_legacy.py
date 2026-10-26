"""FUENTE UNICA del cierre de ESCRITURA del bus viejo (orden de Otixe, 2026-09-15).

Por que existe este modulo
--------------------------
El bus tiene DOS implementaciones de publicacion que NO comparten una linea de
codigo (medido por @saruman-palantir el 2026-09-15):

  1. REST  `gateway/app.py::bus_publish_ep`  (POST /bus/publish)
  2. MCP   `mcp/mcp_server.py::bus_publish`  (tool)

El panel de Sinapsis NO es una tercera: `server.py::api_bus_publish` es un
pass-through (`Response(content=r.content, status_code=r.status_code, ...)`) al
gateway, asi que gatear el REST lo cubre y su codigo de estado se propaga tal cual.

Si el mensaje o el codigo se copiaran a mano en cada puerta, "identico en ambas"
seria una promesa de disciplina. Aca es una propiedad estructural: las dos puertas
importan ESTE modulo y emiten LA MISMA cadena (`MESSAGE`), construida a partir del
mismo `STATUS`. No hay forma de que divergan sin que el test de identidad se ponga
rojo.

Sigue el patron ya validado para `tasks_rbac.py` (tarea c6b61bd1) y `bus_notify.py`
(tarea 2929a26a): el modulo vive en `common/` y se HORNEA en cada imagen via
`additional_contexts` + `COPY --from=common`.

QUE NO HACE ESTE MODULO
-----------------------
- NO toca las LECTURAS. `bus_inbox`, `bus_ack`, `bus_thread`, `bus_threads` y
  `bus_group` siguen vivos y sin cambios: las bandejas viejas se siguen consultando
  y drenando.
- NO toca `bus_notify.notify_assignment()`. Es el aviso automatico de asignacion de
  tareas y hoy es la UNICA via por la que un agente se entera de que le derivaron
  trabajo; cerrarlo sin migrarlo a Correspondencia dejaria a la flota sin aviso.
  Excepcion DELIBERADA y declarada (Saruman, 2026-09-15), no un olvido: se migra en
  otra tarea. Consecuencia honesta: `bus_messages` sigue creciendo por esa via.
- NO borra datos, filas, tools ni rutas. La ruta y el tool siguen registrados y
  siguen respondiendo; lo que cambia es QUE responden.

REVERSIBILIDAD (por configuracion, sin tocar codigo)
----------------------------------------------------
`BUS_LEGACY_PUBLISH` con default CERRADO. Se lee EN CADA LLAMADA, no al importar:
asi el estado del gate no queda congelado en el arranque del proceso y un test
puede ejercerlo en el mismo proceso sin trucos.

FAIL-CLOSED, y es lo que hace que el default valga: solo una lista EXPLICITA de
tokens abre (`OPEN_TOKENS`). Ausente, vacia, con espacios, con otro valor, no-str o
ilegible (bytes no-UTF8 llegan como surrogates via surrogateescape y no matchean) ->
CERRADO. Cualquier excepcion al leer el entorno -> CERRADO.
"""

import os

# Nombre de la variable. Un solo lugar: el mensaje de error la nombra desde aca, asi
# que el texto que lee un agente NUNCA puede quedar desincronizado del nombre real.
ENV_FLAG = "BUS_LEGACY_PUBLISH"

# Allowlist de apertura. Todo lo que no este aca cierra.
OPEN_TOKENS = frozenset({"open", "abierto", "1", "true", "yes", "on", "si"})

# 410 Gone y NO otro codigo. Justificacion (RFC 9110 15.5.11):
#   - 410 dice "este recurso se retiro a proposito, deja de pedirlo". Es la unica
#     familia 4xx que comunica RETIRO en vez de "probaste mal" o "volve despues".
#   - 503 seria MENTIRA operativa: implica reintento, y un cliente bien educado
#     reintentaria una escritura que nunca va a entrar. Un retry-loop indefinido.
#   - 403 dice "no tenes permiso": mandaria a pedir permisos que no existen.
#   - 405 exige cabecera Allow y significa "ese metodo no va en este recurso";
#     POST sigue siendo el metodo correcto de la ruta, no es un error de metodo.
#   - 404 borraria la evidencia de que la ruta existio, y ademas es falso: existe.
# Que sea reversible por env var NO lo vuelve temporal para el cliente: la reapertura
# es una palanca de OPERACION (rollback), no una promesa de disponibilidad.
STATUS = 410

# Reemplazo, nombrado en las DOS superficies para que el mensaje sirva igual en
# cualquiera de las dos.
REPLACEMENT_REST = "POST /correspondencia/publicar"
REPLACEMENT_MCP = "correspondencia_publicar"

# Superficies de LECTURA del bus. Retiradas en F9 (2026-10-05) salvo para los lectores
# de archivo; se enumeran para que el mensaje diga exactamente que se cerro.
READ_SURFACES = ("bus_inbox", "bus_ack", "bus_thread", "bus_threads", "bus_group")

# LA cadena. Identica en REST y en MCP porque es la MISMA constante, no una copia.
MESSAGE = (
    "el bus viejo quedo RETIRADO como canal: publicar esta cerrado "
    f"(HTTP {STATUS} Gone). Usa Correspondencia para publicar: "
    f"REST {REPLACEMENT_REST} / MCP tool {REPLACEMENT_MCP}. "
    "Las LECTURAS del bus tambien quedaron retiradas (F9, 2026-10-05): tu historial "
    "esta en el Archivo de Correspondencia con origen 'bus-legacy'. "
    f"Reversible sin tocar codigo: {ENV_FLAG}=open en el entorno del servicio "
    "(default: CERRADO; cualquier valor no reconocido tambien cierra)."
)


def publish_open(env=None) -> bool:
    """True SOLO si el entorno dice EXPLICITAMENTE que el bus esta abierto.

    `env` existe para los tests (un mapping propio); en produccion se lee
    `os.environ` en cada llamada. Fail-closed en todo lo demas.
    """
    try:
        source = os.environ if env is None else env
        raw = source.get(ENV_FLAG, "")
        if not isinstance(raw, str):
            return False
        return raw.strip().lower() in OPEN_TOKENS
    except Exception:
        # Entorno ilegible => cerrado. Un gate que se cae abierto no es un gate.
        return False


def publish_closed(env=None) -> bool:
    """Complemento explicito de `publish_open`, para que el call site se lea igual
    que la politica ("si el publish esta cerrado, rechazar") y nadie tenga que
    acordarse de invertir el booleano."""
    return not publish_open(env)


# ==== F9 (2026-10-05, tarea 7a208866): retiro de las LECTURAS del bus ====
# Mismo patron que publish: ni la ruta ni el tool se borran, responden 410 con puntero
# al reemplazo. El historial ya esta copiado a Correspondencia (F7, origen 'bus-legacy').
# Lectores de archivo (Otixe, el panel de Sinapsis y Monica) conservan la lectura.
READ_ENV_FLAG = "BUS_LEGACY_READ"
ARCHIVE_READERS_ENV = "BUS_ARCHIVE_READERS"
ARCHIVE_READERS_DEFAULT = "admin"

READ_MESSAGE = (
    f"el bus viejo quedo RETIRADO como canal (HTTP {STATUS} Gone): sus lecturas ("
    + ", ".join(READ_SURFACES) +
    ") ya no estan disponibles. Tu historial esta en el Archivo de Correspondencia "
    "con origen 'bus-legacy': leelo con correspondencia_bandeja(archivo=True) y "
    "correspondencia_leer. Para escribir: correspondencia_publicar. "
    f"Reversible sin tocar codigo: {READ_ENV_FLAG}=open en el entorno del servicio."
)


def read_open(env=None) -> bool:
    """True SOLO si el entorno reabre EXPLICITAMENTE las lecturas. Fail-closed."""
    try:
        source = os.environ if env is None else env
        raw = source.get(READ_ENV_FLAG, "")
        if not isinstance(raw, str):
            return False
        return raw.strip().lower() in OPEN_TOKENS
    except Exception:
        return False


def read_closed(agent, env=None) -> bool:
    """True si a `agent` hay que responderle 410. Los lectores de archivo pasan."""
    if read_open(env):
        return False
    try:
        source = os.environ if env is None else env
        raw = source.get(ARCHIVE_READERS_ENV, ARCHIVE_READERS_DEFAULT)
        if not isinstance(raw, str):
            raw = ARCHIVE_READERS_DEFAULT
        readers = {a.strip().lower() for a in raw.split(",") if a.strip()}
    except Exception:
        readers = set()
    return (agent or "").strip().lower() not in readers
