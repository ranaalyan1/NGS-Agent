FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
	&& apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /agent
RUN python3 -m pip install --no-cache-dir anthropic
COPY agents/base/base_agent.py ./base_agent.py
COPY agents/ai_decider/main.py ./main.py
CMD ["python3", "-u", "main.py"]
