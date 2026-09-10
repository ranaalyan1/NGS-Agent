#!/usr/bin/env Rscript
# DESeq2 differential expression analysis.
# Usage: Rscript deseq2_analysis.R <count_matrix> <sample_sheet> <output_dir> [--contrast cond2vscond1]
suppressPackageStartupMessages({
  library(DESeq2)
  library(ggplot2)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  stop("Usage: deseq2_analysis.R <count_matrix> <sample_sheet> <output_dir> [--contrast A_vs_B]")
}
count_path <- args[1]
sample_sheet_path <- args[2]
out_dir <- args[3]
contrast_arg <- NULL
if (length(args) >= 5 && args[4] == "--contrast") {
  contrast_arg <- args[5]
}

dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

read_table_auto <- function(path) {
  first_line <- readLines(path, n = 1)
  sep <- if (grepl("\t", first_line)) "\t" else ","
  read.table(path, header = TRUE, sep = sep, comment.char = "#",
             check.names = FALSE, stringsAsFactors = FALSE)
}

counts <- read_table_auto(count_path)
samples <- read_table_auto(sample_sheet_path)

if (!all(c("sample_id", "condition") %in% colnames(samples))) {
  stop("sample_sheet must include sample_id and condition columns")
}

rownames(counts) <- counts[[1]]
# featureCounts emits Chr/Start/End/Strand/Length metadata columns; drop non-sample columns.
meta_cols <- c(colnames(counts)[1], "Chr", "Start", "End", "Strand", "Length")
count_mat <- as.matrix(counts[, !(colnames(counts) %in% meta_cols), drop = FALSE])
# featureCounts uses BAM paths as column names; reduce to file stems.
colnames(count_mat) <- sub("\\.bam$", "", basename(colnames(count_mat)))

common <- intersect(colnames(count_mat), samples$sample_id)
if (length(common) < 2) stop("Count matrix and sample sheet do not overlap enough for DESeq2")
count_mat <- count_mat[, common, drop = FALSE]
samples <- samples[match(common, samples$sample_id), , drop = FALSE]
rownames(samples) <- samples$sample_id
if (length(unique(samples$condition)) < 2) stop("Need at least two conditions for DESeq2")

dds <- DESeqDataSetFromMatrix(
  countData = round(count_mat),
  colData = samples,
  design = ~condition
)
dds <- DESeq(dds)

cond_levels <- levels(factor(samples$condition))
if (!is.null(contrast_arg) && grepl("_vs_", contrast_arg)) {
  parts <- strsplit(contrast_arg, "_vs_")[[1]]
  res <- results(dds, contrast = c("condition", parts[1], parts[2]))
} else {
  res <- results(dds, contrast = c("condition", cond_levels[2], cond_levels[1]))
}

res_df <- as.data.frame(res)
res_df <- cbind(gene = rownames(res_df), res_df)
res_df <- res_df[order(res_df$padj), ]
write.csv(res_df, file.path(out_dir, "deseq2_results.csv"), row.names = FALSE)

# PCA plot
vsd <- tryCatch(vst(dds, blind = FALSE), error = function(e) varianceStabilizingTransformation(dds, blind = FALSE))
pca_data <- plotPCA(vsd, intgroup = "condition", returnData = TRUE)
percent_var <- round(100 * attr(pca_data, "percentVar"))
pca_plot <- ggplot(pca_data, aes(PC1, PC2, color = condition)) +
  geom_point(size = 4) +
  theme_minimal(base_size = 14) +
  labs(
    title = "PCA of RNA-Seq Samples",
    x = paste0("PC1 (", percent_var[1], "%)"),
    y = paste0("PC2 (", percent_var[2], "%)")
  )
ggsave(file.path(out_dir, "pca_plot.png"), pca_plot, width = 8, height = 6, dpi = 300)

# Volcano plot
volcano_df <- res_df[!is.na(res_df$padj), ]
volcano_df$significant <- volcano_df$padj < 0.05 & abs(volcano_df$log2FoldChange) > 1
volcano <- ggplot(volcano_df, aes(x = log2FoldChange, y = -log10(padj), color = significant)) +
  geom_point(alpha = 0.7) +
  scale_color_manual(values = c("FALSE" = "grey60", "TRUE" = "firebrick")) +
  theme_minimal(base_size = 14) +
  labs(title = "Volcano Plot", x = "Log2 Fold Change", y = "-log10 adjusted p-value")
ggsave(file.path(out_dir, "volcano_plot.png"), volcano, width = 8, height = 7, dpi = 300)

# Heatmap of top-variance genes
mat <- assay(vsd)
vars <- apply(mat, 1, var)
top_var <- head(order(vars, decreasing = TRUE), 50)
hm <- mat[top_var, , drop = FALSE]
hm_z <- t(scale(t(hm)))
png(file.path(out_dir, "heatmap.png"), width = 2400, height = 2000, res = 300)
heatmap(hm_z, main = "Top 50 variable genes (Z-score)")
invisible(dev.off())

n_sig <- sum(!is.na(res_df$padj) & res_df$padj < 0.05)
cat(sprintf("DESeq2 analysis completed. Genes tested: %d, significant (padj<0.05): %d\n",
            nrow(res_df), n_sig))
