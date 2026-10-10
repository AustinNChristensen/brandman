import base64

from fastapi.testclient import TestClient

from brandman import store
from brandman.main import app


def test_seeded_brand_list_reports_active_mission_without_creating_one(monkeypatch):
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "test-only-password")
    with TestClient(app, headers={"Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode()}) as client:
        brands = {brand['slug']: brand for brand in client.get('/api/brands').json()}
        assert brands['demo-brand']['has_active_mission'] is True
        assert brands['demo-personal']['has_active_mission'] is False
        personal = brands['demo-personal']
        assert store.rows('SELECT id FROM missions WHERE brand_id=?', (personal['id'],)) == []
        mission = store.create_mission(personal['id'], 'Personal test mission',
                                     '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00')
        brands = {brand['slug']: brand for brand in client.get('/api/brands').json()}
        assert brands['demo-personal']['has_active_mission'] is True
        assert client.get('/api/brands/demo-personal/mission/scorecard').status_code == 200
        with store.connection() as conn:
            conn.execute("UPDATE missions SET status='completed' WHERE id=?", (mission['id'],))
        brands = {brand['slug']: brand for brand in client.get('/api/brands').json()}
        assert brands['demo-personal']['has_active_mission'] is False
