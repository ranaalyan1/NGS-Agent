# NGS-Agent v2: Autonomous RNA-Seq Analysis System

**NGS-Agent v2** is a production-ready, autonomous RNA-Seq analysis pipeline that orchestrates quality control, alignment, quantification, differential expression analysis, and biological interpretation into a single command.

## 🎯 Features

- **Autonomous Agent System**: Each stage (QC, alignment, quantification, DE) is managed by intelligent agents with decision-making logic
- **Quality-Driven Pipeline**: Automatic decisions on trimming, validation thresholds, and halt conditions
- **End-to-End Analysis**: From raw FASTQ to publication-ready reports with plots
- **Biological Interpretation**: AI-generated insights on differential expression results
- **Reproducible**: YAML-based configuration ensures consistency and transparency
- **Modular Design**: Each component is independently testable and extensible

## 📊 Pipeline Stages

```
Raw FASTQ Files
    ↓
[QC Agent] → FastQC analysis & trim decision
    ↓
[Alignment Agent] → HISAT2 mapping to reference
    ↓
[Quantification Agent] → featureCounts gene expression
    ↓
[DE Agent] → DESeq2 differential expression
    ↓
[Validation Agent] → Quality thresholds & gates
    ↓
[Report Agent] → HTML report + plots + interpretation
    ↓
Results Directory (BAM, counts, DE results, plots)
```

## 🚀 Quick Start

### Installation

```bash
# Clone or download NGS-Agent v2
cd NGS-Agent-v2

# Create conda environment (recommended)
conda create -n ngs-agent python=3.11 -y
conda activate ngs-agent

# Install Python dependencies
pip install -r requirements.txt

# Install R packages (optional, for full DE analysis)
Rscript scripts/install_r_packages.R
```

### Basic Usage

```bash
# Minimal command (uses defaults)
python scripts/run_ngs_agent.py --input ./data/input --output ./results

# With custom organism
python scripts/run_ngs_agent.py --input ./data/input --organism mouse --output ./results

# With custom config
python scripts/run_ngs_agent.py --config config/custom.yaml --input ./data/input

# Verbose logging
python scripts/run_ngs_agent.py --input ./data/input --verbose

# Dry-run (preview without executing)
python scripts/run_ngs_agent.py --input ./data/input --dry-run
```

### Verification

This repository includes two lightweight checks you can run after installation:

```bash
# Validate the core package layout and imports
python test_system.py

# Exercise the full pipeline code path with mock inputs
python test_e2e.py
```

If you want the shortest setup path, open [QUICKSTART.md](QUICKSTART.md) for a step-by-step 5-minute run-through.

## 📁 Project Structure

```
NGS-Agent-v2/
├── config/
│   └── default.yaml              # Main configuration (edit to customize)
├── agents/
│   ├── qc_agent.py               # Quality control & trim decision
│   ├── alignment_agent.py         # Read mapping
│   ├── quantification_agent.py    # Gene counting
│   ├── de_agent.py                # Differential expression
│   ├── validation_agent.py        # Quality gates
│   └── report_agent.py            # Report generation
├── pipelines/
│   └── controller.py              # Main orchestrator
├── modules/
│   ├── config.py                  # Configuration management
│   ├── logging_config.py          # Unified logging
│   ├── base_agent.py              # Agent base class
├── scripts/
│   ├── run_ngs_agent.py           # CLI entry point
│   ├── deseq2_analysis.R          # DESeq2 + plots
│   ├── enrichment_analysis.R      # GO/KEGG pathway analysis
│   └── install_r_packages.R       # R dependency installer
├── data/
│   ├── input/                     # Place FASTQ files here
│   ├── genomes/                   # Reference genomes (HISAT2 indices)
│   └── annotations/               # GTF annotations
├── results/                       # Output directory (auto-created)
├── logs/                          # Pipeline logs
├── requirements.txt               # Python dependencies
└── README.md                      # This file
```

## ⚙️ Configuration

Edit `config/default.yaml` to customize the pipeline:

