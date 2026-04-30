# NGS-Agent v2: System Architecture

## Overview

**NGS-Agent v2** is a modular, agent-based RNA-Seq analysis system designed for production use. It orchestrates biological data processing through intelligent agents that make autonomous decisions based on pipeline metrics.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      CLI Entry Point                            │
│                   (run_ngs_agent.py)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Configuration Manager                          │
│              (YAML parsing, validation)                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Pipeline Controller                           │
│            (Orchestrates all agents and workflow)               │
└──────────────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
         ▼                    ▼                    ▼
    ┌─────────┐           ┌────────┐         ┌──────────┐
    │   QC    │           │Alignment           │Quant     │
    │ Agent   │──────────▶│ Agent   │────────▶│ Agent    │
    └─────────┘           └────────┘         └──────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ Validation Agent │
                    └──────────────────┘
                              │
                ┌─────────────┴──────────────┐
                │                           │
                ▼                           ▼
         ┌────────────┐              ┌──────────┐
         │  DE Agent  │──────────────▶│ Report   │
         │(DESeq2)    │              │ Agent    │
         └────────────┘              └──────────┘
                                           │
                                           ▼
                                  ┌──────────────────┐
                                  │  Results Output  │
                                  │ (HTML, CSV, PNG) │
                                  └──────────────────┘
```

## Component Descriptions

### 1. CLI Entry Point (`scripts/run_ngs_agent.py`)

- **Purpose**: Command-line interface for users
- **Responsibilities**:
  - Parse command-line arguments
  - Validate input files
  - Initialize configuration
  - Launch pipeline
- **Key Functions**:
  - `parse_arguments()`: Parse CLI args
  - `validate_inputs()`: Check FASTQ files exist
  - `discover_fastq_files()`: Find input files
  - `main()`: Execute pipeline

### 2. Configuration System (`modules/config.py`)

- **Purpose**: Centralized configuration management
- **Features**:
  - YAML parsing with validation
  - Dot-notation access: `config.get("organism.name")`
  - Runtime config updates
  - Path management
- **Classes**:
  - `ConfigManager`: Main config handler
  - `ConfigError`: Custom exceptions

### 3. Pipeline Controller (`pipelines/controller.py`)

- **Purpose**: Orchestrates the entire workflow
- **Responsibilities**:
  - Initialize all agents
  - Execute stages in order
  - Manage agent results
  - Handle halt conditions
  - Generate summary reports
- **Key Methods**:
  - `run()`: Main orchestration method
  - `_check_halt()`: Evaluate halt conditions

### 4. Agent Base Class (`modules/base_agent.py`)

- **Purpose**: Provides common interface for all agents
- **Features**:
  - Standardized execution model
  - Error handling
  - Timing and logging
  - Result formatting
- **Key Classes**:
  - `BaseAgent`: Abstract base class
  - `AgentResult`: Standardized result format
  - `AgentStatus`: Status enumeration

### 5. Agent Implementations

#### QC Agent (`agents/qc_agent.py`)
- Analyzes read quality
- Makes trim decisions
- Checks quality metrics
- **Decision Logic**: If mean_quality < threshold → trim

#### Trim Agent (`agents/trim_agent.py`)
- Removes low-quality bases
- Uses Trimmomatic
- Reports retention rate
- **Output**: Trimmed FASTQ files

#### Alignment Agent (`agents/alignment_agent.py`)
- Maps reads to reference genome
- Uses HISAT2 or STAR
- Validates mapping rate
- **Decision Logic**: If mapping_rate < threshold → warning/halt

#### Quantification Agent (`agents/quantification_agent.py`)
- Counts reads per gene
- Uses featureCounts
- Reports expression metrics
- **Output**: Count matrix TSV

#### DE Agent (`agents/de_agent.py`)
- Runs differential expression analysis
- Uses DESeq2 via R
- Generates plots (PCA, volcano, heatmap)
- **Output**: DE results, visualizations

#### Validation Agent (`agents/validation_agent.py`)
- Checks quality thresholds
- Validates metrics across pipeline
- Applies gates (coverage, mapping, duplication)
- **Decision Logic**: Halt if any gate fails (configurable)

#### Report Agent (`agents/report_agent.py`)
- Generates final HTML report
- Creates biological interpretation
- Compiles all results
- **Output**: HTML report with embedded plots

### 6. Logging System (`modules/logging_config.py`)

- **Features**:
  - Colored console output
  - File logging with rotation
  - Configurable levels (DEBUG, INFO, WARNING, ERROR)
  - Per-agent loggers
- **Classes**:
  - `LoggerFactory`: Logger creation and configuration
  - `_ColoredFormatter`: Terminal colors

### 7. File Manager (`modules/file_manager.py`)

- **Purpose**: File operations utility
- **Functions**:
  - `find_fastq_files()`: Discover input files
  - `find_paired_fastq()`: Handle paired-end reads
  - `count_lines()`: FASTQ file validation
  - `get_file_size_mb()`: File size checking

## Data Flow

### Stage 1: Input Processing
```
User Input
    ↓
