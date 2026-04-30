#!/usr/bin/env Rscript
# NGS-Agent v2: DESeq2 Analysis Script
# Performs differential expression analysis with visualization

library(DESeq2)
library(ggplot2)
library(pheatmap)
library(org.Hs.eg.db)
library(clusterProfiler)

# Command line arguments
args <- commandArgs(trailingOnly = TRUE)

if (length(args) < 2) {
  stop("Usage: Rscript deseq2_analysis.R <count_matrix> <metadata> [output_dir]")
}

count_file <- args[1]
metadata_file <- args[2]
output_dir <- ifelse(length(args) >= 3, args[3], "./results")

dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)

cat("Loading count data from:", count_file, "\n")
cat("Loading metadata from:", metadata_file, "\n")

# Load count matrix
counts <- read.csv(count_file, row.names = 1, sep = "\t")
counts <- as.matrix(counts)

# Load metadata
metadata <- read.csv(metadata_file, row.names = 1, sep = "\t")

# Create DESeq2 object
cat("Creating DESeqDataSet object...\n")
dds <- DESeqDataSetFromMatrix(
  countData = counts,
  colData = metadata,
  design = ~ condition
)

# Pre-filtering: keep genes with at least 10 reads
keep <- rowSums(counts(dds)) >= 10
dds <- dds[keep, ]
cat("Retained", nrow(dds), "genes after filtering\n")

# Run DESeq2
cat("Running DESeq2 analysis...\n")
dds <- DESeq(dds)

# Get results
res <- results(dds, alpha = 0.05)
res_sorted <- res[order(res$padj), ]

# Save results table
cat("Saving results...\n")
results_df <- as.data.frame(res_sorted)
write.csv(results_df, file.path(output_dir, "deseq2_results.csv"))

# Extract significant genes
sig_genes <- res_sorted[res_sorted$padj < 0.05 & !is.na(res_sorted$padj), ]
cat("Found", nrow(sig_genes), "significant genes\n")

up_genes <- sig_genes[sig_genes$log2FoldChange > 0, ]
down_genes <- sig_genes[sig_genes$log2FoldChange < 0, ]
cat("  Upregulated:", nrow(up_genes), "\n")
cat("  Downregulated:", nrow(down_genes), "\n")

# ===== VISUALIZATION =====

# 1. PCA Plot
cat("Generating PCA plot...\n")
vst <- vst(dds, blind = TRUE)
pca_data <- plotPCA(vst, intgroup = "condition", returnData = TRUE)

pca_plot <- ggplot(pca_data, aes(x = PC1, y = PC2, color = condition)) +
  geom_point(size = 4) +
  theme_minimal() +
  ggtitle("Principal Component Analysis (PCA)")

ggsave(file.path(output_dir, "pca_plot.png"), pca_plot, width = 8, height = 6, dpi = 300)

# 2. Volcano Plot
cat("Generating volcano plot...\n")
res_df <- as.data.frame(res_sorted)
res_df$sig <- ifelse(res_df$padj < 0.05 & !is.na(res_df$padj), "Significant", "Not Significant")

volcano_plot <- ggplot(res_df, aes(x = log2FoldChange, y = -log10(padj), color = sig)) +
  geom_point(alpha = 0.6) +
  scale_color_manual(values = c("Significant" = "red", "Not Significant" = "gray")) +
  theme_minimal() +
  ggtitle("Volcano Plot: Log2 FC vs -log10(FDR)") +
  xlab("log2(Fold Change)") +
  ylab("-log10(Adjusted p-value)")

ggsave(file.path(output_dir, "volcano_plot.png"), volcano_plot, width = 10, height = 7, dpi = 300)

# 3. MA Plot
cat("Generating MA plot...\n")
png(file.path(output_dir, "ma_plot.png"), width = 800, height = 600)
plotMA(res, main = "MA Plot: Average Expression vs Log2 Fold Change")
dev.off()

# 4. Top Genes Heatmap
cat("Generating heatmap of top 50 genes...\n")
top_genes <- head(rownames(res_sorted[!is.na(res_sorted$padj), ]), 50)
heatmap_data <- counts(dds, normalized = TRUE)[top_genes, ]

png(file.path(output_dir, "heatmap_top50.png"), width = 1000, height = 800)
pheatmap(
  heatmap_data,
  scale = "row",
  main = "Top 50 Differentially Expressed Genes",
  fontsize_col = 10,
  fontsize_row = 6
)
dev.off()

# 5. Expression Distribution Boxplot
cat("Generating expression boxplot...\n")
norm_counts <- as.data.frame(counts(dds, normalized = TRUE))
norm_counts$gene <- rownames(norm_counts)

expr_plot <- ggplot(norm_counts, aes(x = sample, y = log2(Count + 1))) +
  geom_boxplot() +
  theme_minimal() +
  theme(axis.text.x = element_text(angle = 45, hjust = 1)) +
  ggtitle("Distribution of Normalized Gene Expression")

ggsave(file.path(output_dir, "expression_distribution.png"), expr_plot, width = 10, height = 6, dpi = 300)

# Save summary statistics
cat("Saving summary statistics...\n")
summary_stats <- data.frame(
  Metric = c("Total Genes", "Genes After Filtering", "Significant Genes (padj < 0.05)",
             "Upregulated", "Downregulated", "Mean Normalized Counts", "Median Normalized Counts"),
  Value = c(
    nrow(counts),
    nrow(dds),
    nrow(sig_genes),
    nrow(up_genes),
    nrow(down_genes),
    round(mean(counts(dds, normalized = TRUE)), 2),
    round(median(counts(dds, normalized = TRUE)), 2)
  )
)

write.csv(summary_stats, file.path(output_dir, "summary_statistics.csv"), row.names = FALSE)

cat("\n=== Analysis Complete ===\n")
cat("Results saved to:", output_dir, "\n")
cat("Output files:\n")
cat("  - deseq2_results.csv\n")
cat("  - pca_plot.png\n")
cat("  - volcano_plot.png\n")
cat("  - ma_plot.png\n")
cat("  - heatmap_top50.png\n")
cat("  - expression_distribution.png\n")
cat("  - summary_statistics.csv\n")
