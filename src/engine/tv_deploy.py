"""Despliegue de arte ya finalizado a las Samsung Frame TVs (PRD §3.3, §7.6):
subir una imagen aprobada, seleccionarla en Art Mode, y limpiar subidas
viejas para no llenar la memoria de la TV. Construido sobre el spike de
3.1/3.2 (`scripts/spike_tv_write_path.py`), que ya validó `upload()`/
`select_image()` contra las tres TVs reales sin diferencias de protocolo.

Agnóstico de TV por diseño (funciona por nombre, "43L"/"43R"/"50") — el
spike de 3.1 ya validó que la Frame 50 (protocolo legacy 2.03) no difiere
de las 43" en upload()/select_image(), así que 3.4 se limitó a invocar
este mismo camino también para la 50 desde el flujo en vivo del agente,
sin código de manejo especial.

Reversibilidad (PRD §7.6, dev_plan §3.5): cada despliegue exitoso se
registra en `engine.deploy_history` (un solo nivel: current + previous
por TV). `revert_tv`/`revert_panels` reutilizan `deploy_image_to_tv` para
volver a subir/seleccionar el image_id anterior — no hay un "undo" nativo
en la TV, revertir es simplemente desplegar hacia atrás.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface.
"""

import concurrent.futures
import logging
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from samsungtvws import exceptions
from samsungtvws.art import SamsungTVArt

from engine import deploy_history, generation
from engine.tv_discovery import TvNotFoundError, resolve_tv_host

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "tv_deploy.toml"
)
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# "MY-C0002": categoría de contenido subido por nosotros ("Mis Fotos"), no
# el contenido de fábrica de la TV. SamsungTVArt.available(category=...)
# compara este valor tal cual contra el campo 'category_id' de cada item
# (nunca lo construye a partir de un int, a diferencia de otros métodos de
# la librería como set_auto_rotation_status) — hay que pasar el string
# completo, no el sufijo numérico, o el filtro no matchea nada (confirmado
# en vivo: con category=2 el filtro no borraba ninguna subida vieja).
_MY_PHOTOS_CATEGORY = "MY-C0002"

_CONNECTION_ERRORS = (
    exceptions.ConnectionFailure,
    exceptions.ResponseError,
    exceptions.MessageError,
)

_logger = logging.getLogger(__name__)

# SamsungTVArt defaults to no timeout at all, so an unresponsive TV would
# block deploy_image_to_tv forever. _TV_TIMEOUT_SECONDS bounds each
# individual recv(); _DEPLOY_DEADLINE_SECONDS is an additional wall-clock
# cap on the whole open+upload+select+cleanup sequence, because the
# library's own _wait_for_d2d loop can keep spinning past the per-call
# timeout if the TV emits frames that never match our request (confirmed
# live 2026-07-13 against the 50" TV — see docs/matte_investigation.md and
# upstream samsung-tv-ws-api issue #106). Module-level so tests can shrink
# them instead of waiting the real deadline.
_TV_TIMEOUT_SECONDS = 15
_DEPLOY_DEADLINE_SECONDS = 30

# Extra wait after forcing the stuck socket closed, to give the worker
# thread a chance to actually unblock and finish (successfully or not)
# before giving up on it entirely. The real worst-case block time for
# deploy_image_to_tv is _DEPLOY_DEADLINE_SECONDS + _FORCE_CLOSE_GRACE_SECONDS,
# not just _DEPLOY_DEADLINE_SECONDS.
_FORCE_CLOSE_GRACE_SECONDS = 5


@dataclass
class TvDeployConfig:
    matte: dict[str, str]


