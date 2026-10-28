import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_lab.agent import create_app as agent_app
from ai_lab.retrieval import create_app as retrieval_app
from ai_lab.settings import Settings
from ai_lab.telemetry import Telemetry

TEST_KEY = "synthetic-test-key-not-a-real-secret"


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="critical", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("Test server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Test server did not stop")


@pytest.fixture
def lab():
    # Real HTTP listeners exercise propagation, serialization, client timeout and retries.
    exporter = InMemorySpanExporter()
    retrieval_settings = Settings(TEST_KEY, allow_faults=True)
    retrieval_telemetry = Telemetry("policy-retrieval", retrieval_settings, exporter=exporter)
    reader = InMemoryMetricReader()
    with serve(retrieval_app(retrieval_settings, retrieval_telemetry)) as tool_url:
        settings = Settings(TEST_KEY, retrieval_url=tool_url, allow_faults=True)
        telemetry = Telemetry("policy-agent", settings, exporter=exporter, metric_reader=reader)
        with serve(agent_app(settings, telemetry)) as agent_url:
            yield agent_url, exporter, reader
