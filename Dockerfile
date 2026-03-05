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
    useradd -r -s /bin/false mcp

COPY --from=build /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=build /usr/local/bin/mcp-zuul /usr/local/bin/mcp-zuul

USER mcp

ENTRYPOINT ["mcp-zuul"]
