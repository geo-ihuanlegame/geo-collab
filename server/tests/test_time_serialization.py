from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from server.tests.utils import build_test_app


def test_naive_datetime_in_app_response_has_z(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        class TmpModel(BaseModel):
            t: datetime

        router = APIRouter()

        @router.get("/test/dt", response_model=TmpModel)
        def _():
            return TmpModel(t=datetime(2026, 5, 11, 14, 30, 0))

        @router.get("/test/dt-aware", response_model=TmpModel)
        def _():
            return TmpModel(t=datetime(2026, 5, 11, 14, 30, 0, tzinfo=timezone.utc))

        @router.get("/test/dt-dict")
        def _():
            return {"time": datetime(2026, 5, 11, 14, 30, 0)}

        test_app.client.app.router.routes.clear()
        test_app.client.app.include_router(router)

        resp = client.get("/test/dt")
        assert resp.json()["t"].endswith("Z"), f"Expected Z, got {resp.json()}"

        resp = client.get("/test/dt-aware")
        data = resp.json()["t"]
        assert data.endswith("+00:00"), f"Expected +00:00, got {data}"

        resp = client.get("/test/dt-dict")
        assert resp.json()["time"].endswith("Z"), f"Expected Z, got {resp.json()}"
    finally:
        test_app.cleanup()
