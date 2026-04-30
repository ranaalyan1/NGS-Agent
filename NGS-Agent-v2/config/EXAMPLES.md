# NGS-Agent v2: Example Workflows

This directory contains example configurations for different analysis scenarios.

## Available Examples

### 1. Standard RNA-Seq (Human)
**File**: `rna_seq_human.yaml`
**Use Case**: Standard mRNA expression profiling in human samples
**Organism**: Homo sapiens (GRCh38)
**Aligner**: HISAT2
**DE Method**: DESeq2
**Features**:
  - Quality-driven trimming
  - Genome-guided alignment
  - Gene-level quantification

```bash
python scripts/run_ngs_agent.py --config config/rna_seq_human.yaml --input ./data
```

### 2. Strict Quality Control
**File**: `strict_qc.yaml`
**Use Case**: Sensitive analysis requiring high-quality data
**Features**:
  - Min mapping rate: 70%
  - Min quality score: 30
  - Halt on any validation failure
  - Duplicates removed

```bash
python scripts/run_ngs_agent.py --config config/strict_qc.yaml --input ./data
```

### 3. Quick Preview
**File**: `quick_preview.yaml`
**Use Case**: Fast exploration of data before committing to full analysis
**Features**:
  - Skip trimming
  - Fewer threads (4)
  - Mock DE analysis
  - Fast completion

```bash
python scripts/run_ngs_agent.py --config config/quick_preview.yaml --input ./data
```

### 4. Production Pipeline
**File**: `production.yaml`
**Use Case**: High-throughput production runs
**Features**:
  - Parallel processing (16+ threads)
  - Extended logging
  - Comprehensive reporting
  - Duplicate removal
  - Base quality recalibration

```bash
python scripts/run_ngs_agent.py --config config/production.yaml --input ./data
```

## Creating Custom Configurations

1. Copy an example config:
```bash
cp config/rna_seq_human.yaml config/my_analysis.yaml
```

2. Edit settings:
```yaml
organism:
  name: "mouse"      # Change to mouse
  genome_version: "GRCm39"

alignment:
  min_mapping_rate: 0.65  # Adjust threshold
```

3. Run with custom config:
```bash
python scripts/run_ngs_agent.py --config config/my_analysis.yaml --input ./data
```

## Configuration Decision Tree

```
Is this your first time?
├─ YES → Use: rna_seq_human.yaml
└─ NO  → Need high quality?
         ├─ YES → Use: strict_qc.yaml
         └─ NO  → Need speed?
                  ├─ YES → Use: quick_preview.yaml
                  └─ NO  → Use: production.yaml
```

## Common Customizations

### Change Organism
```yaml
organism:
  name: "mouse"
  genome_version: "GRCm39"
```

### Adjust Trimming Threshold
```yaml
qc:
  trim_trigger_threshold: 28  # Higher = more aggressive trimming
```

### Skip Steps
```yaml
# Skip trimming
trimming:
  enabled: false

# Skip DE analysis
differential_expression:
  enabled: false
```

### Change Number of Threads
```yaml
parallel:
  max_threads: 32  # Increase for faster processing
```

### Strict Mode (Halt on Issues)
```yaml
validation:
  halt_on_validation_failure: true  # Stop if QC fails
```

### Generate PDF Reports
```yaml
reporting:
  generate_pdf: true
  pdf_theme: "professional"
```

## Monitoring Example Run

```bash
# Run with verbose output
python scripts/run_ngs_agent.py \
  --config config/production.yaml \
  --input ./data/input \
  --verbose

# Watch logs in real-time (in another terminal)
tail -f logs/*.log
```

## Analyzing Results

After pipeline completion, check results directory:

```bash
ls -lh results/

# View mapping statistics
cat results/alignment_stats.txt

# View DE summary
head -20 results/deseq2_results.csv

# Open HTML report
open results/report.html  # macOS
xdg-open results/report.html  # Linux
start results/report.html  # Windows
```

## Performance Tips

1. **Increase Threads for Speed**:
   ```yaml
   parallel:
     max_threads: 32
   ```

2. **Reduce Memory Usage**:
   ```yaml
   parallel:
     max_memory_gb: 16
   ```

3. **Skip Intermediate Steps if Not Needed**:
   ```yaml
   skip_trimming: true  # If data already trimmed
   ```

4. **Use SSD for Temporary Files**:
   ```yaml
   paths:
     temp_dir: "/fast_ssd/ngs_temp"
   ```

## Troubleshooting Configurations

### Issue: Out of Memory
**Solution**: Reduce threads and memory:
```yaml
parallel:
  max_threads: 4
  max_memory_gb: 8
```

### Issue: Low Mapping Rate
**Solution**: Adjust alignment settings:
```yaml
alignment:
  min_mapping_rate: 0.50  # Lower threshold
  max_intron_length: 750000  # Increase for larger introns
```

### Issue: Few Significant DE Genes
**Solution**: Adjust DE thresholds:
```yaml
differential_expression:
  alpha: 0.10  # More lenient
  lfc_threshold: 0.5  # Lower fold-change threshold
```

## Comparing Configurations

To understand differences between configs:

```bash
diff config/strict_qc.yaml config/production.yaml
```

Key differences:
- Threads: quick_preview (4) vs production (16)
- Trimming: quick_preview (skip) vs strict_qc (aggressive)
- Validation: strict_qc (halt) vs quick_preview (warn)
