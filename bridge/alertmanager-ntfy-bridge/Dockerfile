# python:3.13-alpine, pinned by digest for reproducible builds.
FROM python:3.13-alpine@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0
COPY bridge.py /bridge.py
USER 65534:65534
ENTRYPOINT ["python3", "/bridge.py"]
