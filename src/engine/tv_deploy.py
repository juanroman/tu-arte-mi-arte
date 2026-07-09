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

import logging
import tomllib
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


@dataclass
class TvDeployConfig:
    matte: str


def load_tv_deploy_config(path: Path | None = None) -> TvDeployConfig:
    """Reads the fixed house matte setting from an editable TOML file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return TvDeployConfig(**data)


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


def deploy_image_to_tv(tv_name: str, image_id: str) -> dict:
    """Sube la imagen `image_id` a la TV `tv_name` ('43L'/'43R'/'50') y la
    selecciona en pantalla (PRD §3.3), limpiando después subidas viejas de
    'Mis Fotos' (PRD §7.6). Nunca borra antes de que la subida nueva haya
    tenido éxito, para que una falla a medias nunca deje la TV sin arte.

    Devuelve {'content_id': ...} o {'error': '<mensaje>'} — nunca lanza.
    """
    image_path = generation.IMAGES_DIR / f"{image_id}.jpg"
    if not image_path.exists():
        return {"error": f"No existe una imagen con image_id={image_id!r}."}

    try:
        host = resolve_tv_host(tv_name)
    except TvNotFoundError as error:
        return {"error": str(error)}

    config = load_tv_deploy_config()
    token_file = DATA_DIR / f"tv_{tv_name.lower()}_token.json"
    tv = SamsungTVArt(host=host, token_file=str(token_file))

    try:
        try:
            tv.open()
        except _CONNECTION_ERRORS as error:
            return {"error": f"No se pudo conectar con la TV {tv_name!r}: {error}"}

        if not tv.supported():
            return {"error": f"La TV {tv_name!r} no soporta Art Mode."}

        try:
            content_id = tv.upload(
                str(image_path), matte=config.matte, portrait_matte=config.matte
            )
        except (*_CONNECTION_ERRORS, OSError, ValueError) as error:
            return {"error": f"Falló la subida a la TV {tv_name!r}: {error}"}

        try:
            tv.select_image(content_id, show=True)
        except _CONNECTION_ERRORS as error:
            return {
                "error": (
                    f"La imagen subió pero no se pudo mostrar en "
                    f"{tv_name!r}: {error}"
                )
            }

        _delete_old_uploads(tv, keep_content_id=content_id, tv_name=tv_name)
        deploy_history.record_deploy(tv_name, image_id)
        return {"content_id": content_id}
    except Exception as error:  # red de seguridad: corre sin supervisión
        _logger.exception("Fallo inesperado desplegando a %s", tv_name)
        return {"error": f"Fallo inesperado desplegando a la TV {tv_name!r}: {error}"}
    finally:
        tv.close()


def deploy_set_to_panels(image_43l: str, image_43r: str, image_50: str) -> dict:
    """Despliega el conjunto aprobado a las tres TVs de la casa (PRD §3.3,
    §3.4).

    A diferencia de generate_set_diptico/generate_set_split (que se
    detienen en el primer error porque son un pipeline secuencial de
    generación), las tres TVs son dispositivos físicos independientes:
    la falla de una nunca debe impedir que las demás reciban su arte.
    Los tres despliegues se intentan siempre.

    Devuelve {'43L': {...}, '43R': {...}, '50': {...}}, cada valor el
    resultado de deploy_image_to_tv para esa pantalla.
    """
    return {
        "43L": deploy_image_to_tv("43L", image_43l),
        "43R": deploy_image_to_tv("43R", image_43r),
        "50": deploy_image_to_tv("50", image_50),
    }


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
