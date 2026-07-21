import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image
from samsungtvws import exceptions

from engine import deploy_history, generation, tv_deploy, tv_discovery
from engine.tv_deploy import (
    TvDeployConfig,
    clear_photos_category,
    configure_batch_rotation,
    deploy_image_to_tv,
    deploy_set_to_panels,
    load_tv_deploy_config,
    revert_panels,
    revert_tv,
    upload_image_to_category,
)
from engine.tv_discovery import TvNotFoundError


class _FakeSamsungTVArt:
    def __init__(
        self,
        host,
        token_file=None,
        timeout=None,
        open_error=None,
        supported=True,
        upload_error=None,
        select_error=None,
        available_error=None,
        existing_content=None,
        content_id="MY_F0001",
        hang_seconds=None,
        open_hang_seconds=None,
        slideshow_status_error=None,
        slideshow_status_result="ok-slideshow",
        auto_rotation_status_result="ok-auto-rotation",
    ):
        self.host = host
        self.token_file = token_file
        self.timeout = timeout
        self._open_error = open_error
        self._supported = supported
        self._upload_error = upload_error
        self._select_error = select_error
        self._available_error = available_error
        self._existing_content = (
            existing_content if existing_content is not None else []
        )
        self._content_id = content_id
        self._hang_seconds = hang_seconds
        self._open_hang_seconds = open_hang_seconds
        self._slideshow_status_error = slideshow_status_error
        self._slideshow_status_result = slideshow_status_result
        self._auto_rotation_status_result = auto_rotation_status_result

        self.connection = None
        self.closed = False
        self.uploaded: list[tuple[str, str, str]] = []
        self.selected: list[str] = []
        self.deleted: list[str] = []
        self.slideshow_status_calls: list[dict] = []
        self.auto_rotation_status_calls: list[dict] = []

    def open(self):
        if self._open_hang_seconds is not None:
            # Mirrors samsungtvws's own handshake loop: self.connection is
            # only assigned once the (possibly hanging) recv loop returns —
            # never mid-hang.
            time.sleep(self._open_hang_seconds)
        if self._open_error is not None:
            raise self._open_error

    def close(self):
        self.closed = True

    def supported(self):
        return self._supported

    def upload(self, path, matte=None, portrait_matte=None):
        if self._hang_seconds is not None:
            time.sleep(self._hang_seconds)
        if self._upload_error is not None:
            raise self._upload_error
        self.uploaded.append((path, matte, portrait_matte))
        return self._content_id

    def select_image(self, content_id, show=True):
        if self._select_error is not None:
            raise self._select_error
        self.selected.append(content_id)

    def available(self, category=None):
        # Mimics the real SamsungTVArt.available(): compares `category`
        # verbatim against each item's 'category_id' field (a string like
        # "MY-C0002"), never builds it from an int — a real bug caught in
        # live testing was passing an int here, which silently matched
        # nothing.
        if self._available_error is not None:
            raise self._available_error
        if not category:
            return self._existing_content
        return [
            item
            for item in self._existing_content
            if item.get("category_id") == category
        ]

    def delete_list(self, content_ids):
        self.deleted.extend(content_ids)
        return True

    def set_slideshow_status(self, duration=0, type=True, category=2, category_id=None):
        if self._slideshow_status_error is not None:
            raise self._slideshow_status_error
        self.slideshow_status_calls.append(
            {"duration": duration, "type": type, "category_id": category_id}
        )
        return self._slideshow_status_result

    def set_auto_rotation_status(
        self, duration=0, type=True, category=2, category_id=None
    ):
        self.auto_rotation_status_calls.append(
            {"duration": duration, "type": type, "category_id": category_id}
        )
        return self._auto_rotation_status_result


def _write_fixture_image(images_dir: Path, image_id: str) -> None:
    images_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), color="blue").save(
        images_dir / f"{image_id}.jpg", format="JPEG"
    )


def _install_fake(monkeypatch, tmp_path, **kwargs):
    fake = _FakeSamsungTVArt(host="10.0.0.1", **kwargs)
    monkeypatch.setattr(tv_deploy, "SamsungTVArt", lambda **_: fake)
    monkeypatch.setattr(tv_deploy, "resolve_tv_host", lambda name: "10.0.0.1")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        deploy_history, "DB_PATH", tmp_path / "tv_deploy_history.sqlite3"
    )
    return fake


