FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agents/base/base_agent.py /app/base_agent.py
COPY agents/report_agent/main.py /app/main.py
CMD ["python3", "-u", "/app/main.py"]