CLI Validation
    ↓
Config Parsing
    ↓
FASTQ File Discovery
```

### Stage 2: Quality Control
```
FASTQ Files
    ↓
QC Agent (FastQC Analysis)
    ↓
Quality Metrics
    ↓
Trim Decision (if needed)
```

### Stage 3: Preprocessing
```
(Optional) Trimmed FASTQ
    ↓
Trim Agent (Trimmomatic)
    ↓
Trimmed FASTQ Output
```

### Stage 4: Alignment
```
(Trimmed) FASTQ
    ↓
Alignment Agent (HISAT2)
    ↓
BAM Files + Statistics
    ↓
Validation (Mapping Rate Check)
```

### Stage 5: Quantification
```
BAM Files
    ↓
Quantification Agent (featureCounts)
    ↓
Count Matrix
    ↓
Gene Expression Metrics
```

### Stage 6: Differential Expression
```
Count Matrix + Metadata
    ↓
DE Agent (DESeq2)
    ↓
DE Results + Plots
```

### Stage 7: Validation & Reporting
```
All Pipeline Results
    ↓
Validation Agent (Quality Gates)
    ↓
Report Agent (HTML Generation)
    ↓
Final Report + Interpretation
```

## Agent Communication Protocol

Agents communicate via standardized `AgentResult` objects:

```python
@dataclass
class AgentResult:
    status: AgentStatus        # ok, warning, error, halted
    payload: Dict[str, Any]    # Analysis results/data
    reasoning: str             # Human-readable explanation
    halt: bool                 # Should pipeline stop?
    halt_reason: str           # Why halt?
    execution_time: float      # Runtime in seconds
```

### Example Flow

```python
# QC Agent executes
qc_result = qc_agent.run(inputs, config)

# Returns
AgentResult(
    status=AgentStatus.OK,
    payload={'mean_quality': 37.5, 'should_trim': False},
    reasoning="QC complete: quality acceptable, trim not needed",
    halt=False
)

# Controller evaluates
if qc_result.halt:
    pipeline.halt()
else:
    continue_to_next_stage()
```

## Decision Logic

### QC Agent Decision
```
mean_quality < trim_trigger_threshold?
├─ YES → should_trim = True
└─ NO  → should_trim = False
```

### Alignment Agent Decision
```
mapping_rate < min_mapping_rate?
├─ YES → status = WARNING, halt = True
└─ NO  → status = OK, halt = False
```

### Validation Agent Decision
```
Any threshold violated?
├─ YES and halt_on_failure = True → HALT
├─ YES and halt_on_failure = False → WARNING
└─ NO → OK
```

## Error Handling

### Graceful Degradation
- If FastQC unavailable → use mock QC metrics
- If R/DESeq2 unavailable → use mock DE results
- Missing optional dependencies → continue with reduced features

### Recovery Mechanisms
- Retry logic for transient failures
- Checkpoint/resume capability
- Detailed error messages and logging
- Halt conditions prevent cascading failures

## Extension Points

Users can extend the system by:

1. **Creating Custom Agents**
   ```python
   class MyAgent(BaseAgent):
       def execute(self, inputs, config):
           # Custom analysis
   ```

2. **Adding New Config Sections**
   ```yaml
   my_custom_section:
     option1: value1
     option2: value2
   ```

3. **Integrating External Tools**
   - Via subprocess calls
   - Via Python APIs
   - Via containerization

## Performance Characteristics

### Scalability
- Linear with thread count (up to CPU cores)
- Parallelizable stages: alignment, quantification
- Sequential stages: QC decision, reporting

### Resource Usage
- Memory: 8-128 GB (configurable)
- Disk: 100+ GB for full analysis
- CPU: 4-32+ cores

### Typical Runtime (100M reads, 8 cores)
- QC: 5-15 min
- Alignment: 30-60 min
- Quantification: 5-10 min
- DE: 2-5 min
- Reporting: 2-3 min
- **Total**: 45-90 minutes

## Security Considerations

- `.env` file excluded from git (sensitive credentials)
- User input validation at CLI
- Subprocess execution with safe parameters
- No arbitrary code execution
- Logging sanitization for sensitive data

## Testing Strategy

### Unit Tests
- Individual agent logic
- Configuration parsing
- File operations

### Integration Tests
- Full pipeline execution
- Agent communication
- Result validation

### Functional Tests
- End-to-end with real data
- Cross-platform compatibility
- Performance benchmarks

## Future Enhancements

1. **Cloud Integration**
   - AWS, Azure, GCP support
   - Containerized agents (Docker, Singularity)

2. **HPC Support**
   - SLURM integration
   - Job scheduling

3. **Advanced Analysis**
   - Single-cell RNA-Seq support
   - Alternative splicing detection
   - miRNA/small RNA analysis

4. **Visualization**
   - Interactive plots (Plotly, Bokeh)
   - Dashboard integration

5. **Machine Learning**
   - Automated parameter optimization
   - Anomaly detection

---

**Version**: 2.0  
**Last Updated**: 2026-04-30  
**Maintainer**: NGS-Agent Development Team
