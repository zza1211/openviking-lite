FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 1933
CMD ["ov-lite", "serve", "--config", "/config/config.local.json"]
