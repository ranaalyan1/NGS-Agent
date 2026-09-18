FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ca-certificates \
        r-base \
        r-base-dev \
        r-cran-tidyverse \
        r-cran-ggplot2 \
        r-cran-jsonlite \
        r-cran-readr \
        r-cran-dplyr \
        r-cran-tibble \
        r-cran-htmlwidgets \
        r-cran-plotly \
        libcurl4-openssl-dev \
        libxml2-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Bioconductor packages used by de_analysis.R (not available via apt).
RUN Rscript -e 'install.packages("BiocManager", repos="https://cloud.r-project.org")' \
    && Rscript -e 'BiocManager::install(c("DESeq2", "ComplexHeatmap", "EnhancedVolcano"), update=FALSE, ask=FALSE)' \
    && Rscript -e 'install.packages(c("matrixStats", "circlize"), repos="https://cloud.r-project.org")'

RUN python3 -m pip install --no-cache-dir boto3 pydantic

WORKDIR /app
COPY agents/base/base_agent.py /app/base_agent.py
COPY shared/storage.py /app/storage.py
COPY agents/de_agent/main.py /app/main.py
COPY agents/de_agent/de_analysis.R /app/de_analysis.R
CMD ["python3", "-u", "/app/main.py"]