def test_deploy_image_to_tv_success_uploads_selects_and_returns_content_id(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path, content_id="MY_F0099")

    result = deploy_image_to_tv("43L", "img_0001")

    assert result == {"content_id": "MY_F0099"}
    assert fake.selected == ["MY_F0099"]
    assert fake.closed is True


def test_deploy_image_to_tv_reports_missing_image(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    def _fail_resolve(name):
        raise AssertionError("resolve_tv_host should not run without a local image")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _fail_resolve)

    result = deploy_image_to_tv("43L", "img_does_not_exist")

    assert "error" in result


def test_deploy_image_to_tv_rejects_malformed_image_id(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    def _fail_resolve(name):
        raise AssertionError("resolve_tv_host should not run for an invalid image_id")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _fail_resolve)

    result = deploy_image_to_tv("43L", "../../../etc/passwd")

    assert "error" in result
    assert "inválido" in result["error"]


def test_deploy_image_to_tv_converts_tv_not_found_to_error_dict(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    def _raise_not_found(name):
        raise TvNotFoundError(f"No hay TV {name!r}")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _raise_not_found)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result


def test_deploy_image_to_tv_reports_connection_failure_on_open(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(
        monkeypatch, tmp_path, open_error=exceptions.ConnectionFailure("no conecta")
    )

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert fake.closed is True


def test_deploy_image_to_tv_logs_warning_on_connection_failure(
    tmp_path, monkeypatch, caplog
):
    """A deploy failure must not just come back as an error dict — it
    should also leave a WARNING in journalctl, since the returned dict is
    only visible to whoever handles the tool call, not to someone reading
    logs remotely.
    """
    _write_fixture_image(tmp_path, "img_0001")
    _install_fake(
        monkeypatch, tmp_path, open_error=exceptions.ConnectionFailure("no conecta")
    )

    with caplog.at_level(logging.WARNING, logger="engine.tv_deploy"):
        result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert any(
        record.levelno == logging.WARNING and "43L" in record.message
        for record in caplog.records
    )


def test_deploy_image_to_tv_reports_unsupported_tv(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path, supported=False)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert fake.uploaded == []


def test_deploy_image_to_tv_reports_upload_failure(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(
        monkeypatch, tmp_path, upload_error=exceptions.ResponseError("falló subida")
    )

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert fake.selected == []


def test_deploy_image_to_tv_reports_select_failure(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    _install_fake(
        monkeypatch,
        tmp_path,
        select_error=exceptions.MessageError("no se pudo mostrar"),
    )

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result


def test_deploy_image_to_tv_cleans_up_old_uploads_excluding_new_one(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(
        monkeypatch,
        tmp_path,
        content_id="MY_new",
        existing_content=[
            {"content_id": "MY_old1", "category_id": "MY-C0002"},
            {"content_id": "MY_old2", "category_id": "MY-C0002"},
            {"content_id": "SAM-F0201", "category_id": "MY-C0008"},
        ],
    )

    result = deploy_image_to_tv("43L", "img_0001")

    assert result == {"content_id": "MY_new"}
    assert sorted(fake.deleted) == ["MY_old1", "MY_old2"]


def test_deploy_image_to_tv_does_not_crash_when_an_item_lacks_content_id(
    tmp_path, monkeypatch
):
    """The old-uploads comprehension filtered with item.get("content_id")
    but then indexed with item["content_id"] -- an item missing that key
    passes the "not the one to keep" filter and then blows up on the
    bracket access, breaking deploy_image_to_tv's documented "nunca lanza"
    guarantee.
    """
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(
        monkeypatch,
        tmp_path,
        content_id="MY_new",
        existing_content=[
            {"content_id": "MY_old1", "category_id": "MY-C0002"},
            {"category_id": "MY-C0002"},
        ],
    )

    result = deploy_image_to_tv("43L", "img_0001")

    assert result == {"content_id": "MY_new"}
    assert fake.deleted == ["MY_old1"]


def test_deploy_image_to_tv_skips_delete_call_when_nothing_to_clean(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path, existing_content=[])

    deploy_image_to_tv("43L", "img_0001")

    assert fake.deleted == []


def test_deploy_image_to_tv_cleanup_failure_does_not_fail_the_deploy(
    tmp_path, monkeypatch, caplog
):
    _write_fixture_image(tmp_path, "img_0001")
    _install_fake(
        monkeypatch,
        tmp_path,
        content_id="MY_new",
        available_error=exceptions.ConnectionFailure("se cayó"),
    )

    with caplog.at_level(logging.WARNING):
        result = deploy_image_to_tv("43L", "img_0001")

    assert result == {"content_id": "MY_new"}


def test_deploy_image_to_tv_uses_per_tv_token_file_path(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(tv_deploy, "resolve_tv_host", lambda name: "10.0.0.1")
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)

    captured = {}

    def _fake_factory(**kwargs):
        captured.update(kwargs)
        return _FakeSamsungTVArt(host=kwargs["host"])

    monkeypatch.setattr(tv_deploy, "SamsungTVArt", _fake_factory)

    deploy_image_to_tv("43L", "img_0001")

    assert captured["token_file"] == str(tmp_path / "tv_43l_token.json")


def test_deploy_image_to_tv_uses_the_matte_configured_for_that_tv(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    config_path = tmp_path / "tv_deploy.toml"
    config_path.write_text(
        '[matte]\n"43L" = "shadowbox_warm"\n"43R" = "modern_warm"\n'
        '"50" = "flexible"\n'
    )
    monkeypatch.setattr(tv_deploy, "CONFIG_PATH", config_path)
    fake = _install_fake(monkeypatch, tmp_path)

    deploy_image_to_tv("50", "img_0001")

    assert fake.uploaded[-1][1] == "flexible"
    assert fake.uploaded[-1][2] == "flexible"


def test_deploy_image_to_tv_reports_missing_matte_for_unknown_tv(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    config_path = tmp_path / "tv_deploy.toml"
    config_path.write_text('[matte]\n"43L" = "shadowbox_warm"\n')
    monkeypatch.setattr(tv_deploy, "CONFIG_PATH", config_path)
    fake = _install_fake(monkeypatch, tmp_path)

    result = deploy_image_to_tv("50", "img_0001")

    assert "error" in result
    assert fake.uploaded == []


def test_deploy_image_to_tv_returns_error_instead_of_raising_when_matte_table_missing(
    tmp_path, monkeypatch
):
    """A tv_deploy.toml with no [matte] table at all (e.g. mid-edit, or a
    merge conflict) must surface as {'error': ...}, not crash the caller —
    deploy_image_to_tv's docstring promises it 'nunca lanza'.
    """
    _write_fixture_image(tmp_path, "img_0001")
    config_path = tmp_path / "tv_deploy.toml"
    config_path.write_text("# sin sección [matte]\n")
    monkeypatch.setattr(tv_deploy, "CONFIG_PATH", config_path)
    fake = _install_fake(monkeypatch, tmp_path)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert fake.uploaded == []


def test_deploy_image_to_tv_returns_error_instead_of_raising_for_legacy_matte_config(
    tmp_path, monkeypatch
):
    """A tv_deploy.toml still in the old pre-per-TV format (a bare string,
    not a table) must also surface as {'error': ...} instead of raising a
    TypeError when indexed by tv_name.
    """
    _write_fixture_image(tmp_path, "img_0001")
    config_path = tmp_path / "tv_deploy.toml"
    config_path.write_text('matte = "shadowbox_warm"\n')
    monkeypatch.setattr(tv_deploy, "CONFIG_PATH", config_path)
    fake = _install_fake(monkeypatch, tmp_path)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert fake.uploaded == []


def test_deploy_image_to_tv_returns_error_instead_of_raising_when_tvs_config_corrupt(
    tmp_path, monkeypatch
):
    """A corrupt config/tvs.toml (e.g. truncated mid-write by a crash — see
    tv_discovery._save_last_known_ip) must surface as {'error': ...}, not
    propagate tomllib.TOMLDecodeError out of a function documented as
    'nunca lanza'. resolve_tv_host is NOT stubbed here (unlike
    _install_fake's default) so the real load_tv_configs runs against the
    corrupt file.
    """
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)
    corrupt_config = tmp_path / "tvs.toml"
    corrupt_config.write_text("not valid toml [")
    monkeypatch.setattr(tv_discovery, "CONFIG_PATH", corrupt_config)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result


def test_deploy_image_to_tv_returns_error_instead_of_raising_when_tvs_config_missing(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)
    monkeypatch.setattr(tv_discovery, "CONFIG_PATH", tmp_path / "does_not_exist.toml")

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result


def test_deploy_image_to_tv_returns_error_when_tv_deploy_config_corrupt(
    tmp_path, monkeypatch
):
    """Same failure mode as the tvs.toml case above, but for
    config/tv_deploy.toml (the matte config) -- also loaded with a bare
    tomllib.load with no TOMLDecodeError handling around it.
    """
    _write_fixture_image(tmp_path, "img_0001")
    _install_fake(monkeypatch, tmp_path)
    corrupt_config = tmp_path / "tv_deploy.toml"
    corrupt_config.write_text("not valid toml [")
    monkeypatch.setattr(tv_deploy, "CONFIG_PATH", corrupt_config)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result


def test_deploy_image_to_tv_passes_timeout_to_samsungtvart(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(tv_deploy, "resolve_tv_host", lambda name: "10.0.0.1")
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)

    captured = {}

    def _fake_factory(**kwargs):
        captured.update(kwargs)
        return _FakeSamsungTVArt(host=kwargs["host"])

    monkeypatch.setattr(tv_deploy, "SamsungTVArt", _fake_factory)

    deploy_image_to_tv("43L", "img_0001")

    assert captured["timeout"] == tv_deploy._TV_TIMEOUT_SECONDS


def test_deploy_image_to_tv_times_out_on_unresponsive_tv(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, hang_seconds=2)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert "no respondió" in result["error"]


def test_deploy_image_to_tv_logs_error_when_watchdog_times_out(
    tmp_path, monkeypatch, caplog
):
    """The deploy watchdog (§ docs/matte_investigation.md) is the symptom
    of a TV possibly left in an inconsistent state — it must log at ERROR
    (not just return an error dict), since this is exactly the kind of
    hang someone away from home would need journalctl to explain.
    """
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, hang_seconds=2)

    with caplog.at_level(logging.ERROR, logger="engine.tv_deploy"):
        result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert any(
        record.levelno == logging.ERROR and "43L" in record.message
        for record in caplog.records
    )


def test_deploy_image_to_tv_returns_success_when_worker_finishes_during_grace_period(
    tmp_path, monkeypatch
):
    """If the forced socket-close unblocks the worker before the grace
    period (_FORCE_CLOSE_GRACE_SECONDS) elapses, and the worker actually
    completed the deploy successfully, that success must not be discarded
    in favor of the generic timeout error — a caller (e.g. the Telegram
    bot's revert-button flow) must not be told a deploy failed when it
    actually succeeded.
    """
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 3)
    fake = _install_fake(monkeypatch, tmp_path, content_id="MY_late", hang_seconds=0.3)

    result = deploy_image_to_tv("43L", "img_0001")

    assert result == {"content_id": "MY_late"}
    assert fake.selected == ["MY_late"]


def test_deploy_image_to_tv_abandoned_worker_does_not_record_history_after_timeout(
    tmp_path, monkeypatch
):
    """Once the watchdog gives up and reports a timeout, a worker that
    later finishes in the background must not mutate shared state (deploy
    history, cleanup) behind the caller's back — the caller has already
    moved on and may have retried.
    """
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, hang_seconds=2)

    result = deploy_image_to_tv("43L", "img_0001")
    assert "error" in result

    time.sleep(2.5)

    history = deploy_history.get_history(
        "43L", path=tmp_path / "tv_deploy_history.sqlite3"
    )
    assert history is None


def test_deploy_image_to_tv_logs_distinct_message_when_hang_precedes_connection(
    tmp_path, monkeypatch, caplog
):
    """If the hang happens inside open()'s own handshake (before
    tv.connection is ever assigned, mirroring samsungtvws's real
    behavior), there is no socket to force-close — the log must say so
    distinctly instead of claiming a connection was forced closed when
    nothing was.
    """
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, open_hang_seconds=2)

    with caplog.at_level(logging.ERROR, logger="engine.tv_deploy"):
        result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert any(
        record.levelno == logging.ERROR
        and "43L" in record.message
        and "forzada a cerrar" not in record.message
        for record in caplog.records
    )


def test_deploy_image_to_tv_worst_case_wait_is_deadline_plus_grace_constant(
    tmp_path, monkeypatch
):
    """The real worst-case block time is _DEPLOY_DEADLINE_SECONDS plus a
    named, overridable _FORCE_CLOSE_GRACE_SECONDS constant, not a hidden
    inline literal — a hang that exceeds their sum must still time out.
    """
    assert hasattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS")

    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, hang_seconds=2)

    result = deploy_image_to_tv("43L", "img_0001")

    assert "error" in result
    assert "no respondió" in result["error"]


