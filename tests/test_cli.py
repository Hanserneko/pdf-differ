from unittest.mock import patch

import pytest

from pdfdiff.cli import main


def test_installed_launcher_keeps_service_on_loopback():
    with patch("pdfdiff.cli.uvicorn.run") as serve:
        main(["--port", "8766"])
    serve.assert_called_once_with("pdfdiff.app:app", host="127.0.0.1", port=8766, workers=1)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "not-a-port"])
def test_invalid_ports_do_not_start_service(port):
    with patch("pdfdiff.cli.uvicorn.run") as serve, pytest.raises(SystemExit) as error:
        main(["--port", port])
    assert error.value.code == 2
    serve.assert_not_called()
