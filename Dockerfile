FROM python:3.13-slim AS build

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends libkrb5-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir ".[kerberos]"

FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends libkrb5-3 libgssapi-krb5-2 && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -r -u 10001 -s /bin/false mcp

COPY --from=build /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=build /usr/local/bin/mcp-zuul /usr/local/bin/mcp-zuul

USER mcp

LABEL org.opencontainers.image.title="mcp-zuul" \
      org.opencontainers.image.description="MCP server for Zuul CI — build failure analysis, log search, pipeline status, and job configuration" \
      org.opencontainers.image.source="https://github.com/imatza-rh/mcp-zuul" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.documentation="https://imatza-rh.github.io/mcp-zuul/"

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import mcp_zuul" || exit 1

ENTRYPOINT ["mcp-zuul"]