def test_upload_image_to_category_success_uploads_without_selecting_or_deleting(
    tmp_path, monkeypatch
):
    """dev_plan_phase_2.md §4.1: a diferencia de deploy_image_to_tv, esta
    función solo puebla 'Mis Fotos' -- nunca selecciona la imagen en
    pantalla ni borra subidas viejas, porque durante una subida por lote
    mostrar/limpiar a mitad de camino sería activamente incorrecto.
    """
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path, content_id="MY_F0099")

    result = upload_image_to_category("43L", "img_0001")

    assert result == {"content_id": "MY_F0099"}
    assert fake.selected == []
    assert fake.deleted == []
    assert fake.uploaded == [
        (str(tmp_path / "img_0001.jpg"), "shadowbox_warm", "shadowbox_warm")
    ]
    assert fake.closed is True


def test_upload_image_to_category_does_not_record_deploy_history(tmp_path, monkeypatch):
    """No tiene sentido registrar 'qué se está mostrando ahora' (esa es la
    semántica de deploy_history, usada por revert_tv) cuando esta función
    nunca selecciona nada en pantalla.
    """
    _write_fixture_image(tmp_path, "img_0001")
    _install_fake(monkeypatch, tmp_path, content_id="MY_F0099")

    upload_image_to_category("43L", "img_0001")

    history = deploy_history.get_history(
        "43L", path=tmp_path / "tv_deploy_history.sqlite3"
    )
    assert history is None


