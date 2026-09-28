# NGS-Agent: the Box (drop-zone web page) and the `ngs` CLI in one image.
#
# Build:  docker build -t ngs-agent .
# Box:    docker run --rm -p 8000:8000 ngs-agent
# CLI:    docker run --rm -v "$PWD:/data" ngs-agent ngs /data/sample_fastqc.zip
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY core/ core/
COPY doors/ doors/

RUN pip install --no-cache-dir ".[box]" \
    && python -c "import core.assess, doors.cli, doors.gui.app; print('ngs-agent OK')"

RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["python", "-m", "uvicorn", "doors.gui.app:app", "--host", "0.0.0.0", "--port", "8000"]
