"""Spike script (dev-only, NOT a pytest test, NOT part of the engine):
generates one fresh 4K piece per TV shape and cycles through every matte
each TV's own firmware reports (`get_matte_list()`), driving all three
Frame TVs (43L, 43R, 50) at the same time so a human standing in the room
can look at all three at once, take notes, and report back which matte fits
best — one choice for the two 43" panels, one for the 50 — per
KNOWN_ISSUES.md #3 and the follow-up to `spike_tv_write_path.py`
(dev_plan §3.1), which hardcoded `matte="none"` only to get a clean test
upload, not as a style decision.

This is a throwaway diagnostic, not the shape of the eventual per-TV matte
config: it doesn't touch `config/tv_deploy.toml` and doesn't persist a
choice — the human reports the two chosen matte ids back at the end, out of
band. Per the user, 43L and 43R share the same matte catalog (both queried
independently below anyway, since two physical units could still drift),
so a single round advances all three screens together; a screen whose list
is shorter than the others simply stops updating once exhausted while the
rest continue.

Usage:
    uv run python scripts/spike_matte_gallery.py [--start N]

    --start N: skip the first N rounds (e.g. to resume a session that was
    interrupted partway through) instead of starting over.

Each round uploads the same generated image (one image shared by 43L/43R,
a separate one for 50, matching each shape's aspect ratio) with the next
matte in that screen's list, selects it, prints the matte name for every
screen side by side, and waits for a single Enter before advancing all
three — so the pace is set by the person looking at the screens, not a
timer. Only one upload is kept on each TV's 'Mis Fotos' storage at a time
(old ones are deleted as we go) so a long run doesn't fill up the TVs.

Each TV's connection is opened fresh right before its upload and closed
right after, rather than held open across the whole run: confirmed live
(2026-07-13) that a `SamsungTVArt` websocket left idle 20s+ — which easily
happens while the other two TVs take their turn, plus however long the
human takes at the Enter prompt between rounds — fails its next send with
a `BrokenPipeError`, and blindly closing+reopening mid-failure once even
knocked the 50" out of full-screen art display into its on-screen Art
Store/nav menu. Reconnecting per round means no connection is ever idle
long enough to go stale in the first place.

Every TV call also runs under a hard wall-clock watchdog, not just
`samsungtvws`'s own `timeout=`. That per-call timeout only bounds a single
`recv()`; `_wait_for_d2d` keeps looping and consuming frames as long as
each individual `recv()` succeeds, even if none of them ever matches our
request (a known failure mode — see upstream
github.com/xchwarze/samsung-tv-ws-api issue #106, "stuck at upload()").
Confirmed live (2026-07-13): a round against the 50" hung silently for 50+
seconds with `timeout=15` set and no exception raised. The watchdog force-
closes the raw socket from a separate thread after `_ROUND_DEADLINE_SECONDS`
so a stuck `recv()` gets kicked with an OSError instead of hanging forever.
"""

import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from samsungtvws import exceptions
from samsungtvws.art import SamsungTVArt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import generation  # noqa: E402
from engine.tv_discovery import resolve_tv_host  # noqa: E402

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_MY_PHOTOS_CATEGORY = "MY-C0002"

# SamsungTVArt defaults to no timeout at all, so a stalled response (e.g.
# the 50" mid Art-Store-nav confusion observed live 2026-07-13) would hang
# this script forever instead of surfacing as a connection error to retry.
_TV_TIMEOUT_SECONDS = 15

# Belt-and-suspenders wall-clock cap on top of _TV_TIMEOUT_SECONDS — see
# module docstring for why samsungtvws's own timeout isn't sufficient on
# its own. Generous enough to cover a real upload+select+cleanup (observed
# ~13s end to end) with headroom.
_ROUND_DEADLINE_SECONDS = 30

_GALLERY_PROMPT = (
    "A single fine-art still life photograph of a bowl of fruit on a "
    "wooden table, warm natural light, rich texture and detail, simple "
    "uncluttered background, gallery-quality composition."
)