def test_upload_image_to_category_reports_missing_image(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    def _fail_resolve(name):
        raise AssertionError("resolve_tv_host should not run without a local image")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _fail_resolve)

    result = upload_image_to_category("43L", "img_does_not_exist")

    assert "error" in result


def test_upload_image_to_category_converts_tv_not_found_to_error_dict(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    def _raise_not_found(name):
        raise TvNotFoundError(f"No hay TV {name!r}")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _raise_not_found)

    result = upload_image_to_category("43L", "img_0001")

    assert "error" in result


def test_upload_image_to_category_reports_connection_failure_on_open(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(
        monkeypatch, tmp_path, open_error=exceptions.ConnectionFailure("no conecta")
    )

    result = upload_image_to_category("43L", "img_0001")

    assert "error" in result
    assert fake.closed is True


def test_upload_image_to_category_reports_unsupported_tv(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path, supported=False)

    result = upload_image_to_category("43L", "img_0001")

    assert "error" in result
    assert fake.uploaded == []


def test_upload_image_to_category_reports_upload_failure(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(
        monkeypatch, tmp_path, upload_error=exceptions.ResponseError("falló subida")
    )

    result = upload_image_to_category("43L", "img_0001")

    assert "error" in result
    assert fake.closed is True


def test_upload_image_to_category_uses_the_matte_configured_for_that_tv(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path)

    upload_image_to_category("50", "img_0001")

    assert fake.uploaded == [
        (str(tmp_path / "img_0001.jpg"), "shadowbox_warm", "shadowbox_warm")
    ]


def test_upload_image_to_category_returns_error_when_tvs_config_corrupt(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)
    corrupt_config = tmp_path / "tvs.toml"
    corrupt_config.write_text("not valid toml [")
    monkeypatch.setattr(tv_discovery, "CONFIG_PATH", corrupt_config)

    result = upload_image_to_category("43L", "img_0001")

    assert "error" in result


def test_upload_image_to_category_times_out_on_unresponsive_tv(tmp_path, monkeypatch):
    """La función nueva comparte el mismo watchdog extraído
    (_run_with_deploy_watchdog) que deploy_image_to_tv -- una TV sin
    responder no debe colgar este llamado para siempre tampoco aquí.
    """
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, hang_seconds=2)

    result = upload_image_to_category("43L", "img_0001")

    assert "error" in result
    assert "no respondió" in result["error"]


def test_upload_image_to_category_logs_error_when_watchdog_times_out(
    tmp_path, monkeypatch, caplog
):
    _write_fixture_image(tmp_path, "img_0001")
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, hang_seconds=2)

    with caplog.at_level(logging.ERROR, logger="engine.tv_deploy"):
        result = upload_image_to_category("43L", "img_0001")

    assert "error" in result
    assert any(
        record.levelno == logging.ERROR and "43L" in record.message
        for record in caplog.records
    )


