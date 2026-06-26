FROM python:3.11-slim

WORKDIR /app

# Install dependencies in a separate layer so source changes don't bust the cache
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]"

COPY src/ ./src/
COPY scripts/ ./scripts/

# Non-root user — never run as root in production
RUN useradd -m canopy
USER canopy

# Persistent data volume — mount at this path to survive container restarts
ENV CANOPY_DATA_DIR=/data
VOLUME ["/data"]

# Gradio default port
EXPOSE 7860

CMD ["python", "scripts/run_ui.py"]
