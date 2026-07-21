import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import deploy_history  # noqa: E402


def test_get_history_returns_none_when_tv_never_deployed(tmp_path):
    db_path = tmp_path / "tv_deploy_history.sqlite3"

    assert deploy_history.get_history("43L", path=db_path) is None


def test_first_record_deploy_sets_current_with_no_previous(tmp_path):
    db_path = tmp_path / "tv_deploy_history.sqlite3"

    deploy_history.record_deploy("43L", "img_0001", path=db_path)
    history = deploy_history.get_history("43L", path=db_path)

    assert history.current_image_id == "img_0001"
    assert history.previous_image_id is None


def test_second_record_deploy_shifts_current_to_previous(tmp_path):
    db_path = tmp_path / "tv_deploy_history.sqlite3"

    deploy_history.record_deploy("43L", "img_0001", path=db_path)
    deploy_history.record_deploy("43L", "img_0002", path=db_path)
    history = deploy_history.get_history("43L", path=db_path)

    assert history.current_image_id == "img_0002"
    assert history.previous_image_id == "img_0001"


def test_third_record_deploy_keeps_only_one_level_of_history(tmp_path):
    db_path = tmp_path / "tv_deploy_history.sqlite3"

    deploy_history.record_deploy("43L", "img_0001", path=db_path)
    deploy_history.record_deploy("43L", "img_0002", path=db_path)
    deploy_history.record_deploy("43L", "img_0003", path=db_path)
    history = deploy_history.get_history("43L", path=db_path)

    assert history.current_image_id == "img_0003"
    assert history.previous_image_id == "img_0002"


def test_history_is_independent_per_tv(tmp_path):
    db_path = tmp_path / "tv_deploy_history.sqlite3"

    deploy_history.record_deploy("43L", "img_left", path=db_path)
    deploy_history.record_deploy("50", "img_wide", path=db_path)

    assert deploy_history.get_history("43L", path=db_path).current_image_id == (
        "img_left"
    )
    assert deploy_history.get_history("50", path=db_path).current_image_id == (
        "img_wide"
    )


def test_concurrent_record_deploy_calls_do_not_lose_either_update(
    tmp_path, monkeypatch
):
    """record_deploy read the previous current_image_id in one connection
    then wrote in a separate one -- a race window where a second concurrent
    call for the same tv_name could commit between the read and the write,
    and get silently clobbered by the first call's now-stale `previous`
    value. Force the interleaving deterministically: pause the first
    record_deploy call right after its read, let a second call run to
    completion, then release the first -- the fix must ensure the second
    call's write is never lost.
    """
    db_path = tmp_path / "tv_deploy_history.sqlite3"

    thread_1_read = threading.Event()
    release_thread_1 = threading.Event()
    real_get_history = deploy_history.get_history
    first_call_done = False

    def _get_history_pausing_first_caller(tv_name, path=None):
        nonlocal first_call_done
        existing = real_get_history(tv_name, path)
        if not first_call_done:
            first_call_done = True
            thread_1_read.set()
            release_thread_1.wait(timeout=5)
        return existing

    monkeypatch.setattr(
        deploy_history, "get_history", _get_history_pausing_first_caller
    )

    thread_1 = threading.Thread(
        target=deploy_history.record_deploy, args=("43L", "img_A", db_path)
    )
    thread_1.start()
    assert thread_1_read.wait(timeout=5)

    # Give thread_2 a real window to run to completion (buggy code: no
    # lock, so it finishes here comfortably) or to block on the fix's
    # lock (in which case this join simply times out without completing).
    thread_2 = threading.Thread(
        target=deploy_history.record_deploy, args=("43L", "img_B", db_path)
    )
    thread_2.start()
    thread_2.join(timeout=0.3)
    release_thread_1.set()
    thread_1.join(timeout=5)
    thread_2.join(timeout=5)

    history = real_get_history("43L", db_path)
    assert {history.current_image_id, history.previous_image_id} == {"img_A", "img_B"}