def test_clear_photos_category_deletes_all_existing_content(tmp_path, monkeypatch):
    """dev_plan_phase_2.md §4.2: a diferencia de _delete_old_uploads usada
    por deploy_image_to_tv (que siempre preserva la imagen recién
    subida), clear_photos_category no tiene ninguna que preservar --
    borra absolutamente todo lo que ya estaba subido.
    """
    fake = _install_fake(
        monkeypatch,
        tmp_path,
        existing_content=[
            {"content_id": "MY_F0001", "category_id": "MY-C0002"},
            {"content_id": "MY_F0002", "category_id": "MY-C0002"},
        ],
    )

    result = clear_photos_category("43L")

    assert result == {"cleared": True}
    assert sorted(fake.deleted) == ["MY_F0001", "MY_F0002"]
    assert fake.closed is True


def test_clear_photos_category_deletes_items_even_when_some_lack_content_id(
    tmp_path, monkeypatch
):
    """keep_content_id=None means "delete everything" -- an item missing
    content_id has nothing to delete by id, but it must not block deletion
    of the other items that do have one.
    """
    fake = _install_fake(
        monkeypatch,
        tmp_path,
        existing_content=[
            {"content_id": "MY_F0001", "category_id": "MY-C0002"},
            {"category_id": "MY-C0002"},
        ],
    )

    result = clear_photos_category("43L")

    assert result == {"cleared": True}
    assert fake.deleted == ["MY_F0001"]


