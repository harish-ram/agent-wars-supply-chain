FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    UV_HTTP_TIMEOUT=300 \
    UV_LINK_MODE=copy

WORKDIR /app

# uv resolves and downloads dependencies in parallel - far faster than pip on slow networks.
# Cache mounts keep downloaded files between builds, so a retry resumes instead of starting over.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=120 --retries 10 uv==0.6.14
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

COPY supply_chain_agent ./supply_chain_agent
COPY static ./static
COPY main.py .

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
