import asyncio
import time

import httpx
from fastapi import Request
from opentelemetry.trace import SpanKind
from pydantic import ValidationError

from ai_lab.common import make_app, require_auth, require_fault_permission
from ai_lab.models import AskRequest, AskResponse, Citation, RetrievalResponse
from ai_lab.provider import DeterministicModel
from ai_lab.settings import Settings
from ai_lab.telemetry import PROPAGATOR, Telemetry, mark_error


class BudgetExhausted(Exception):
    pass


class ToolFailure(Exception):
    pass


class RetryableToolFailure(Exception):
    pass


class Run:
    def __init__(self, request):
        self.request = request
        self.steps = 0
        self.tool_calls = 0

    def step(self):
        if self.steps >= self.request.max_steps:
            raise BudgetExhausted
        self.steps += 1

    def tool_call(self):
        if self.tool_calls >= self.request.max_tool_calls:
            raise BudgetExhausted
        self.tool_calls += 1


async def retrieve(topic, run, settings, telemetry, client):
    # Fixed destination from deployment settings, never from user input. No redirects/proxy env.
    # The client is shared for this application's lifespan, including connection pooling.
    for attempt in range(2):
        run.tool_call()
        started = time.monotonic()
        with telemetry.span(
            "tool.retrieve", kind=SpanKind.CLIENT, attributes={"lab.retry": attempt}
        ) as span:
            headers = {"X-Lab-Key": settings.api_key}
            PROPAGATOR.inject(headers)
            try:
                async with client.stream(
                    "POST",
                    settings.retrieval_url + "/v1/retrieve",
                    headers=headers,
                    json={"topic": topic, "scenario": run.request.scenario},
                ) as response:
                    span.set_attribute("http.response.status_code", response.status_code)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise RetryableToolFailure
                    if response.status_code != 200:
                        mark_error(span, "tool_rejected")
                        telemetry.record("tool", "error", started)
                        raise ToolFailure
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8192:
                            raise ValueError("Tool response exceeds bound")
                documents = RetrievalResponse.model_validate_json(data).documents
                span.set_attribute("lab.retrieval.count", len(documents))
                telemetry.record("tool", "ok", started)
                return documents
            except (ValidationError, ValueError):
                mark_error(span, "invalid_tool_response")
                telemetry.record("tool", "invalid", started)
                raise ToolFailure from None
            except (httpx.HTTPError, RetryableToolFailure):
                mark_error(span, "tool_unavailable")
                telemetry.record("tool", "error", started)
                if attempt == 1:
                    raise ToolFailure from None
        await asyncio.sleep(0.02)  # Bounded retry, included in the request deadline.


async def execute(body, settings, telemetry, model, client):
    run = Run(body)
    started = time.monotonic()
    answer = "The demonstration could not complete. No eligibility decision was made."
    citations = []
    outcome = "success"
    with telemetry.span(
        "agent.invoke", attributes={"lab.model.mode": "deterministic-mock"}
    ) as span:
        try:
            async with asyncio.timeout(body.timeout_ms / 1000):
                run.step()
                model_started = time.monotonic()
                with telemetry.span("model.plan", attributes={"lab.model.name": "mock-policy-v1"}):
                    if body.scenario == "model_timeout":
                        await asyncio.sleep(10)
                    topic = await model.plan(body.question)
                    telemetry.record("model", "ok", model_started)
                if topic is None:
                    outcome = "unsupported"
                    answer = "This synthetic demo only covers repair assistance and applications."
                else:
                    run.step()
                    documents = await retrieve(topic, run, settings, telemetry, client)
                    if not documents:
                        outcome = "no_evidence"
                        answer = "No synthetic policy evidence was found. Human review is required."
                    else:
                        run.step()
                        model_started = time.monotonic()
                        with telemetry.span(
                            "model.generate", attributes={"lab.model.name": "mock-policy-v1"}
                        ):
                            answer = await model.answer(documents)
                            telemetry.record("model", "ok", model_started)
                        citations = [
                            Citation(document_id=d.document_id, title=d.title) for d in documents
                        ]
        except BudgetExhausted:
            outcome = "budget_exhausted"
        except TimeoutError:
            outcome = "deadline_exceeded"
        except ToolFailure:
            outcome = "tool_error"
        span.set_attributes(
            {"lab.outcome": outcome, "lab.steps": run.steps, "lab.tool.calls": run.tool_calls}
        )
        if outcome in {"budget_exhausted", "deadline_exceeded", "tool_error"}:
            mark_error(span, outcome)
        telemetry.record("agent", outcome, started)
        return AskResponse(
            outcome=outcome,
            answer=answer,
            citations=citations,
            trace_id=f"{span.get_span_context().trace_id:032x}",
            steps=run.steps,
            tool_calls=run.tool_calls,
        )


def create_app(settings=None, telemetry=None):
    settings = settings or Settings.from_env()
    telemetry = telemetry or Telemetry("policy-agent", settings)
    model = DeterministicModel()
    app = make_app("Synthetic Policy Agent", settings, telemetry, {"/healthz", "/v1/ask"})
    app.state.http_client = httpx.AsyncClient(
        timeout=0.25,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )

    @app.post("/v1/ask", response_model=AskResponse)
    async def ask(body: AskRequest, request: Request):
        require_auth(request, settings)
        require_fault_permission(body.scenario, settings)
        return await execute(body, settings, telemetry, model, app.state.http_client)

    return app