_CONNECTION_ERRORS = (
    exceptions.ConnectionFailure,
    exceptions.ResponseError,
    exceptions.MessageError,
)


@dataclass
class TvSession:
    """Connection *parameters*, not a live connection — see module docstring
    for why each round opens and closes its own short-lived connection
    instead of one being held for the whole run."""

    name: str  # "43L" / "43R" / "50"
    host: str
    token_file: str
    image_path: Path
    matte_ids: list[str]


def _connect(session: TvSession) -> SamsungTVArt:
    tv = SamsungTVArt(
        host=session.host,
        token_file=session.token_file,
        timeout=_TV_TIMEOUT_SECONDS,
    )
    tv.open()
    return tv


def _delete_old_uploads(tv: SamsungTVArt, keep_content_id: str, tv_name: str) -> None:
    try:
        old_ids = [
            item["content_id"]
            for item in tv.available(category=_MY_PHOTOS_CATEGORY)
            if item.get("content_id") != keep_content_id
        ]
        if old_ids:
            tv.delete_list(old_ids)
    except _CONNECTION_ERRORS as error:
        print(f"  ({tv_name}: no se pudo limpiar subidas viejas: {error})")


def _matte_ids(matte_list: dict) -> list[str]:
    """Flattens get_matte_list()'s matte_types into a list of ids, always
    including 'none' first (frameless) even if the TV doesn't list it
    explicitly as an option.

    Each entry is shaped like {"matte_type": "shadowbox"} (confirmed live
    against 43L/43R/50, 2026-07-13) — there is no separate "id" field, the
    "matte_type" value itself is the id `upload()`/`change_matte()` expect.
    """
    types_ = matte_list.get("matte_types") or []
    ids = []
    for entry in types_:
        matte_id = entry.get("matte_type") if isinstance(entry, dict) else entry
        if matte_id and matte_id not in ids:
            ids.append(str(matte_id))
    if "none" not in ids:
        ids.insert(0, "none")
    return ids


def _generate_sample(aspect_ratio: str, label: str) -> Path:
    print(f"Generando imagen de muestra {label} ({aspect_ratio})...")
    result = generation.generate_image(
        _GALLERY_PROMPT, aspect_ratio=aspect_ratio, image_size="4K"
    )
    if "error" in result:
        raise SystemExit(f"Falló la generación de {label}: {result['error']}")
    print(f"  -> {result['path']} (image_id={result['image_id']})")
    return Path(result["path"])


def _open_session(tv_key: str, tv_name: str, image_path: Path) -> TvSession | None:
    """Resolves one TV's host and matte catalog, or returns None (printing
    why) if the TV can't be reached/paired/opened right now — a single
    flaky TV (e.g. a dropped socket during handshake, observed live as
    BrokenPipeError on the 50" unit) must not crash the whole gallery run
    for the others. Only used to probe capabilities up front; the actual
    per-round connection is opened fresh each time by `_connect`.
    """
    try:
        host = resolve_tv_host(tv_name)
        token_file = str(DATA_DIR / f"tv_{tv_key}_token.spike")
        print(
            f"Abriendo conexión con {tv_name} @ {host} (aprueba 'Allow this "
            "device?' en el control si se pide)..."
        )
        tv = SamsungTVArt(host=host, token_file=token_file, timeout=_TV_TIMEOUT_SECONDS)
        tv.open()
        try:
            if not tv.supported():
                print(f"  {tv_name}: no soporta Art Mode, se omite.")
                return None
            matte_ids = _matte_ids(tv.get_matte_list())
        finally:
            tv.close()
    except (*_CONNECTION_ERRORS, OSError) as error:
        print(f"  {tv_name}: no se pudo abrir la sesión ({error}), se omite.")
        return None
    print(f"  {tv_name}: {len(matte_ids)} mattes -> {matte_ids}")
    return TvSession(
        name=tv_name,
        host=host,
        token_file=token_file,
        image_path=image_path,
        matte_ids=matte_ids,
    )


