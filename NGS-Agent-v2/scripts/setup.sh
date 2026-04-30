#!/bin/bash
# NGS-Agent v2: Setup Script
# Initializes the pipeline environment and downloads reference data

set -e

echo "NGS-Agent v2: Environment Setup"
echo "================================"

# Check Python version
echo -e "\n[1/5] Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found. Please install Python 3.11+"
    exit 1
fi

python_version=$(python3 --version | awk '{print $2}')
echo "✓ Found Python $python_version"

# Create directories
echo -e "\n[2/5] Creating directory structure..."
mkdir -p data/input data/genomes data/annotations results logs
echo "✓ Directory structure created"

# Install Python dependencies
echo -e "\n[3/5] Installing Python dependencies..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "✓ Virtual environment created"
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "✓ Python dependencies installed"

# Optional: Install R packages
echo -e "\n[4/5] Installing R packages (optional)..."
if command -v Rscript &> /dev/null; then
    echo "R found. Installing required packages..."
    Rscript scripts/install_r_packages.R
    echo "✓ R packages installed"
else
    echo "⚠ R not found. DESeq2 analysis will use mock results."
    echo "  To enable full analysis, install R 4.0+ and rerun this script."
fi

# Create .env file if needed
echo -e "\n[5/5] Setting up configuration..."
if [ ! -f ".env" ]; then
    cat > .env << EOF
# NGS-Agent v2 Environment Variables
# Customize as needed

# Output settings
OUTPUT_DIR=./results
LOG_LEVEL=INFO

# External API keys (optional)
# OPENROUTER_API_KEY=your_key
# ANTHROPIC_API_KEY=your_key
EOF
    echo "✓ Configuration file created (.env)"
fi

echo -e "\n================================"
echo "Setup complete! ✓"
echo ""
echo "Next steps:"
echo "1. Place FASTQ files in data/input/"
echo "2. Download reference genome (see README)"
echo "3. Run: python scripts/run_ngs_agent.py --input ./data/input"
echo ""
