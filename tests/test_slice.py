import json
import logging
import time
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TEST_KEY
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from ai_lab.agent import create_app
from ai_lab.settings import Settings
from ai_lab.telemetry import ATTRIBUTES

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = yaml.safe_load((ROOT / "contracts/agent.openapi.yaml").read_text())


def ask(lab, **overrides):
    response = httpx.post(
        lab[0] + "/v1/ask",
        headers={"X-Lab-Key": TEST_KEY},
        json={"question": "How do I apply for repair assistance?", **overrides},
        timeout=6,
        trust_env=False,
    )
    assert response.status_code == 200
    body = response.json()
    schema = {"$ref": "#/components/schemas/AskResponse", "components": CONTRACT["components"]}
    Draft202012Validator(schema).validate(body)
    return body


def trace_spans(lab, trace_id):
    return [s for s in lab[1].get_finished_spans() if f"{s.context.trace_id:032x}" == trace_id]


def assert_graph(spans):
    names = [s.name for s in spans]
    expected = [
        "POST /v1/ask",
        "agent.invoke",
        "model.plan",
        "tool.retrieve",
        "POST /v1/retrieve",
        "retrieval.search",
        "model.generate",
    ]
    assert sorted(names) == sorted(expected)  # Detect missing AND duplicate spans.
    by_name = {s.name: s for s in spans}
    edges = {
        "agent.invoke": "POST /v1/ask",
        "model.plan": "agent.invoke",
        "tool.retrieve": "agent.invoke",
        "POST /v1/retrieve": "tool.retrieve",
        "retrieval.search": "POST /v1/retrieve",
        "model.generate": "agent.invoke",
    }
    for child, parent in edges.items():
        assert by_name[child].parent.span_id == by_name[parent].context.span_id
    assert by_name["POST /v1/ask"].parent is None
    assert {s.resource.attributes["service.name"] for s in spans} == {
        "policy-agent",
        "policy-retrieval",
    }


def test_contracts_are_valid():
    for path in (ROOT / "contracts").glob("*.yaml"):
        validate(yaml.safe_load(path.read_text()))


def test_expected_trace_graph_over_real_http(lab):
    body = ask(lab)
    assert body["outcome"] == "success"
    assert body["citations"][0]["document_id"] == "SYN-REPAIR-001"
    assert body["usage"] == {"input_tokens": None, "output_tokens": None}
    assert_graph(trace_spans(lab, body["trace_id"]))


@pytest.mark.parametrize(
    ("scenario", "outcome", "calls"),
    [
        ("empty", "no_evidence", 1),
        ("timeout", "tool_error", 2),
        ("rate_limit", "tool_error", 2),
        ("server_error", "tool_error", 2),
        ("malformed", "tool_error", 1),
        ("model_timeout", "deadline_exceeded", 0),
    ],
)
def test_faults_are_bounded(lab, scenario, outcome, calls):
    started = time.monotonic()
    body = ask(lab, scenario=scenario, timeout_ms=900)
    assert body["outcome"] == outcome
    assert body["tool_calls"] == calls
    assert body["citations"] == []
    assert time.monotonic() - started < 2.5
    assert (
        len([s for s in trace_spans(lab, body["trace_id"]) if s.name == "tool.retrieve"]) == calls
    )


@pytest.mark.parametrize(
    ("limits", "steps", "calls"),
    [
        ({"max_steps": 1}, 1, 0),
        ({"max_steps": 2}, 2, 1),
        ({"max_tool_calls": 0}, 2, 0),
        ({"max_tool_calls": 1, "scenario": "server_error"}, 2, 1),
    ],
)
def test_budget_enforcement(lab, limits, steps, calls):
    body = ask(lab, **limits)
    assert body["outcome"] == "budget_exhausted"
    assert (body["steps"], body["tool_calls"]) == (steps, calls)


def test_elapsed_time_budget(lab):
    body = ask(lab, scenario="timeout", timeout_ms=50)
    assert body["outcome"] == "deadline_exceeded"
    assert body["tool_calls"] == 1


def test_unsupported_question_does_not_call_tool(lab):
    body = ask(lab, question="What is the weather?")
    assert body["outcome"] == "unsupported"
    assert body["tool_calls"] == 0


def test_auth_input_and_fault_controls():
    with TestClient(create_app(Settings(TEST_KEY))) as client:
        assert client.post("/v1/ask", json={"question": "repair"}).status_code == 401
        headers = {"X-Lab-Key": TEST_KEY}
        assert (
            client.post(
                "/v1/ask", headers=headers, json={"question": "repair", "scenario": "timeout"}
            ).status_code
            == 403
        )
        invalid = client.post(
            "/v1/ask",
            headers=headers,
            json={"question": "repair", "unexpected": "SENSITIVE_CANARY"},
        )
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "Invalid request"}
        assert (
            client.post("/v1/ask", headers=headers, json={"question": "x" * 513}).status_code == 422
        )


def test_content_never_reaches_spans_metrics_or_operational_logs(lab, caplog):
    caplog.set_level(logging.INFO, logger="ai_lab.operational")
    canary = "SENSITIVE_CANARY_email_secret_48271"
    body = ask(lab, question="repair " + canary)
    spans = trace_spans(lab, body["trace_id"])
    assert_graph(spans)
    encoded = json.dumps([json.loads(span.to_json()) for span in spans])
    assert canary not in encoded
    assert TEST_KEY not in encoded
    assert all(set(s.attributes) <= ATTRIBUTES and not s.events for s in spans)
    assert canary not in caplog.text
    assert TEST_KEY not in caplog.text
    metrics = lab[2].get_metrics_data()
    assert metrics is not None
    assert canary not in metrics.to_json()
    for resource in metrics.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    assert set(point.attributes) <= {"lab.operation", "lab.outcome"}


def test_external_traceparent_is_preserved_without_baggage(lab):
    incoming_trace = "1234567890abcdef1234567890abcdef"
    response = httpx.post(
        lab[0] + "/v1/ask",
        trust_env=False,
        headers={
            "X-Lab-Key": TEST_KEY,
            "traceparent": f"00-{incoming_trace}-1234567890abcdef-01",
            "baggage": "private=SENSITIVE_BAGGAGE",
        },
        json={"question": "repair"},
    )
    assert response.json()["trace_id"] == incoming_trace
    spans = trace_spans(lab, incoming_trace)
    root = next(s for s in spans if s.name == "POST /v1/ask")
    assert root.parent.span_id == int("1234567890abcdef", 16)
    assert "SENSITIVE_BAGGAGE" not in "".join(s.to_json() for s in spans)


def test_versioned_answer_dataset(lab):
    dataset = json.loads((ROOT / "evaluations/policy-v1.json").read_text())
    for case in dataset:
        body = ask(lab, question=case["question"])
        assert body["outcome"] == case["outcome"]
        assert [c["document_id"] for c in body["citations"]] == case["document_ids"]
        for phrase in case["required_phrases"]:
            assert phrase in body["answer"]