def test_clear_photos_category_reports_no_content_without_error(tmp_path, monkeypatch):
    fake = _install_fake(monkeypatch, tmp_path, existing_content=[])

    result = clear_photos_category("43L")

    assert result == {"cleared": True}
    assert fake.deleted == []


def test_clear_photos_category_converts_tv_not_found_to_error_dict(
    tmp_path, monkeypatch
):
    def _raise_not_found(name):
        raise TvNotFoundError(f"No hay TV {name!r}")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _raise_not_found)

    result = clear_photos_category("43L")

    assert "error" in result


def test_clear_photos_category_reports_connection_failure_on_open(
    tmp_path, monkeypatch
):
    fake = _install_fake(
        monkeypatch, tmp_path, open_error=exceptions.ConnectionFailure("no conecta")
    )

    result = clear_photos_category("43L")

    assert "error" in result
    assert fake.closed is True


def test_clear_photos_category_reports_unsupported_tv(tmp_path, monkeypatch):
    fake = _install_fake(monkeypatch, tmp_path, supported=False)

    result = clear_photos_category("43L")

    assert "error" in result
    assert fake.deleted == []


def test_clear_photos_category_returns_error_instead_of_raising_when_tvs_config_corrupt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)
    corrupt_config = tmp_path / "tvs.toml"
    corrupt_config.write_text("not valid toml [")
    monkeypatch.setattr(tv_discovery, "CONFIG_PATH", corrupt_config)

    result = clear_photos_category("43L")

    assert "error" in result


def test_clear_photos_category_times_out_on_unresponsive_tv(tmp_path, monkeypatch):
    """Comparte el mismo watchdog extraído (_run_with_deploy_watchdog) que
    el resto de las funciones de este módulo.
    """
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, open_hang_seconds=2)

    result = clear_photos_category("43L")

    assert "error" in result
    assert "no respondió" in result["error"]


