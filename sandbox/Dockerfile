FROM python:3.12-slim

RUN groupadd --gid 1000 harness \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/harness harness

RUN pip install --no-cache-dir pytest

WORKDIR /workspace

USER 1000:1000

CMD ["python"]
