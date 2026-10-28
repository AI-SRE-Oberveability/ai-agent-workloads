import asyncio
import json
import time
from importlib.resources import files

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ai_lab.common import make_app, require_auth, require_fault_permission
from ai_lab.models import RetrievalRequest
from ai_lab.settings import Settings
from ai_lab.telemetry import Telemetry


def create_app(settings=None, telemetry=None):
    settings = settings or Settings.from_env()
    telemetry = telemetry or Telemetry("policy-retrieval", settings)
    app = make_app("Synthetic Retrieval", settings, telemetry, {"/healthz", "/v1/retrieve"})
    policies = json.loads(files("ai_lab").joinpath("policies.json").read_text(encoding="utf-8"))

    @app.post("/v1/retrieve")
    async def retrieve(body: RetrievalRequest, request: Request):
        require_auth(request, settings)
        require_fault_permission(body.scenario, settings)
        if body.scenario == "timeout":
            await asyncio.sleep(0.75)
        if body.scenario in {"rate_limit", "server_error"}:
            raise HTTPException(
                429 if body.scenario == "rate_limit" else 503, detail="Synthetic dependency fault"
            )
        if body.scenario == "malformed":
            return JSONResponse({"documents": "intentionally-invalid"})
        started = time.monotonic()
        with telemetry.span("retrieval.search") as span:
            documents = [] if body.scenario == "empty" else [policies[body.topic]]
            span.set_attribute("lab.retrieval.count", len(documents))
            telemetry.record("retrieval", "found" if documents else "empty", started)
            return {"documents": documents}

    return app