def test_configure_batch_rotation_uses_new_slideshow_api_on_success(
    tmp_path, monkeypatch
):
    fake = _install_fake(monkeypatch, tmp_path)

    result = configure_batch_rotation("43L", 1440, False)

    assert result == {"result": "ok-slideshow"}
    assert fake.slideshow_status_calls == [
        {"duration": 1440, "type": False, "category_id": "MY-C0002"}
    ]
    assert fake.auto_rotation_status_calls == []
    assert fake.closed is True


def test_configure_batch_rotation_falls_back_to_legacy_auto_rotation_api(
    tmp_path, monkeypatch
):
    """Algunas TVs (protocolo legacy, PRD §3.2) no soportan la API nueva
    de slideshow y responden con ResponseError -- mismo patrón de
    fallback que el propio CLI de samsungtvws (art-slideshow-set).
    """
    fake = _install_fake(
        monkeypatch,
        tmp_path,
        slideshow_status_error=exceptions.ResponseError("no soportado"),
    )

    result = configure_batch_rotation("50", 1440, False)

    assert result == {"result": "ok-auto-rotation"}
    assert fake.auto_rotation_status_calls == [
        {"duration": 1440, "type": False, "category_id": "MY-C0002"}
    ]


def test_configure_batch_rotation_converts_tv_not_found_to_error_dict(
    tmp_path, monkeypatch
):
    def _raise_not_found(name):
        raise TvNotFoundError(f"No hay TV {name!r}")

    monkeypatch.setattr(tv_deploy, "resolve_tv_host", _raise_not_found)

    result = configure_batch_rotation("43L", 1440, False)

    assert "error" in result


def test_configure_batch_rotation_reports_connection_failure_on_open(
    tmp_path, monkeypatch
):
    fake = _install_fake(
        monkeypatch, tmp_path, open_error=exceptions.ConnectionFailure("no conecta")
    )

    result = configure_batch_rotation("43L", 1440, False)

    assert "error" in result
    assert fake.closed is True


def test_configure_batch_rotation_reports_unsupported_tv(tmp_path, monkeypatch):
    fake = _install_fake(monkeypatch, tmp_path, supported=False)

    result = configure_batch_rotation("43L", 1440, False)

    assert "error" in result
    assert fake.slideshow_status_calls == []


def test_configure_batch_rotation_returns_error_when_tvs_config_corrupt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(tv_deploy, "DATA_DIR", tmp_path)
    corrupt_config = tmp_path / "tvs.toml"
    corrupt_config.write_text("not valid toml [")
    monkeypatch.setattr(tv_discovery, "CONFIG_PATH", corrupt_config)

    result = configure_batch_rotation("43L", 1440, False)

    assert "error" in result


