#!/usr/bin/env Rscript
# NGS-Agent v2: GO/KEGG Enrichment Analysis
# Performs pathway enrichment analysis on DE genes

library(clusterProfiler)
library(org.Hs.eg.db)
library(ggplot2)
library(igraph)

args <- commandArgs(trailingOnly = TRUE)

if (length(args) < 1) {
  stop("Usage: Rscript enrichment_analysis.R <deseq2_results> [output_dir]")
}

results_file <- args[1]
output_dir <- ifelse(length(args) >= 2, args[2], "./results")

dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)

cat("Loading DESeq2 results from:", results_file, "\n")
results <- read.csv(results_file, row.names = 1)

# Get significant genes
sig_threshold <- 0.05
lfc_threshold <- 1

sig_results <- results[
  !is.na(results$padj) & results$padj < sig_threshold &
  abs(results$log2FoldChange) > lfc_threshold,
]

cat("Found", nrow(sig_results), "significant DE genes\n")

# Convert gene names to Entrez IDs
gene_symbols <- rownames(sig_results)
entrez_ids <- bitr(gene_symbols, fromType = "SYMBOL", toType = "ENTREZID", OrgDb = org.Hs.eg.db)

if (nrow(entrez_ids) == 0) {
  cat("Warning: No gene IDs could be converted\n")
  quit(save = "no")
}

# ===== GO ENRICHMENT =====
cat("\nPerforming GO enrichment analysis...\n")

ego <- enrichGO(
  gene = entrez_ids$ENTREZID,
  OrgDb = org.Hs.eg.db,
  ont = "BP",
  pAdjustMethod = "BH",
  pvalueCutoff = 0.05,
  qvalueCutoff = 0.05
)

if (!is.null(ego) && nrow(ego) > 0) {
  cat("Found", nrow(ego), "enriched GO terms\n")
  
  # Save results
  go_results <- as.data.frame(ego)
  write.csv(go_results, file.path(output_dir, "go_enrichment.csv"), row.names = FALSE)
  
  # Plot
  png(file.path(output_dir, "go_barplot.png"), width = 1000, height = 800)
  print(barplot(ego, showCategory = 20, title = "Top 20 GO Biological Processes"))
  dev.off()
  
  png(file.path(output_dir, "go_network.png"), width = 1000, height = 800)
  print(cnetplot(ego, showCategory = 5, colorEdge = TRUE))
  dev.off()
}

# ===== KEGG ENRICHMENT =====
cat("\nPerforming KEGG pathway enrichment...\n")

kk <- enrichKEGG(
  gene = entrez_ids$ENTREZID,
  organism = "hsa",
  pAdjustMethod = "BH",
  pvalueCutoff = 0.05,
  qvalueCutoff = 0.05
)

if (!is.null(kk) && nrow(kk) > 0) {
  cat("Found", nrow(kk), "enriched KEGG pathways\n")
  
  # Save results
  kegg_results <- as.data.frame(kk)
  write.csv(kegg_results, file.path(output_dir, "kegg_enrichment.csv"), row.names = FALSE)
  
  # Plot
  png(file.path(output_dir, "kegg_barplot.png"), width = 1000, height = 800)
  print(barplot(kk, showCategory = 20, title = "Top 20 KEGG Pathways"))
  dev.off()
}

# ===== GENERATE INTERPRETATION =====
cat("\nGenerating biological interpretation...\n")

interpretation <- paste(
  "Pathway Enrichment Summary:\n",
  "================================\n\n"
)

if (!is.null(ego) && nrow(ego) > 0) {
  top_go <- ego@result[1:min(5, nrow(ego@result)), ]
  interpretation <- paste(interpretation,
    "Top GO Terms:\n",
    paste(paste("  -", top_go$Description), collapse = "\n"),
    "\n\n"
  )
}

if (!is.null(kk) && nrow(kk) > 0) {
  top_kegg <- kk@result[1:min(5, nrow(kk@result)), ]
  interpretation <- paste(interpretation,
    "Top KEGG Pathways:\n",
    paste(paste("  -", top_kegg$Description), collapse = "\n"),
    "\n"
  )
}

writeLines(interpretation, file.path(output_dir, "interpretation.txt"))

cat("\n=== Enrichment Analysis Complete ===\n")
cat("Results saved to:", output_dir, "\n")
