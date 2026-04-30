"""
NGS-Agent v2: File Management Utilities
Handles file operations, validation, and organization.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional


class FileManager:
    """Manages file operations for the pipeline."""
    
    @staticmethod
    def ensure_dir(path: str) -> Path:
        """Ensure directory exists and return as Path."""
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return p
    
    @staticmethod
    def find_fastq_files(directory: str) -> List[str]:
        """Find all FASTQ files in directory."""
        patterns = ['*.fastq', '*.fastq.gz', '*.fq', '*.fq.gz']
        files = []
        
        for pattern in patterns:
            files.extend(Path(directory).glob(pattern))
        
        return sorted([str(f) for f in files])
    
    @staticmethod
    def find_paired_fastq(directory: str) -> tuple[List[str], List[str]]:
        """
        Find paired-end FASTQ files.
        
        Returns:
            Tuple of (R1_files, R2_files)
        """
        r1_pattern = ['*_R1.fastq.gz', '*_R1.fastq', '*_1.fastq.gz', '*_1.fastq']
        r2_pattern = ['*_R2.fastq.gz', '*_R2.fastq', '*_2.fastq.gz', '*_2.fastq']
        
        r1_files = []
        r2_files = []
        
        for pattern in r1_pattern:
            r1_files.extend(Path(directory).glob(pattern))
        
        for pattern in r2_pattern:
            r2_files.extend(Path(directory).glob(pattern))
        
        return sorted([str(f) for f in r1_files]), sorted([str(f) for f in r2_files])
    
    @staticmethod
    def count_lines(filepath: str) -> int:
        """Count lines in file (handles gzipped files)."""
        if filepath.endswith('.gz'):
            import gzip
            with gzip.open(filepath, 'rt') as f:
                return sum(1 for _ in f)
        else:
            with open(filepath, 'r') as f:
                return sum(1 for _ in f)
    
    @staticmethod
    def get_file_size_mb(filepath: str) -> float:
        """Get file size in MB."""
        return os.path.getsize(filepath) / (1024 * 1024)
    
    @staticmethod
    def copy_file(src: str, dst: str) -> bool:
        """Copy file with error handling."""
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as e:
            return False
    
    @staticmethod
    def remove_file(filepath: str) -> bool:
        """Remove file safely."""
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                return True
        except Exception:
            pass
        return False