def _apply_round(session: TvSession, index: int) -> str | None:
    """Uploads/selects the matte at `index` in this session's list. Returns
    the matte id applied, or None if this session's list is already
    exhausted (nothing uploaded, screen just keeps showing its last matte).

    Opens and closes its own connection rather than reusing one held across
    rounds — see module docstring for why. Runs under a wall-clock watchdog
    thread since samsungtvws's own `timeout=` doesn't bound the total time
    a stuck `_wait_for_d2d` loop can spend (see module docstring).
    """
    if index >= len(session.matte_ids):
        return None
    matte_id = session.matte_ids[index]

    state = {"tv": None}
    error = {}

    def work() -> None:
        try:
            tv = _connect(session)
            state["tv"] = tv
            try:
                content_id = tv.upload(
                    str(session.image_path), matte=matte_id, portrait_matte=matte_id
                )
                tv.select_image(content_id, show=True)
                _delete_old_uploads(
                    tv, keep_content_id=content_id, tv_name=session.name
                )
            finally:
                try:
                    tv.close()
                except (*_CONNECTION_ERRORS, OSError):
                    pass
        except (*_CONNECTION_ERRORS, OSError, ValueError) as exc:
            error["exc"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(_ROUND_DEADLINE_SECONDS)

    if worker.is_alive():
        # Force the raw socket closed from here so the worker's stuck
        # recv() raises and the thread can exit (upstream issue #106).
        tv = state.get("tv")
        if tv is not None and tv.connection is not None:
            try:
                tv.connection.sock.close()
            except OSError:
                pass
        worker.join(5)
        print(
            f"  ({session.name}: sin respuesta tras {_ROUND_DEADLINE_SECONDS}s "
            f"aplicando matte={matte_id!r}, conexión forzada a cerrar)"
        )
        return None

    if "exc" in error:
        print(f"  ({session.name}: falló aplicar matte={matte_id!r}: {error['exc']})")
        return None

    return matte_id


def main() -> None:
    args = sys.argv[1:]
    start_index = 0
    if "--start" in args:
        start_index = int(args[args.index("--start") + 1])

    image_43 = _generate_sample("9:16", '43" (43L/43R)')
    image_50 = _generate_sample("16:9", '50"')

    candidates = [
        ("43l", "43L", image_43),
        ("43r", "43R", image_43),
        ("50", "50", image_50),
    ]
    sessions = []
    for tv_key, tv_name, image_path in candidates:
        session = _open_session(tv_key, tv_name, image_path)
        if session is not None:
            sessions.append(session)

    if not sessions:
        raise SystemExit("Ninguna TV pudo abrir sesión, nada que hacer.")

    max_rounds = max(len(s.matte_ids) for s in sessions)
    last_index = start_index
    try:
        for i in range(start_index, max_rounds):
            last_index = i
            print(f"\n=== Ronda {i + 1}/{max_rounds} ===")
            for session in sessions:
                applied = _apply_round(session, i)
                if applied is None and i >= len(session.matte_ids):
                    print(
                        f"  {session.name}: sin más opciones (se queda en "
                        f"'{session.matte_ids[-1]}')."
                    )
                else:
                    print(f"  {session.name}: matte = {applied!r}")
            input(
                "\n  >>> Anota los nombres de arriba. Presiona Enter para la "
                "siguiente ronda (Ctrl+C para salir)..."
            )
    except KeyboardInterrupt:
        print(f"\nInterrumpido en la ronda {last_index}.")
        print(f"Para retomar: uv run python {sys.argv[0]} --start {last_index}")

    print(
        "\nListo. Cuando tengas tus notas, dime el matte elegido para "
        "43L/43R y el elegido para 50."
    )


if __name__ == "__main__":
    main()