def test_configure_batch_rotation_times_out_on_unresponsive_tv(tmp_path, monkeypatch):
    monkeypatch.setattr(tv_deploy, "_DEPLOY_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(tv_deploy, "_FORCE_CLOSE_GRACE_SECONDS", 0.1)
    _install_fake(monkeypatch, tmp_path, open_hang_seconds=2)

    result = configure_batch_rotation("43L", 1440, False)

    assert "error" in result
    assert "no respondió" in result["error"]


def test_deploy_set_to_panels_deploys_all_three_independently_on_success(monkeypatch):
    calls = []

    def _fake_deploy(tv_name, image_id):
        calls.append((tv_name, image_id))
        return {"content_id": f"MY_{tv_name}"}

    monkeypatch.setattr(tv_deploy, "deploy_image_to_tv", _fake_deploy)

    result = deploy_set_to_panels("img_left", "img_right", "img_wide")

    assert result == {
        "43L": {"content_id": "MY_43L"},
        "43R": {"content_id": "MY_43R"},
        "50": {"content_id": "MY_50"},
    }
    # No se garantiza el orden de finalización una vez que los tres
    # despliegues corren concurrentemente — solo que los tres ocurrieron.
    assert sorted(calls) == sorted(
        [
            ("43L", "img_left"),
            ("43R", "img_right"),
            ("50", "img_wide"),
        ]
    )


def test_deploy_set_to_panels_one_tv_failure_does_not_block_the_others(monkeypatch):
    def _fake_deploy(tv_name, image_id):
        if tv_name == "50":
            return {"error": "no se pudo conectar"}
        return {"content_id": f"MY_{tv_name}"}

    monkeypatch.setattr(tv_deploy, "deploy_image_to_tv", _fake_deploy)

    result = deploy_set_to_panels("img_left", "img_right", "img_wide")

    assert "error" in result["50"]
    assert result["43L"] == {"content_id": "MY_43L"}
    assert result["43R"] == {"content_id": "MY_43R"}


def test_deploy_set_to_panels_runs_deploys_concurrently(monkeypatch):
    """The three TVs are independent physical devices (per this function's
    own docstring) — the three deploys must overlap, not run one after the
    other, or a single unresponsive TV triples the worst-case latency of
    the whole set.
    """

    def _fake_deploy(tv_name, image_id):
        time.sleep(0.3)
        return {"content_id": f"MY_{tv_name}"}

    monkeypatch.setattr(tv_deploy, "deploy_image_to_tv", _fake_deploy)

    start = time.monotonic()
    deploy_set_to_panels("img_left", "img_right", "img_wide")
    elapsed = time.monotonic() - start

    assert elapsed < 0.6


def test_load_tv_deploy_config_reads_house_config():
    config = load_tv_deploy_config()

    assert config.matte["43L"] == "shadowbox_warm"
    assert config.matte["43R"] == "shadowbox_warm"
    assert config.matte["50"] == "shadowbox_warm"


def test_load_tv_deploy_config_reads_custom_path(tmp_path):
    config_path = tmp_path / "tv_deploy.toml"
    config_path.write_text(
        '[matte]\n"43L" = "shadowbox_polar"\n"43R" = "shadowbox_polar"\n'
        '"50" = "modern_warm"\n'
    )

    config = load_tv_deploy_config(path=config_path)

    assert config == TvDeployConfig(
        matte={"43L": "shadowbox_polar", "43R": "shadowbox_polar", "50": "modern_warm"}
    )


def test_successful_deploy_records_history(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_0001")
    _install_fake(monkeypatch, tmp_path, content_id="MY_F0099")

    deploy_image_to_tv("43L", "img_0001")

    history = deploy_history.get_history(
        "43L", path=tmp_path / "tv_deploy_history.sqlite3"
    )
    assert history.current_image_id == "img_0001"
    assert history.previous_image_id is None


def test_revert_tv_without_prior_history_returns_error_without_touching_tv(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_0001")
    fake = _install_fake(monkeypatch, tmp_path)

    result = revert_tv("43L")

    assert "error" in result
    assert fake.uploaded == []


def test_revert_tv_with_history_redeploys_previous_image(tmp_path, monkeypatch):
    _write_fixture_image(tmp_path, "img_old")
    _write_fixture_image(tmp_path, "img_new")
    fake = _install_fake(monkeypatch, tmp_path, content_id="MY_reverted")

    deploy_image_to_tv("43L", "img_old")
    deploy_image_to_tv("43L", "img_new")

    result = revert_tv("43L")

    assert result == {"content_id": "MY_reverted"}
    assert fake.uploaded[-1][0] == str(tmp_path / "img_old.jpg")


def test_reverting_twice_in_a_row_alternates_between_last_two_versions(
    tmp_path, monkeypatch
):
    _write_fixture_image(tmp_path, "img_a")
    _write_fixture_image(tmp_path, "img_b")
    fake = _install_fake(monkeypatch, tmp_path, content_id="MY_x")

    deploy_image_to_tv("43L", "img_a")
    deploy_image_to_tv("43L", "img_b")

    first_revert = revert_tv("43L")
    second_revert = revert_tv("43L")

    assert "error" not in first_revert
    assert "error" not in second_revert
    assert fake.uploaded[-2][0] == str(tmp_path / "img_a.jpg")
    assert fake.uploaded[-1][0] == str(tmp_path / "img_b.jpg")


def test_revert_panels_deploys_all_requested_tvs_independently(monkeypatch):
    calls = []

    def _fake_revert(tv_name):
        calls.append(tv_name)
        if tv_name == "43R":
            return {"error": "no se pudo conectar"}
        return {"content_id": f"MY_{tv_name}"}

    monkeypatch.setattr(tv_deploy, "revert_tv", _fake_revert)

    result = revert_panels(["43L", "43R"])

    assert calls == ["43L", "43R"]
    assert result["43L"] == {"content_id": "MY_43L"}
    assert "error" in result["43R"]
    assert "50" not in result
