FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
WORKDIR /app
COPY requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt
COPY src /app/src
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["python", "-m", "uvicorn", "ai_lab.agent:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "warning"]
