#!/usr/bin/env Rscript
# DESeq2 differential expression analysis.
#
# Usage:
#   Rscript deseq2_analysis.R <count_matrix> <sample_sheet> <output_dir> [--contrast "<numerator>:<denominator>"]
#
# count_matrix: featureCounts output (or any TSV/CSV with gene rows and
#               per-sample integer count columns; comment/header rows that
#               featureCounts writes are skipped automatically).
# sample_sheet: CSV/TSV with columns `sample` and `condition` (plus optional
#               `batch` covariate). Sample names must match count columns.
# output_dir:   receives deseq2_results.csv, pca_plot.png, volcano_plot.png,
#               heatmap.png, and size_factors.csv.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  stop("Usage: Rscript deseq2_analysis.R <count_matrix> <sample_sheet> <output_dir> [--contrast num:denom]")
}
count_path <- args[1]
sample_path <- args[2]
out_dir <- args[3]
contrast <- if (length(args) >= 5 && args[4] == "--contrast") args[5] else NULL

dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

suppressPackageStartupMessages({
  library(DESeq2)
})

read_table_auto <- function(path) {
  if (grepl("\\.csv$", path, ignore.case = TRUE)) {
    utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
  } else {
    utils::read.delim(path, check.names = FALSE, stringsAsFactors = FALSE)
  }
}

# --- Load counts (skip featureCounts comment lines starting with '#') -----
counts_raw <- read_table_auto(count_path)
counts_raw <- counts_raw[!startsWith(trimws(as.character(counts_raw[[1]])), "#"), ]
gene_ids <- as.character(counts_raw[[1]])
gene_col_name <- names(counts_raw)[1]
counts <- counts_raw[, setdiff(names(counts_raw), gene_col_name), drop = FALSE]
rownames(counts) <- gene_ids
counts <- as.matrix(counts)
storage.mode(counts) <- "integer"

# --- Load sample metadata --------------------------------------------------
samples <- read_table_auto(sample_path)
sample_col <- intersect(c("sample", "sample_id", "Sample"), names(samples))[1]
condition_col <- intersect(c("condition", "group", "treatment"), names(samples))[1]
if (is.na(sample_col) || is.na(condition_col)) {
  stop("Sample sheet must contain 'sample' and 'condition' (or group/treatment) columns.")
}
rownames(samples) <- samples[[sample_col]]
samples[[condition_col]] <- factor(samples[[condition_col]])

# Keep only samples present in both matrices; order consistently.
common <- intersect(colnames(counts), rownames(samples))
if (length(common) < 2) stop("Fewer than 2 shared samples between counts and sample sheet.")
counts <- counts[, common, drop = FALSE]
samples <- samples[common, , drop = FALSE]

# --- DESeq2 run ------------------------------------------------------------
design_formula <- stats::as.formula(paste("~", condition_col))
if ("batch" %in% names(samples)) {
  samples$batch <- factor(samples$batch)
  design_formula <- stats::as.formula(paste("~ batch +", condition_col))
}

dds <- DESeqDataSetFromMatrix(countData = counts, colData = samples, design = design_formula)
dds <- DESeq(dds)
results_name <- NULL
if (!is.null(contrast)) {
  parts <- strsplit(contrast, ":")[[1]]
  res <- results(dds, contrast = c(condition_col, parts[1], parts[2]))
} else {
  available <- resultsNames(dds)
  results_name <- available[length(available)]
  res <- if (length(available) > 1) results(dds, name = results_name) else results(dds)
}

out_df <- as.data.frame(res)
out_df$gene <- rownames(out_df)
out_df$significant <- out_df$padj < 0.05 & !is.na(out_df$padj)
utils::write.csv(out_df, file.path(out_dir, "deseq2_results.csv"), row.names = FALSE)
utils::write.csv(data.frame(sample = colnames(dds), size_factor = sizeFactors(dds)),
                 file.path(out_dir, "size_factors.csv"), row.names = FALSE)

# --- Plots -----------------------------------------------------------------
grDevices::png(file.path(out_dir, "pca_plot.png"), width = 900, height = 700)
print(plotPCA(rlog(dds, blind = FALSE), intgroup = condition_col))
grDevices::dev.off()

grDevices::png(file.path(out_dir, "volcano_plot.png"), width = 900, height = 700)
volcano_df <- out_df[!is.na(out_df$padj), ]
volcano_df$log10_padj <- -log10(volcano_df$padj)
print(lattice::xyplot(log10_padj ~ log2FoldChange,
                      group = significant,
                      data = volcano_df,
                      auto.key = list(title = "padj < 0.05", columns = 2),
                      xlab = "log2 fold change", ylab = "-log10(padj)"))
grDevices::dev.off()

grDevices::png(file.path(out_dir, "heatmap.png"), width = 900, height = 700)
top_genes <- head(order(out_df$padj, na.last = NA), n = 25)
mat <- counts(top_genes, normalized = TRUE)
pheatmap::pheatmap(mat, annotation_col = as.data.frame(samples[[condition_col]], row.names = rownames(samples),
                                                       optional = TRUE) |>
                     stats::setNames(condition_col),
                   main = "Top 25 genes by adjusted p-value", silent = TRUE)
grDevices::dev.off()

cat("DESeq2 analysis complete. Results:", file.path(out_dir, "deseq2_results.csv"), "\n")
