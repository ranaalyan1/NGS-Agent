# NGS-Agent v2: Installation & Deployment Guide

## Quick Start (5 minutes)

```bash
# 1. Navigate to NGS-Agent v2
cd NGS-Agent-v2

# 2. Run setup
bash scripts/setup.sh

# 3. Place FASTQ files in data/input/
cp /your/fastq/files data/input/

# 4. Run analysis
python scripts/run_ngs_agent.py --input ./data/input --output ./results

# 5. View results
open results/report.html
```

## Detailed Installation

### Prerequisites

**System Requirements:**
- Linux or macOS (Windows: use WSL2)
- 16+ GB RAM
- 100+ GB free disk space
- Bash shell

**Software Requirements:**
- Python 3.11 or higher
- Git
- HISAT2 (optional, but recommended)
- Samtools (optional, but recommended)
- Trimmomatic (optional, but recommended)
- R 4.0+ (optional, for full analysis)

### Step 1: Clone Repository

```bash
git clone https://github.com/yourusername/NGS-Agent-v2.git
cd NGS-Agent-v2
```

### Step 2: Create Virtual Environment

```bash
# Using venv
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or on Windows: .\.venv\Scripts\activate

# Or using conda (recommended)
conda create -n ngs-agent python=3.11
conda activate ngs-agent
```

### Step 3: Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 4: Install External Tools (Optional but Recommended)

#### Using Conda (Recommended)
```bash
conda install -c bioconda hisat2 samtools trimmomatic featurecounts
conda install -c conda-forge r-base r-deseq2
```

#### Using Apt (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install -y \
  hisat2 \
  samtools \
  trimmomatic \
  subread \
  r-base \
  r-bioc-deseq2
```

#### Using Homebrew (macOS)
```bash
brew install hisat2 samtools trimmomatic r

# Install R packages
Rscript scripts/install_r_packages.R
```

### Step 5: Download Reference Genomes

```bash
# Create genome directory
mkdir -p data/genomes/GRCh38

# Download human reference (example)
cd data/genomes/GRCh38
wget ftp://ftp.ensembl.org/pub/release-112/fasta/homo_sapiens/dna/\
  Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz
gunzip *.fa.gz

# Build HISAT2 index
hisat2-build Homo_sapiens.GRCh38.dna.primary_assembly.fa hisat2_index

# Download GTF annotation
wget ftp://ftp.ensembl.org/pub/release-112/gtf/homo_sapiens/\
  Homo_sapiens.GRCh38.112.gtf.gz
gunzip *.gtf.gz

cd ../../..
```

### Step 6: Verify Installation

```bash
# Test command
python scripts/run_ngs_agent.py --help

# Should display help with no errors
```

## Configuration

### Basic Configuration

Edit `config/default.yaml` or create custom config:

```yaml
organism:
  name: "human"
  genome_version: "GRCh38"

qc:
  min_quality_score: 25

alignment:
  threads: 8
  min_mapping_rate: 0.60

parallel:
  max_threads: 8
  max_memory_gb: 32
```

### Environment Variables

Create `.env` (optional):

```bash
cp .env.example .env
# Edit .env with your settings
```

## Running the Pipeline

### Basic Run

```bash
python scripts/run_ngs_agent.py \
  --input ./data/input \
  --output ./results
```

### With Custom Configuration

```bash
python scripts/run_ngs_agent.py \
  --config config/strict_qc.yaml \
  --input ./data/input \
  --output ./results
```

### With Custom Options

```bash
python scripts/run_ngs_agent.py \
  --input ./data/input \
  --organism mouse \
  --threads 16 \
  --verbose \
  --output ./results
```

### Preview (Dry Run)

```bash
python scripts/run_ngs_agent.py \
  --input ./data/input \
  --dry-run
```

## Docker Installation (Optional)

### Build Docker Image

```dockerfile
# Create Dockerfile
FROM continuumio/miniconda3:latest

WORKDIR /app

COPY . .

RUN conda install -y -c bioconda \
    hisat2 samtools trimmomatic featurecounts && \
    pip install -r requirements.txt && \
    Rscript scripts/install_r_packages.R

ENTRYPOINT ["python", "scripts/run_ngs_agent.py"]
```

### Build and Run

```bash
# Build image
docker build -t ngs-agent-v2 .

