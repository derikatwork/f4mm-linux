import pytest


@pytest.fixture(autouse=True)
def _isolated_app_dirs(tmp_path_factory, monkeypatch):
    """Never let a test read or write the real ~/.config or ~/.local/share."""
    root = tmp_path_factory.mktemp("appdirs")
    monkeypatch.setenv("BETHMM_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("BETHMM_DATA_DIR", str(root / "data"))
