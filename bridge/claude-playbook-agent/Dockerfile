# python:3.13-slim, pinned by digest for reproducible builds. Same base as
# mcp-gateway/proxmox-mcp.
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt
COPY agent.py .
USER 65534:65534
ENTRYPOINT ["python3", "agent.py"]
