# NGS-Agent: the Box (drop-zone web page) and the `ngs` CLI in one image.
#
# Build:  docker build -t ngs-agent .
# Box:    docker run --rm -p 8000:8000 ngs-agent
#         then open http://127.0.0.1:8000 and drop a file on it.
# CLI:    docker run --rm -v "$PWD:/data" ngs-agent ngs /data/sample_fastqc.zip
#
# No Python install needed on the host: the image carries everything.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Project metadata first (cheap layer), then the product.
COPY pyproject.toml README.md LICENSE ./
COPY core/ core/
COPY doors/ doors/
COPY ngs_agent/ ngs_agent/

RUN pip install --no-cache-dir ".[box]" \
    && python -c "import core.assess, doors.cli, doors.gui.app; print('ngs-agent OK')"

# Run as a non-root user: the Box only needs to read the dropped file.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# Default: serve the Box. Override the command to use the CLI instead.
CMD ["python", "-m", "uvicorn", "doors.gui.app:app", "--host", "0.0.0.0", "--port", "8000"]
