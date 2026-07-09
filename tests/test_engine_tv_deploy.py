import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image
from samsungtvws import exceptions

from engine import deploy_history, generation, tv_deploy
from engine.tv_deploy import (
    TvDeployConfig,
    deploy_image_to_tv,
    deploy_set_to_panels,
    load_tv_deploy_config,
    revert_panels,
    revert_tv,
)
from engine.tv_discovery import TvNotFoundError


class _FakeSamsungTVArt:
    def __init__(
        self,
        host,
        token_file=None,
        open_error=None,
        supported=True,
        upload_error=None,
        select_error=None,
        available_error=None,
        existing_content=None,
        content_id="MY_F0001",
    ):
        self.host = host
        self.token_file = token_file
        self._open_error = open_error
        self._supported = supported
        self._upload_error = upload_error
        self._select_error = select_error
        self._available_error = available_error
        self._existing_content = (
            existing_content if existing_content is not None else []
        )
        self._content_id = content_id

        self.closed = False
        self.uploaded: list[tuple[str, str, str]] = []
        self.selected: list[str] = []
        self.deleted: list[str] = []

    def open(self):
        if self._open_error is not None:
            raise self._open_error

    def close(self):
        self.closed = True

    def supported(self):
        return self._supported

    def upload(self, path, matte=None, portrait_matte=None):
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
    assert calls == [
        ("43L", "img_left"),
        ("43R", "img_right"),
        ("50", "img_wide"),
    ]


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


def test_load_tv_deploy_config_reads_house_config():
    config = load_tv_deploy_config()

    assert config.matte


def test_load_tv_deploy_config_reads_custom_path(tmp_path):
    config_path = tmp_path / "tv_deploy.toml"
    config_path.write_text('matte = "shadowbox_polar"\n')

    config = load_tv_deploy_config(path=config_path)

    assert config == TvDeployConfig(matte="shadowbox_polar")


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
