import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


def load_gateway():
    path = Path(__file__).parents[1] / "agent/gateway.py"
    spec = importlib.util.spec_from_file_location("test_gateway_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_answer_window_passes_text_through_and_persists_record(monkeypatch, tmp_path):
    gateway = load_gateway()
    gateway.WINDOW = tmp_path / "window"
    seen = []

    def fake_run(role, prompt, **kwargs):
        seen.append((role, prompt, kwargs))
        return "front reply", {"outcome": "done", "harness": "fake"}, 0

    monkeypatch.setattr(gateway, "run_role", fake_run)
    record = gateway.answer_window("unaltered request")

    assert seen[0][0:2] == ("front", "unaltered request")
    assert record["reply"] == "front reply"
    saved = json.loads((gateway.WINDOW / "run-0001.json").read_text())
    assert saved["question"] == "unaltered request"
    assert saved["reply"] == "front reply"


def test_guide_route_is_gone_and_retained_routes_still_answer(monkeypatch, tmp_path):
    gateway = load_gateway()
    gateway.WINDOW = tmp_path / "window"
    monkeypatch.setattr(
        gateway,
        "run_role",
        lambda role, prompt, **kwargs: (
            "ok",
            {"outcome": "done", "harness": "fake", "duration_ms": 1},
            0,
        ),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/healthz") as response:
            assert json.load(response) == {"ok": True}
        with urllib.request.urlopen(f"{base}/status") as response:
            assert json.load(response)["type"] == "status"
        with urllib.request.urlopen(f"{base}/jobs") as response:
            assert json.load(response)["type"] == "jobs"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/guide")
        assert error.value.code == 404
        request = urllib.request.Request(
            f"{base}/window",
            data=json.dumps({"text": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["reply"] == "ok"
    finally:
        server.shutdown()
        thread.join()
