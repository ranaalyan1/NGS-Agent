#!/usr/bin/env bash
# setup-native.sh -- install all system-level bioinformatics tools needed to run
# the NGS-Agent pipeline natively (no Docker required).
#
# Supports Debian/Ubuntu.  On macOS use Homebrew (brew install <pkg>).
set -euo pipefail

OS="$(uname -s)"

install_linux() {
    echo "==> Updating apt package lists..."
    sudo apt-get update -y

    echo "==> Installing bioinformatics tools..."
    sudo apt-get install -y --no-install-recommends \
        python3 python3-pip \
        fastqc default-jre-headless \
        hisat2 samtools \
        bwa \
        trimmomatic \
        subread \
        r-base r-base-dev \
        r-cran-tidyverse r-cran-ggplot2 r-cran-jsonlite \
        r-cran-readr r-cran-dplyr r-cran-tibble \
        r-cran-htmlwidgets r-cran-httr \
        ca-certificates curl

    echo "==> Installing Python dependencies..."
    python3 -m pip install --no-cache-dir -r requirements.txt
}

install_macos() {
    if ! command -v brew &>/dev/null; then
        echo "Homebrew not found.  Install it from https://brew.sh and re-run this script."
        exit 1
    fi

    echo "==> Installing bioinformatics tools via Homebrew..."
    brew install python fastqc hisat2 samtools bwa trimmomatic subread r

    echo "==> Installing Python dependencies..."
    python3 -m pip install --no-cache-dir -r requirements.txt
}

echo "==> Detected OS: ${OS}"
case "${OS}" in
    Linux)  install_linux  ;;
    Darwin) install_macos  ;;
    *)
        echo "Unsupported OS: ${OS}.  Please install the dependencies manually."
        exit 1
        ;;
esac

echo ""
echo "==> Setup complete."
echo "    Copy .env.example to .env and fill in your credentials, then run:"
echo "      python worker.py"
echo "    or use the quick-start wizard:"
echo "      make wizard"
