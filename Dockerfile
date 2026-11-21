# Multi-stage: install where a compiler may be needed, then copy only the
# installed packages into a slim runtime image.
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md ./
COPY nanocrawl ./nanocrawl

RUN pip install --prefix=/install .


FROM python:3.12-slim AS runtime

# A crawler makes outbound network requests; it has no reason to be root.
RUN useradd --create-home --uid 10001 nanocrawl

WORKDIR /work
COPY --from=builder /install /usr/local

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER nanocrawl

# The image is the CLI, so arguments pass straight through:
#   docker run --rm nanocrawl --demo
#   docker run --rm -v "$PWD:/work" nanocrawl https://example.com --out /work/corpus.jsonl
ENTRYPOINT ["nanocrawl"]
CMD ["--demo"]
