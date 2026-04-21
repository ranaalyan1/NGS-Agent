FROM ngs/base-agent:latest
WORKDIR /agent
RUN apt-get update \
	&& apt-get install -y --no-install-recommends subread \
	&& rm -rf /var/lib/apt/lists/*
COPY main.py ./main.py
CMD ["python3", "-u", "main.py"]
