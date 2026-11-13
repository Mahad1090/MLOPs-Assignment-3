"""Prediction API - the FastAPI inference service deployed on Kubernetes.

Liveness answers as soon as the process is up; readiness answers only when a
model is loaded, so a Pod that cannot load its model is taken out of the
Service instead of receiving traffic.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

APP_VERSION = os.environ.get("APP_VERSION", "dev")
MODEL_PATH = Path(os.environ.get("MODEL_PATH", "/app/model/model.pkl"))
MODEL_META_PATH = Path(os.environ.get("MODEL_META_PATH", str(MODEL_PATH.with_name("model_meta.json"))))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout, format="%(message)s")
log = logging.getLogger("prediction-api")
state: dict[str, Any] = {"model": None, "meta": {}, "load_error": None}


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}))


def load_model() -> None:
    try:
        state["model"] = joblib.load(MODEL_PATH)
        if MODEL_META_PATH.exists():
            state["meta"] = json.loads(MODEL_META_PATH.read_text())
        state["load_error"] = None
        _log("model_loaded", model_path=str(MODEL_PATH), model=state["meta"])
    except Exception as exc:  # noqa: BLE001 - keep serving liveness; readiness reports the error
        state["model"] = None
        state["load_error"] = f"{type(exc).__name__}: {exc}"
        _log("model_load_failed", level="ERROR", model_path=str(MODEL_PATH), error=state["load_error"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    _log("startup", app_version=APP_VERSION)
    load_model()
    yield


app = FastAPI(title="Prediction API", version=APP_VERSION, lifespan=lifespan)


class PredictRequest(BaseModel):
    instances: list[dict[str, float]] = Field(min_length=1, max_length=256)


class PredictResponse(BaseModel):
    predictions: list[int]
    probabilities: list[float]
    model_version: str | None
    request_id: str


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if not request.url.path.startswith("/health"):
        _log(
            "request",
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return response


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    if state["model"] is None:
        _log("readiness_failed", level="ERROR", reason=state["load_error"])
        raise HTTPException(status_code=503, detail=f"model not loaded: {state['load_error']}")
    return {"status": "ready"}


@app.get("/version")
def version() -> dict[str, Any]:
    return {"app_version": APP_VERSION, "model_loaded": state["model"] is not None, "model": state["meta"]}


@app.post("/predict", response_model=PredictResponse)
def predict(request: PredictRequest) -> PredictResponse:
    model = state["model"]
    if model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    features = list(getattr(model, "feature_names_in_", []))
    frame = pd.DataFrame(request.instances)
    missing = [f for f in features if f not in frame.columns]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing features: {missing[:5]}")
    frame = frame[features] if features else frame
    probabilities = model.predict_proba(frame)[:, 1]
    return PredictResponse(
        predictions=[int(p >= 0.5) for p in probabilities],
        probabilities=[round(float(p), 6) for p in probabilities],
        model_version=str(state["meta"].get("model_version")) if state["meta"] else None,
        request_id=uuid.uuid4().hex,
    )
