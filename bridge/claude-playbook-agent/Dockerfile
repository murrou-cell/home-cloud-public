# python:3.13-slim, pinned by digest for reproducible builds. Same base as
# mcp-gateway/proxmox-mcp. Build context is the repo root (see
# .github/workflows/claude-playbook-agent.yml) so this can pull common.py and
# prompts/ straight from mcp-gateway - real files, not a duplicated copy that
# would silently drift out of sync.
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280
WORKDIR /app
COPY bridge/claude-playbook-agent/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt
COPY bridge/claude-playbook-agent/agent.py .
COPY bridge/mcp-gateway/common.py .
COPY bridge/mcp-gateway/prompts/ ./prompts/
USER 65534:65534
ENTRYPOINT ["python3", "agent.py"]
