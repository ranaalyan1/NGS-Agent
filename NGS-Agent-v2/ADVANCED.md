# NGS-Agent v2: Advanced Usage Guide

## Table of Contents
1. [Custom Workflows](#custom-workflows)
2. [Integration with Other Tools](#integration-with-other-tools)
3. [Programmatic Usage](#programmatic-usage)
4. [Pipeline Extension](#pipeline-extension)
5. [Performance Optimization](#performance-optimization)
6. [Troubleshooting](#troubleshooting)

## Custom Workflows

### Running Specific Pipeline Stages

You can run individual stages independently:

```python
from pipelines.controller import PipelineController
from agents.qc_agent import QCAgent

# Run just QC
qc_agent = QCAgent()
result = qc_agent.run(
    {'fastq_files': ['input.fastq.gz']},
    config
)
print(result.reasoning)
```

### Integrating into Your Code

```python
from pipelines.controller import PipelineController

# Initialize pipeline
pipeline = PipelineController(config_path="config/custom.yaml")

# Run analysis
results = pipeline.run(
    fastq_files=['sample1.fastq.gz', 'sample2.fastq.gz'],
    metadata={'condition': 'control'}
)

# Access results
print(f"Status: {results['status']}")
print(f"DE genes: {results['results']['de']['significant_genes']}")
```

## Integration with Other Tools

### Importing Results into Python

```python
import pandas as pd

# Load DE results
de_results = pd.read_csv('results/deseq2_results.csv', index_col=0)

# Filter significant genes
sig_genes = de_results[de_results['padj'] < 0.05]

# Get top upregulated
top_up = sig_genes.nlargest(10, 'log2FoldChange')
print(top_up)
```

### Using Counts in External Tools

```python
import numpy as np
from scipy.cluster.hierarchy import dendrogram, linkage

# Load normalized counts
counts = pd.read_csv('results/counts.tsv', sep='\t', index_col=0)

# Compute hierarchical clustering
linkage_matrix = linkage(counts.T, method='euclidean')
dendrogram(linkage_matrix, labels=counts.columns)
```

### Galaxy Integration

NGS-Agent v2 can be wrapped for Galaxy:

```xml
<tool id="ngs_agent_v2" name="NGS-Agent v2">
  <inputs>
    <param name="input_fastq" type="data" format="fastqsanger.gz" label="FASTQ File"/>
    <param name="organism" type="select" label="Organism">
      <option value="human">Human</option>
      <option value="mouse">Mouse</option>
    </param>
  </inputs>
  <outputs>
    <data name="report" format="html" from_work_dir="results/report.html"/>
    <data name="deseq2" format="csv" from_work_dir="results/deseq2_results.csv"/>
  </outputs>
  <command>python scripts/run_ngs_agent.py --input $input_fastq --organism $organism</command>
</tool>
```

## Programmatic Usage

### As a Python Package

```bash
# Install in development mode
pip install -e .
```

```python
from ngs_agent_v2 import PipelineController

pipeline = PipelineController()
results = pipeline.run(['sample1.fastq.gz'])
```

### Via REST API (Future Enhancement)

```bash
# Start API server
python scripts/api_server.py

# Submit job
curl -X POST http://localhost:5000/api/submit \
  -F "fastq=@sample1.fastq.gz" \
  -F "organism=human"

# Check status
curl http://localhost:5000/api/status/run_id_123
```

### Via Command Line Interface

```bash
# Get help
python scripts/run_ngs_agent.py --help

# Verbose output
python scripts/run_ngs_agent.py --input ./data --verbose

# Dry run (preview)
python scripts/run_ngs_agent.py --input ./data --dry-run

# Custom config
python scripts/run_ngs_agent.py \
  --config config/custom.yaml \
  --input ./data \
  --output ./results
```

## Pipeline Extension

### Creating Custom Agents

```python
from modules.base_agent import BaseAgent, AgentResult, AgentStatus

class CustomAgent(BaseAgent):
    def __init__(self):
        super().__init__("custom")
    
    def execute(self, inputs, config):
        # Your analysis logic here
        result_data = self._analyze(inputs)
        
        return AgentResult(
            status=AgentStatus.OK,
            payload=result_data,
            reasoning="Custom analysis complete",
            halt=False
        )
    
    def _analyze(self, inputs):
        # Implementation
        return {}

# Use in pipeline
agent = CustomAgent()
result = agent.run(inputs, config)
```

### Adding New QC Checks

```python
# Extend QCAgent
from agents.qc_agent import QCAgent

class EnhancedQCAgent(QCAgent):
    def execute(self, inputs, config):
        # Call parent
        result = super().execute(inputs, config)
        
        # Add custom checks
        custom_check = self._check_contamination(inputs)
        result.payload['contamination_check'] = custom_check
        
        return result
    
    def _check_contamination(self, inputs):
        # Your contamination check logic
        return {}
```

## Performance Optimization

### Parallel Processing

```yaml
parallel:
  max_threads: 32          # Use all available cores
  max_memory_gb: 128       # Available RAM
  use_cluster: true        # Submit to HPC

# HPC Configuration
hpc:
  job_scheduler: "slurm"   # or "pbs", "sge"
  queue: "normal"
  walltime: "24:00:00"
  nodes: 4
```

### Caching Results

```yaml
advanced:
  cache_results: true
  cache_dir: "/fast_ssd/ngs_cache"
```

### Checkpointing

```yaml
advanced:
  checkpoint_dir: "./checkpoints"
  resume_from_checkpoint: true  # Skip completed stages
```

## Troubleshooting

### Issue: Pipeline Runs Slowly

1. Check CPU usage:
```bash
top -p $(pgrep -f run_ngs_agent)
```

2. Increase threads:
```yaml
parallel:
  max_threads: 64
```

3. Use faster storage:
```yaml
paths:
  temp_dir: "/nvme_ssd/tmp"  # Much faster than HDD
```

### Issue: Memory Errors

1. Check memory usage:
```bash
ps aux | grep python
```

2. Reduce memory usage:
```yaml
parallel:
  max_memory_gb: 16
  max_threads: 4
```

3. Enable swap (not recommended for production):
```bash
sudo fallocate -l 32G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### Issue: Alignment Fails

1. Verify genome index:
```bash
ls data/genomes/GRCh38/hisat2_index/
```

2. Re-build if missing:
```bash
hisat2-build genome.fa hisat2_index
```

### Debug Mode

Enable full debugging:

```yaml
logging:
  level: "DEBUG"
  log_to_file: true

advanced:
  keep_intermediate_files: true
  verbose: true
```

Check logs:
```bash
tail -f logs/*.log
grep ERROR logs/*.log
```

### Performance Profiling

Profile agent execution:

```python
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()

# Run pipeline
results = pipeline.run(fastq_files)

profiler.disable()
stats = pstats.Stats(profiler)
stats.sort_stats('cumulative')
stats.print_stats(20)  # Top 20 functions
```

## Benchmarks

Typical runtimes on modern hardware (8 cores, 32GB RAM):

| Stage | Time | Notes |
|-------|------|-------|
| QC | 5-15 min | FastQC on 100M reads |
| Trimming | 10-20 min | Optional, if needed |
| Alignment | 30-60 min | HISAT2 on 100M reads |
| Quantification | 5-10 min | featureCounts |
| DE Analysis | 2-5 min | DESeq2 on 20k genes |
| **Total** | **1-2 hours** | Full pipeline |

Performance scales with:
- Number of reads
- Reference genome size
- Available CPUs/memory
- Storage speed

---

For more information, see the main [README.md](../README.md)