# Run container
docker run -v $(pwd)/data:/app/data \
           -v $(pwd)/results:/app/results \
           ngs-agent-v2 --input ./data/input
```

## Singularity Installation (HPC)

### Build Singularity Container

```bash
# From Docker image
singularity build ngs-agent-v2.sif docker://ngs-agent-v2

# Or from definition file
singularity build ngs-agent-v2.sif ngs-agent-v2.def
```

### Run on HPC

```bash
# Interactive run
singularity run -B /input:/mnt/input \
               -B /output:/mnt/output \
               ngs-agent-v2.sif \
               --input /mnt/input

# SLURM submission
sbatch run_ngs_agent.sbatch
```

## Troubleshooting Installation

### Issue: "Python 3.11+ required"
```bash
python --version
python3 --version

# If version < 3.11, install newer version or use conda
conda create -n ngs-agent python=3.11
```

### Issue: "FastQC not found"
```bash
# Install via conda
conda install -c bioconda fastqc

# Or via apt
sudo apt-get install fastqc
```

### Issue: "HISAT2 not found"
```bash
# Install via conda
conda install -c bioconda hisat2

# Build index
hisat2-build genome.fa index_prefix
```

### Issue: "Module not found"
```bash
# Reinstall dependencies
pip install --force-reinstall -r requirements.txt

# Or activate virtual environment
source .venv/bin/activate
```

### Issue: "Insufficient disk space"
```bash
# Check available space
df -h

# Expected usage for 100M reads:
# - Temp files: 20-30 GB
# - Output: 50-80 GB
# Total: 70-110 GB
```

## Performance Optimization

### For Local Machines

```yaml
parallel:
  max_threads: 8
  max_memory_gb: 16
```

### For Workstations

```yaml
parallel:
  max_threads: 32
  max_memory_gb: 64
```

### For Servers/HPC

```yaml
parallel:
  max_threads: 128
  max_memory_gb: 256
  use_cluster: true
```

### Use Fast Storage

```bash
# Point temp directory to SSD
export TMPDIR=/fast_ssd/tmp
# Or in config:
paths:
  temp_dir: /fast_ssd/ngs_temp
```

## Validation

### After Installation

```bash
# Run quick test
bash scripts/quick_test.sh

# Should complete in < 1 minute
# Should generate: qc, alignment, quantification results
```

### Verify Tools

```bash
# Check FastQC
fastqc --version

# Check HISAT2
hisat2 --version

# Check Samtools
samtools --version

# Check featureCounts
featureCounts -v

# Check R/DESeq2
Rscript -e "library(DESeq2); print('DESeq2 OK')"
```

## Production Deployment

### Recommended Setup

```
/bioinformatics/
├── ngs-agent-v2/              # Main application
├── data/
│   ├── genomes/               # Reference genomes
│   ├── annotations/           # GTF files
│   └── runs/                  # Project directories
└── backups/                   # Regular backups
```

### Create Symbolic Link

```bash
ln -s /full/path/to/NGS-Agent-v2 ~/ngs-agent
alias ngs="python ~/ngs-agent/scripts/run_ngs_agent.py"

# Then use
ngs --input ./data --output ./results
```

### Setup Cron Job for Monitoring

```bash
# Add to crontab -e
0 */6 * * * /path/to/ngs-agent/scripts/monitor.sh >> /var/log/ngs-monitor.log 2>&1
```

### Enable Notifications

```yaml
notifications:
  enabled: true
  on_completion: true
  on_error: true
  email: admin@example.com
```

## Updates and Maintenance

### Update to Latest Version

```bash
cd NGS-Agent-v2
git pull origin main
pip install --upgrade -r requirements.txt
```

### Backup Results

```bash
# Backup everything
tar -czf ngs_backup_$(date +%Y%m%d).tar.gz results/

# Or sync to external drive
rsync -av results/ /external_drive/backups/
```

### Clean Up

```bash
# Remove temporary files
rm -rf /tmp/ngs-agent-*

# Clean conda
conda clean --all

# Remove old results (be careful!)
rm -rf results/old_runs/*
```

## Support

- **Documentation**: See README.md and ADVANCED.md
- **Issues**: GitHub Issues
- **Questions**: Email support@example.com

---

**Installation Complete!** 🎉

Next: Place FASTQ files in `data/input/` and run:
```bash
python scripts/run_ngs_agent.py --input ./data/input
```
