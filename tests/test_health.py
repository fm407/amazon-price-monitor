from fastapi.testclient import TestClient

from app.main import create_app


def test_health_reports_ok_when_database_is_reachable(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
