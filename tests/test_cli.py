from unittest.mock import patch
from pathlib import Path

import pytest

from pdfdiff.cli import main
from pdfdiff.app import app


@pytest.fixture(autouse=True)
def isolated_launch_configuration(monkeypatch):
    # CLI 会修改进程内应用配置，测试结束后恢复，避免后续 lifespan 使用已删除的临时目录。
    monkeypatch.setattr(app.state, "configured_roots", (Path.cwd().resolve(),), raising=False)


def test_installed_launcher_keeps_service_on_loopback():
    with patch("pdfdiff.cli.uvicorn.run") as serve:
        main(["--port", "8766"])
    serve.assert_called_once_with(app, host="127.0.0.1", port=8766, workers=1)
    assert app.state.configured_roots == (Path.cwd().resolve(),)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "not-a-port"])
def test_invalid_ports_do_not_start_service(port):
    with patch("pdfdiff.cli.uvicorn.run") as serve, pytest.raises(SystemExit) as error:
        main(["--port", port])
    assert error.value.code == 2
    serve.assert_not_called()


def test_repeated_allowed_roots_replace_the_default_directory(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with patch("pdfdiff.cli.uvicorn.run") as serve:
        main(["--allow-root", str(first), "--allow-root", str(second)])
    assert app.state.configured_roots == (first.resolve(), second.resolve())
    serve.assert_called_once_with(app, host="127.0.0.1", port=8765, workers=1)


def test_relative_allowed_root_is_resolved_from_startup_directory(tmp_path, monkeypatch):
    folder = tmp_path / "inputs"
    folder.mkdir()
    monkeypatch.chdir(tmp_path)
    with patch("pdfdiff.cli.uvicorn.run") as serve:
        main(["--allow-root", "inputs"])
    assert app.state.configured_roots == (folder.resolve(),)
    serve.assert_called_once_with(app, host="127.0.0.1", port=8765, workers=1)


@pytest.mark.parametrize("exists_as_file", [False, True])
def test_invalid_allowed_root_does_not_start_the_service(tmp_path, exists_as_file):
    path = tmp_path / "invalid-root"
    if exists_as_file:
        path.write_text("regular file", encoding="utf-8")
    with patch("pdfdiff.cli.uvicorn.run") as serve, pytest.raises(SystemExit) as error:
        main(["--allow-root", str(path)])
    assert error.value.code == 2
    serve.assert_not_called()
