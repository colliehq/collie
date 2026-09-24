"""A cached memory model is loaded without asking the Hub; only a missing file is downloaded.

A stand-in ``huggingface_hub`` module records every call, so nothing touches the network.
"""
import sys
import types

import pytest

from harness import embeddings


@pytest.fixture
def hub(monkeypatch):
    calls, cached = [], {"model.onnx"}
    module = types.ModuleType("huggingface_hub")

    def hf_hub_download(repo, filename, **kwargs):
        calls.append((filename, bool(kwargs.get("local_files_only"))))
        if kwargs.get("local_files_only") and filename not in cached:
            raise FileNotFoundError(filename)          # what the real client raises is also an error
        return "/cache/" + filename

    module.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return calls


def test_a_cached_file_never_reaches_the_hub(hub):
    assert embeddings._hf_file("org/model", "model.onnx") == "/cache/model.onnx"
    assert hub == [("model.onnx", True)]


def test_a_missing_file_is_downloaded(hub):
    assert embeddings._hf_file("org/model", "tokenizer.json") == "/cache/tokenizer.json"
    assert hub == [("tokenizer.json", True), ("tokenizer.json", False)]


def test_the_model_loaders_use_the_cache_first_helper():
    import inspect
    source = inspect.getsource(embeddings)
    assert source.count("hf_hub_download(") == 2      # only inside _hf_file