def load_tv_deploy_config(path: Path | None = None) -> TvDeployConfig:
    """Reads the per-TV matte setting from an editable TOML file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return TvDeployConfig(matte=data["matte"])


def _delete_old_uploads(tv: SamsungTVArt, keep_content_id: str, tv_name: str) -> None:
    """Borra todo el contenido de 'Mis Fotos' en `tv` salvo
    `keep_content_id` (PRD §7.6). Nunca lanza: una falla de limpieza se
    registra y se ignora — la imagen nueva ya está mostrándose, así que
    esto es housekeeping incompleto, no una falla de despliegue.
    """
    try:
        old_ids = [
            item["content_id"]
            for item in tv.available(category=_MY_PHOTOS_CATEGORY)
            if item.get("content_id") != keep_content_id
        ]
        if old_ids:
            tv.delete_list(old_ids)
    except _CONNECTION_ERRORS as error:
        _logger.warning("No se pudo limpiar subidas viejas en %s: %s", tv_name, error)


def _resolve_deploy_target(tv_name: str, image_id: str) -> tuple[Path, str, str] | dict:
    """Resuelve las precondiciones comunes a cualquier operación de subida
    (con o sin selección en pantalla): que la imagen exista en disco, el
    host de la TV, y el matte configurado para `tv_name`. Devuelve
    `(image_path, host, matte)` o `{'error': ...}` si algo falla — nunca
    lanza. Compartido por `deploy_image_to_tv`/`upload_image_to_category`
    para no duplicar estas ~15 líneas entre ambas.
    """
    image_path = generation.IMAGES_DIR / f"{image_id}.jpg"
    if not image_path.exists():
        return {"error": f"No existe una imagen con image_id={image_id!r}."}

    try:
        host = resolve_tv_host(tv_name)
    except TvNotFoundError as error:
        _logger.error("No se pudo resolver la TV %s: %s", tv_name, error)
        return {"error": str(error)}

    try:
        config = load_tv_deploy_config()
        matte = config.matte[tv_name]
    except (KeyError, TypeError) as error:
        return {
            "error": (
                f"Configuración de matte inválida para la TV {tv_name!r}: {error}"
            )
        }

    return image_path, host, matte


def _run_with_deploy_watchdog(
    tv_name: str,
    tv: SamsungTVArt,
    work: Callable[[dict, threading.Event], None],
) -> dict:
    """Orquesta un `work(outcome, abandoned)` bajo el mismo watchdog de
    reloj que ya protegía `deploy_image_to_tv` (_DEPLOY_DEADLINE_SECONDS):
    la propia librería samsungtvws puede quedarse esperando una respuesta
    indefinidamente incluso con timeout= puesto (bug upstream, ver
    docs/matte_investigation.md) — sin este tope, una TV que no responde
    colgaría el llamado para siempre. Extraído para que
    `upload_image_to_category` (dev_plan_phase_2.md §4.1) comparta esta
    protección sin duplicar el manejo de hilo/timeout/cierre forzado.

    `work` recibe `outcome` (debe escribir su resultado final en
    `outcome["result"] = {...}`) y `abandoned` (un `threading.Event` que
    `work` debe consultar antes de mutar cualquier estado compartido al
    final de un camino de éxito — si ya está puesto, el watchdog ya dio
    por perdido el intento y el caller pudo haber seguido su curso, p. ej.
    reintentando; `work` es responsable de su propio `tv.close()`, este
    helper no abre ni cierra la conexión). Devuelve {'error': ...} en
    cualquier tipo de falla; nunca lanza.

    Sigue leyendo `_DEPLOY_DEADLINE_SECONDS`/`_FORCE_CLOSE_GRACE_SECONDS`
    del namespace del módulo en cada llamada (no como parámetros con
    default) para que los tests puedan seguir monkeypatcheándolas.
    """
    outcome: dict = {}
    abandoned = threading.Event()

    worker = threading.Thread(target=work, args=(outcome, abandoned), daemon=True)
    worker.start()
    worker.join(_DEPLOY_DEADLINE_SECONDS)

    if worker.is_alive():
        # Bloqueado dentro de recv() (bug upstream, ver
        # docs/matte_investigation.md): forzar el cierre del socket crudo
        # desde afuera es la única forma de destrabarlo. Si el hang ocurre
        # durante el propio handshake de open() (antes de que
        # samsungtvws asigne tv.connection), no hay socket que forzar —
        # se distingue en el log para no reportar un cierre que nunca
        # ocurrió.
        sock = getattr(getattr(tv, "connection", None), "sock", None)
        forced_close = False
        if sock is not None:
            forced_close = True
            try:
                sock.close()
            except OSError:
                pass
        worker.join(_FORCE_CLOSE_GRACE_SECONDS)

        if not worker.is_alive():
            # El cierre forzado (o el propio trabajo) destrabó al worker
            # dentro del período de gracia — si terminó con éxito, ese
            # resultado real no debe descartarse a favor de un timeout
            # genérico.
            return outcome.get(
                "result",
                {"error": (f"Fallo inesperado en la TV {tv_name!r} (sin resultado).")},
            )

        abandoned.set()
        if forced_close:
            _logger.error(
                "Sin respuesta de la TV %s tras %ss, conexión forzada a cerrar",
                tv_name,
                _DEPLOY_DEADLINE_SECONDS,
            )
        else:
            _logger.error(
                "Sin respuesta de la TV %s tras %ss; la conexión nunca "
                "llegó a establecerse, no hay socket que forzar a cerrar",
                tv_name,
                _DEPLOY_DEADLINE_SECONDS,
            )
        return {
            "error": (
                f"La TV {tv_name!r} no respondió a tiempo "
                f"({_DEPLOY_DEADLINE_SECONDS}s); puede haber quedado en un "
                f"estado inconsistente."
            )
        }

    return outcome.get(
        "result",
        {"error": f"Fallo inesperado en la TV {tv_name!r} (sin resultado)."},
    )


def deploy_image_to_tv(tv_name: str, image_id: str) -> dict:
    """Sube la imagen `image_id` a la TV `tv_name` ('43L'/'43R'/'50') y la
    selecciona en pantalla (PRD §3.3), limpiando después subidas viejas de
    'Mis Fotos' (PRD §7.6). Nunca borra antes de que la subida nueva haya
    tenido éxito, para que una falla a medias nunca deje la TV sin arte.

    Corre bajo un watchdog de reloj (_run_with_deploy_watchdog,
    _DEPLOY_DEADLINE_SECONDS): la propia librería samsungtvws puede
    quedarse esperando una respuesta indefinidamente incluso con timeout=
    puesto (bug upstream, ver docs/matte_investigation.md) — sin este
    tope, una TV que no responde colgaría este llamado para siempre.

    Devuelve {'content_id': ...} o {'error': '<mensaje>'} — nunca lanza.
    """
    _logger.info("Desplegando a %s: image_id=%s", tv_name, image_id)

    resolved = _resolve_deploy_target(tv_name, image_id)
    if isinstance(resolved, dict):
        return resolved
    image_path, host, matte = resolved

    token_file = DATA_DIR / f"tv_{tv_name.lower()}_token.json"
    tv = SamsungTVArt(
        host=host, token_file=str(token_file), timeout=_TV_TIMEOUT_SECONDS
    )

    def work(outcome: dict, abandoned: threading.Event) -> None:
        try:
            try:
                tv.open()
            except _CONNECTION_ERRORS as error:
                _logger.warning("No se pudo conectar con la TV %s: %s", tv_name, error)
                outcome["result"] = {
                    "error": f"No se pudo conectar con la TV {tv_name!r}: {error}"
                }
                return

            if not tv.supported():
                _logger.warning("La TV %s no soporta Art Mode", tv_name)
                outcome["result"] = {"error": f"La TV {tv_name!r} no soporta Art Mode."}
                return

            try:
                content_id = tv.upload(
                    str(image_path), matte=matte, portrait_matte=matte
                )
            except (*_CONNECTION_ERRORS, OSError, ValueError) as error:
                _logger.warning("Falló la subida a la TV %s: %s", tv_name, error)
                outcome["result"] = {
                    "error": f"Falló la subida a la TV {tv_name!r}: {error}"
                }
                return

            try:
                tv.select_image(content_id, show=True)
            except _CONNECTION_ERRORS as error:
                _logger.warning(
                    "La imagen subió pero no se pudo mostrar en %s: %s",
                    tv_name,
                    error,
                )
                outcome["result"] = {
                    "error": (
                        f"La imagen subió pero no se pudo mostrar en "
                        f"{tv_name!r}: {error}"
                    )
                }
                return

            if abandoned.is_set():
                # El watchdog ya reportó timeout y el caller siguió su
                # curso (posiblemente reintentando) — mutar deploy_history
                # o borrar subidas viejas ahora correría contra ese
                # reintento en paralelo, así que este resultado tardío se
                # descarta sin tocar estado compartido.
                _logger.warning(
                    "La TV %s terminó tarde, después del timeout del "
                    "watchdog; se descarta el resultado sin registrar "
                    "historial (image_id=%s content_id=%s)",
                    tv_name,
                    image_id,
                    content_id,
                )
                return

            _delete_old_uploads(tv, keep_content_id=content_id, tv_name=tv_name)
            deploy_history.record_deploy(tv_name, image_id)
            _logger.info(
                "Desplegado con éxito en %s: image_id=%s content_id=%s",
                tv_name,
                image_id,
                content_id,
            )
            outcome["result"] = {"content_id": content_id}
        except Exception as error:  # red de seguridad: corre sin supervisión
            _logger.exception("Fallo inesperado desplegando a %s", tv_name)
            outcome["result"] = {
                "error": f"Fallo inesperado desplegando a la TV {tv_name!r}: {error}"
            }
        finally:
            try:
                tv.close()
            except Exception as error:
                _logger.debug("Cierre de conexión falló para %s: %s", tv_name, error)

    return _run_with_deploy_watchdog(tv_name, tv, work)


def upload_image_to_category(tv_name: str, image_id: str) -> dict:
    """Sube la imagen `image_id` a la categoría 'Mis Fotos' de la TV
    `tv_name` SIN seleccionarla en pantalla y SIN borrar subidas viejas
    (dev_plan_phase_2.md §4.1) — a diferencia de `deploy_image_to_tv`, que
    hace ambas cosas porque modela "esto es lo que se muestra ahora".
    Pensada para poblar la categoría con las N imágenes de un lote antes
    de configurar la rotación nativa (Etapa 4.2): mostrar o limpiar
    contenido a mitad de una carga por lote sería activamente incorrecto
    (la TV parpadearía entre hasta N imágenes, y cada subida borraría todo
    lo subido del lote hasta ese momento).

    Mismo andamiaje de watchdog que `deploy_image_to_tv`
    (`_run_with_deploy_watchdog`) — una TV sin responder no debe colgar
    este llamado para siempre. Nunca lanza; devuelve {'content_id': ...}
    o {'error': '<mensaje>'}.

    No escribe en `deploy_history`: esa tabla modela "qué se está
    mostrando ahora mismo" para el revert de pieza suelta (PRD §7.6), y no
    tiene sentido durante una subida por lote donde nada se selecciona
    todavía.

    Riesgo conocido, documentado aquí y diferido explícitamente al cierre
    de 4.2 (no se arregla en esta iteración): un despliegue de pieza
    suelta posterior (`deploy_image_to_tv`/`deploy_set_to_panels`)
    seguiría llamando `_delete_old_uploads`, que borraría TODO el
    contenido de 'Mis Fotos' salvo la imagen recién desplegada —
    incluyendo cualquier galería de lote activa subida por esta función.
    4.2 debe decidir cómo coexisten ambos flujos antes de cerrar Etapa 4.
    """
    _logger.info(
        "Subiendo a 'Mis Fotos' de %s (sin seleccionar): image_id=%s",
        tv_name,
        image_id,
    )

    resolved = _resolve_deploy_target(tv_name, image_id)
    if isinstance(resolved, dict):
        return resolved
    image_path, host, matte = resolved

    token_file = DATA_DIR / f"tv_{tv_name.lower()}_token.json"
    tv = SamsungTVArt(
        host=host, token_file=str(token_file), timeout=_TV_TIMEOUT_SECONDS
    )

    def work(outcome: dict, abandoned: threading.Event) -> None:
        del abandoned  # nada que proteger: no hay estado compartido que mutar
        try:
            try:
                tv.open()
            except _CONNECTION_ERRORS as error:
                _logger.warning("No se pudo conectar con la TV %s: %s", tv_name, error)
                outcome["result"] = {
                    "error": f"No se pudo conectar con la TV {tv_name!r}: {error}"
                }
                return

            if not tv.supported():
                _logger.warning("La TV %s no soporta Art Mode", tv_name)
                outcome["result"] = {"error": f"La TV {tv_name!r} no soporta Art Mode."}
                return

            try:
                content_id = tv.upload(
                    str(image_path), matte=matte, portrait_matte=matte
                )
            except (*_CONNECTION_ERRORS, OSError, ValueError) as error:
                _logger.warning("Falló la subida a la TV %s: %s", tv_name, error)
                outcome["result"] = {
                    "error": f"Falló la subida a la TV {tv_name!r}: {error}"
                }
                return

            _logger.info(
                "Subida a 'Mis Fotos' con éxito en %s: image_id=%s content_id=%s",
                tv_name,
                image_id,
                content_id,
            )
            outcome["result"] = {"content_id": content_id}
        except Exception as error:  # red de seguridad: corre sin supervisión
            _logger.exception("Fallo inesperado subiendo a %s", tv_name)
            outcome["result"] = {
                "error": f"Fallo inesperado subiendo a la TV {tv_name!r}: {error}"
            }
        finally:
            try:
                tv.close()
            except Exception as error:
                _logger.debug("Cierre de conexión falló para %s: %s", tv_name, error)

    return _run_with_deploy_watchdog(tv_name, tv, work)


def deploy_set_to_panels(image_43l: str, image_43r: str, image_50: str) -> dict:
    """Despliega el conjunto aprobado a las tres TVs de la casa (PRD §3.3,
    §3.4).

    A diferencia de generate_set_diptico/generate_set_split (que se
    detienen en el primer error porque son un pipeline secuencial de
    generación), las tres TVs son dispositivos físicos independientes:
    la falla de una nunca debe impedir que las demás reciban su arte.
    Los tres despliegues se intentan siempre.

    Devuelve {'43L': {...}, '43R': {...}, '50': {...}}, cada valor el
    resultado de deploy_image_to_tv para esa pantalla. Los tres despliegues
    corren en paralelo (no secuencialmente) — cada uno ya tiene su propio
    watchdog interno (_DEPLOY_DEADLINE_SECONDS), así que encadenarlos uno
    tras otro solo multiplicaría por tres el peor caso de latencia sin
    ninguna razón, dado que son dispositivos físicos independientes.
    """
    images = {"43L": image_43l, "43R": image_43r, "50": image_50}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(images)) as executor:
        futures = {
            tv_name: executor.submit(deploy_image_to_tv, tv_name, image_id)
            for tv_name, image_id in images.items()
        }
        results = {tv_name: future.result() for tv_name, future in futures.items()}
    summary = {
        tv_name: ("error" if "error" in result else "ok")
        for tv_name, result in results.items()
    }
    _logger.info("Despliegue de conjunto completo: %s", summary)
    return results


def revert_tv(tv_name: str) -> dict:
    """Revierte la TV `tv_name` a la versión que tenía desplegada justo
    antes de la actual (PRD §7.6, dev_plan §3.5) — un solo nivel de
    historial, no una pila: revertir dos veces seguidas alterna entre las
    dos últimas versiones, ya que `deploy_image_to_tv` vuelve a registrar
    historial al desplegar la anterior.

    Devuelve {'error': ...} sin tocar la TV si no hay una versión anterior
    guardada (nunca se ha desplegado, o solo se ha desplegado una vez).
    En caso contrario, reutiliza deploy_image_to_tv tal cual —mismo shape
    de resultado, misma independencia de fallas— para desplegar el
    image_id anterior.
    """
    history = deploy_history.get_history(tv_name)
    if history is None or history.previous_image_id is None:
        _logger.info("Revert sin historial previo para %s", tv_name)
        return {
            "error": f"No hay una versión anterior guardada para la TV {tv_name!r}."
        }
    return deploy_image_to_tv(tv_name, history.previous_image_id)


def revert_panels(tv_names: list[str]) -> dict:
    """Revierte cada TV en `tv_names` de forma independiente (PRD §7.6,
    dev_plan §3.5) — a diferencia de deploy_set_to_panels, que siempre
    actúa sobre las tres pantallas, esto acepta cualquier subconjunto
    (p. ej. solo las que sí cambiaron en un despliegue parcial). La falla
    de una TV nunca bloquea a las demás.

    Devuelve {tv_name: {...}, ...} solo para los nombres pedidos.
    """
    return {tv_name: revert_tv(tv_name) for tv_name in tv_names}
