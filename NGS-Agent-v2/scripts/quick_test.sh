#!/bin/bash
# NGS-Agent v2: Quick Test Script
# Tests the pipeline with mock data

set -e

echo "NGS-Agent v2: Running Quick Test"
echo "=================================="

# Activate virtual environment if it exists
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Create test data
echo -e "\n[1/3] Setting up test environment..."
mkdir -p test_data/input test_data/results

# Create minimal FASTQ files for testing
cat > test_data/input/test_R1.fastq << 'EOF'
@read1
ATCGATCGATCGATCGATCGATCG
+
IIIIIIIIIIIIIIIIIIIIIIII
@read2
GCTAGCTAGCTAGCTAGCTAGCTA
+
JJJJJJJJJJJJJJJJJJJJJJJJ
EOF

cat > test_data/input/test_R2.fastq << 'EOF'
@read1
ATCGATCGATCGATCGATCGATCG
+
IIIIIIIIIIIIIIIIIIIIIIII
@read2
GCTAGCTAGCTAGCTAGCTAGCTA
+
JJJJJJJJJJJJJJJJJJJJJJJJ
EOF

echo "✓ Test data created"

# Run pipeline
echo -e "\n[2/3] Running pipeline with test data..."
python scripts/run_ngs_agent.py \
    --input test_data/input \
    --output test_data/results \
    --verbose

# Check results
echo -e "\n[3/3] Verifying results..."
if [ -d "test_data/results" ]; then
    file_count=$(ls -1 test_data/results 2>/dev/null | wc -l)
    echo "✓ Results directory contains $file_count files"
else
    echo "✗ Results directory not found"
    exit 1
fi

echo -e "\n=================================="
echo "Quick test completed successfully! ✓"
echo ""
echo "To run on real data:"
echo "  python scripts/run_ngs_agent.py --input ./data/input --output ./results"
