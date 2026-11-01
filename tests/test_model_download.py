import hashlib
import importlib.util
import io
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("model_download", "ops/docker/download_model.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize("valid", [True, False])
def test_model_published_only_after_checksum(monkeypatch, tmp_path, valid):
    downloader = module()
    monkeypatch.chdir(tmp_path)
    payload = b"synthetic model"
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(payload))
    monkeypatch.setattr(downloader, "SHA256", hashlib.sha256(payload).hexdigest() if valid else "0"*64)
    if valid:
        downloader.download()
        assert Path("model.gguf").read_bytes() == payload
    else:
        with pytest.raises(ValueError, match="checksum"):
            downloader.download()
        assert not Path("model.gguf").exists()
    assert not Path("model.partial").exists()