### Key Settings

```yaml
organism:
  name: "human"                      # Organism (human, mouse, zebrafish)
  genome_version: "GRCh38"

qc:
  enabled: true
  min_quality_score: 25              # Trim if quality < 25
  trim_trigger_threshold: 25

alignment:
  aligner: "hisat2"                  # or "star"
  threads: 8
  min_mapping_rate: 0.60             # Halt if mapping < 60%

differential_expression:
  enabled: true
  alpha: 0.05                        # Significance threshold
  lfc_threshold: 1                   # Log2 fold-change

validation:
  min_reads_per_sample: 10000
  halt_on_validation_failure: false  # Set true for strict mode

reporting:
  ai_interpretation: true            # Generate AI insights
  generate_html: true
```

## 📋 Input Requirements

### FASTQ Files

Place your FASTQ files in `data/input/`:

**Single-end:**
```
data/input/
├── sample1.fastq.gz
├── sample2.fastq.gz
└── sample3.fastq.gz
```

**Paired-end:**
```
data/input/
├── sample1_R1.fastq.gz
├── sample1_R2.fastq.gz
├── sample2_R1.fastq.gz
├── sample2_R2.fastq.gz
```

### Reference Genome

Ensure reference files are in `data/`:

```
data/
├── genomes/GRCh38/
│   └── hisat2_index/
│       └── *.ht2 files
└── annotations/GRCh38/
    └── Homo_sapiens.GRCh38.112.gtf
```

For first-time setup, download and build indices:

```bash
# Download human genome
mkdir -p data/genomes/GRCh38
cd data/genomes/GRCh38

# Download FASTA (example - adjust as needed)
wget ftp://ftp.ensembl.org/.../Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz
gunzip *.fa.gz

# Build HISAT2 index
hisat2-build Homo_sapiens.GRCh38.dna.primary_assembly.fa hisat2_index
```

## 📊 Output Files

After successful run, check `results/`:

```
results/
├── report.html                  # Main report (view in browser)
├── qc_report.html               # FastQC report
├── aligned.bam                  # Aligned reads
├── aligned.bam.bai              # BAM index
├── counts.tsv                   # Gene counts matrix
├── deseq2_results.csv           # DE results
├── pca_plot.png                 # PCA visualization
├── volcano_plot.png             # Volcano plot (FC vs p-value)
├── heatmap_top50.png            # Top DE genes heatmap
├── ma_plot.png                  # MA plot
├── go_enrichment.csv            # GO term enrichment
├── kegg_enrichment.csv          # KEGG pathway enrichment
└── pipeline.log                 # Execution log
```

## 🧠 Agent Decision Logic

### QC Agent
- **Input**: FASTQ files
- **Decision**: Should we trim?
- **Logic**: 
  - If mean quality < trim_trigger_threshold → trim
  - Else → skip trimming
- **Output**: QC metrics, trim recommendation

### Alignment Agent
- **Input**: (Trimmed) FASTQ files
- **Decision**: Is mapping rate acceptable?
- **Logic**:
  - If mapping_rate < min_mapping_rate → WARNING/HALT
  - Else → OK
- **Output**: BAM file, mapping statistics

### Validation Agent
- **Checks**:
  - Min reads per sample
  - Min mapping rate
  - Min gene coverage
- **Decision**: Overall QC pass/fail
- **Output**: Validation report

### DE Agent
- **Input**: Count matrix
- **Method**: DESeq2
- **Output**: 
  - Significant genes (padj < 0.05)
  - Log2 fold-changes
  - P-values
  - Plots (PCA, volcano, heatmap)

### Report Agent
- **Input**: All pipeline results
- **Generation**:
  - HTML report with embedded plots
  - Summary statistics
  - Biological interpretation
  - Top DE genes

## 🔧 Advanced Usage

### Custom Configuration

Create `config/my_analysis.yaml`:

```yaml
organism:
  name: "mouse"
  genome_version: "GRCm39"

qc:
  min_quality_score: 28

alignment:
  threads: 16
  min_mapping_rate: 0.70

validation:
  halt_on_validation_failure: true  # Strict mode
```

