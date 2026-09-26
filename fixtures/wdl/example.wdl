version 1.0

workflow rnaseq {
  input {
    File fastq_r1
    File fastq_r2
  }

  call AlignTask {
    input:
      fastq_r1 = fastq_r1,
      fastq_r2 = fastq_r2
  }

  output {
    File bam = AlignTask.bam
  }
}

task AlignTask {
  input {
    File fastq_r1
    File fastq_r2
  }

  command <<<
    hisat2 -x /refs/grch38 -1 ~{fastq_r1} -2 ~{fastq_r2} | samtools sort -o aligned.bam
  >>>

  output {
    File bam = "aligned.bam"
  }

  runtime {
    docker: "quay.io/biocontainers/hisat2:2.2.1"
    memory: "32 GB"
  }
}
