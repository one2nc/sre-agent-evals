FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
      "langfuse>=4.17,<5" "httpx>=0.27,<1" "openai>=1.40,<3" "pyyaml>=6,<7" \
      "opentelemetry-sdk>=1.30,<2" "opentelemetry-exporter-otlp-proto-http>=1.30,<2"

COPY demo/ ./demo/
COPY export/ ./export/
COPY data/ ./data/

ENTRYPOINT ["python", "-m", "demo"]
