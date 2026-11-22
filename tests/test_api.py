import pytest
from fastapi.testclient import TestClient

import api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STATICDB_DIR", str(tmp_path))
    monkeypatch.delenv("STATICDB_MEME_DELAY", raising=False)
    api.get_store.cache_clear()
    api.get_store().write_segment({i: {"id": i, "name": f"Customer {i}"} for i in range(1, 6)})
    yield TestClient(api.app)
    api.get_store.cache_clear()


def test_get_customer(client):
    res = client.get("/customer/2")
    assert res.status_code == 200
    body = res.json()
    assert body["customer"] == {"id": 2, "name": "Customer 2"}
    assert body["meta"]["segment"] == "seg_0001"
    assert body["meta"]["frames"] == [0, 0]


def test_missing_and_invalid_ids(client):
    assert client.get("/customer/999").status_code == 404
    assert client.get("/customer/0").status_code == 422


def test_update_then_flush(client):
    res = client.put("/customer/2", json={"name": "Renamed"})
    assert res.status_code == 202
    assert res.json()["pending"] == 1
    assert client.get("/customer/2").json()["customer"]["name"] == "Customer 2"

    flushed = client.post("/flush").json()
    assert flushed["status"] == "flushed" and flushed["segment"] == "seg_0002"
    assert client.get("/customer/2").json()["customer"]["name"] == "Renamed"
    assert client.post("/flush").json() == {"status": "nothing pending"}


def test_oversized_update_rejected(client):
    assert client.put("/customer/1", json={"blob": "x" * 600}).status_code == 422


def test_stats_and_homepage(client):
    assert client.get("/stats").json()["records"] == 5
    page = client.get("/")
    assert page.status_code == 200 and "YouTubeDB" in page.text