Run with custom config:
```bash
python scripts/run_ngs_agent.py --config config/my_analysis.yaml --input ./data
```

### Skip Specific Steps

```bash
# Skip QC
python scripts/run_ngs_agent.py --input ./data --skip-qc

# Skip DE analysis
python scripts/run_ngs_agent.py --input ./data --skip-de

# Skip trimming (even if QC recommends)
python scripts/run_ngs_agent.py --input ./data --skip-trimming
```

### Parallel Processing

Adjust threads in config or command line:

```bash
python scripts/run_ngs_agent.py --input ./data --threads 32
```

### Enable Verbose Logging

```bash
python scripts/run_ngs_agent.py --input ./data --verbose
```

Logs are saved to `logs/`:
- `agent.qc.log` - QC agent logs
- `agent.alignment.log` - Alignment agent logs
- etc.

## 📈 Interpreting Results

### Volcano Plot
- X-axis: log2(fold-change) - direction/magnitude of expression change
- Y-axis: -log10(adjusted p-value) - statistical significance
- Red dots: Significant DE genes
- Gray dots: Not significant

### PCA Plot
- Points represent samples
- Distance between samples = global expression differences
- Tight clusters of replicates = good data quality
- Separation by condition = clear biological signal

### Heatmap
- Rows: Top 50 DE genes
- Columns: Samples
- Colors: Expression level (red = high, blue = low)
- Hierarchical clustering = grouping genes with similar patterns

### DE Results Table
```
gene_name    log2FoldChange    pvalue    padj    baseMean
ACKR1        4.23             1.2e-50   1.1e-45  2543.1
ACKR2        -3.15            2.3e-40   2.0e-35  1823.5
...
```

Columns:
- `log2FoldChange`: Expression change (positive = upregulated)
- `padj`: Benjamini-Hochberg adjusted p-value
- `baseMean`: Average normalized expression

## 🐛 Troubleshooting

### "No FASTQ files found"
- Ensure files are in `data/input/`
- Check file extensions: `.fastq`, `.fastq.gz`, `.fq`, `.fq.gz`
- Use correct naming for paired-end: `*_R1.fastq.gz`, `*_R2.fastq.gz`

### "Low mapping rate" warning
- Check quality: are reads trimmed appropriately?
- Verify genome/organism match
- May indicate contamination or wrong organism

### "Genome index not found"
- Build HISAT2 index (see "Reference Genome" section)
- Or specify custom path in config:
  ```yaml
  organism:
    genome_path: "/path/to/hisat2_index"
  ```

### Out of memory
- Reduce threads: `--threads 4`
- Increase available memory
- Set in config: `parallel.max_memory_gb: 32`

### R packages not installed
Run:
```bash
Rscript scripts/install_r_packages.R
```

## 📚 Installation Requirements

### System
- Linux or macOS (Windows: use WSL2)
- 64-bit processor
- 16+ GB RAM recommended

### Software
- Python 3.11+
- R 4.0+ (optional, for full DE analysis)

### Python Packages
See `requirements.txt`

### R Packages (optional)
- DESeq2
- ggplot2
- pheatmap
- clusterProfiler
- org.Hs.eg.db
- igraph

## 📝 Citation

If you use NGS-Agent v2, please cite:

```
NGS-Agent v2: Autonomous RNA-Seq Analysis System
(Cite your publication here)
```

## 📄 License

MIT License - See LICENSE file

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## 📧 Support

For issues, questions, or feature requests:
- Open an issue on GitHub
- Email: support@example.com

## 🎓 Learning Resources

- [HISAT2 Documentation](https://daehwankimlab.github.io/hisat2/)
- [DESeq2 Vignette](https://bioconductor.org/packages/release/bioc/vignettes/DESeq2/inst/doc/DESeq2.html)
- [RNA-Seq Best Practices](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3625848/)

---

**Built for researchers and bioinformaticians. Ready for production use.**
