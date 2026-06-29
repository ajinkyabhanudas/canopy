FROM python:3.11-slim

WORKDIR /app

# Install deps in a separate layer so source changes don't bust the pip cache.
# Stub src/canopy/__init__.py lets setuptools resolve editable install metadata
# without the full source tree — this layer only rebuilds if pyproject.toml changes.
COPY pyproject.toml ./
RUN mkdir -p src/canopy && touch src/canopy/__init__.py
RUN pip install --no-cache-dir -e ".[dev]"

# Overwrite stub with real source (editable install picks it up via the .pth file)
COPY src/ ./src/
COPY scripts/ ./scripts/

# Non-root user — /data must be chowned before USER switch so the volume
# inherits canopy ownership when Docker initialises it on first run.
RUN useradd -m canopy && mkdir -p /data && chown canopy:canopy /data
USER canopy

# Persistent data volume — mount at this path to survive container restarts
ENV CANOPY_DATA_DIR=/data
VOLUME ["/data"]

# Gradio default port
EXPOSE 7860

CMD ["python", "scripts/run_ui.py"]
