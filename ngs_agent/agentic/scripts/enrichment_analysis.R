#!/usr/bin/env Rscript
# GO enrichment analysis from DESeq2 results (expects gene, log2FoldChange, padj columns).
# Usage: Rscript enrichment_analysis.R <deseq2_results_csv> <organism> <output_dir>
suppressPackageStartupMessages({
  library(clusterProfiler)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  stop("Usage: enrichment_analysis.R <deseq2_results_csv> <organism> <output_dir>")
}
de_path <- args[1]
organism <- tolower(args[2])
out_dir <- args[3]

dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
csv_out <- file.path(out_dir, "go_enrichment.csv")
html_out <- file.path(out_dir, "go_enrichment.html")

orgdb_name <- switch(organism,
  "human" = "org.Hs.eg.db",
  "mouse" = "org.Mm.eg.db",
  "rat" = "org.Rn.eg.db",
  stop(sprintf("Unsupported organism '%s'. Use human, mouse, or rat.", organism))
)
suppressPackageStartupMessages(library(orgdb_name, character.only = TRUE))
orgdb <- get(orgdb_name)

de <- read.csv(de_path, stringsAsFactors = FALSE)
if (!all(c("gene", "log2FoldChange", "padj") %in% colnames(de))) {
  stop("DE results must include gene, log2FoldChange, and padj columns")
}

sig <- de[!is.na(de$padj) & de$padj < 0.05 & abs(de$log2FoldChange) > 1, ]
genes <- unique(sig$gene)

write_empty <- function() {
  write.csv(
    data.frame(ID = character(), Description = character(), p.adjust = numeric()),
    csv_out, row.names = FALSE
  )
  writeLines("<html><body><p>No significant genes for GO enrichment.</p></body></html>", html_out)
}

if (length(genes) == 0) {
  write_empty()
  cat("No significant genes; wrote empty GO enrichment outputs.\n")
  quit(save = "no")
}

entrez <- tryCatch(
  bitr(genes, fromType = "SYMBOL", toType = "ENTREZID", OrgDb = orgdb),
  error = function(e) data.frame()
)
if (nrow(entrez) == 0) {
  write_empty()
  cat("No genes mapped to Entrez IDs; wrote empty GO enrichment outputs.\n")
  quit(save = "no")
}

ego <- enrichGO(
  gene = unique(entrez$ENTREZID),
  OrgDb = orgdb,
  keyType = "ENTREZID",
  ont = "BP",
  pAdjustMethod = "BH",
  readable = TRUE
)

go_df <- as.data.frame(ego)
write.csv(go_df, csv_out, row.names = FALSE)

rows <- if (nrow(go_df) > 0) {
  paste0(
    "<tr><td>", go_df$ID, "</td><td>", go_df$Description,
    "</td><td>", signif(go_df$p.adjust, 3), "</td><td>", go_df$Count, "</td></tr>",
    collapse = "\n"
  )
} else {
  "<tr><td colspan='4'>No enriched GO terms found.</td></tr>"
}
html <- paste0(
  "<html><head><title>GO Enrichment</title></head><body>",
  "<h1>GO Enrichment (Biological Process)</h1>",
  "<table border='1' cellpadding='4'>",
  "<tr><th>ID</th><th>Description</th><th>Adj. p-value</th><th>Count</th></tr>",
  rows,
  "</table></body></html>"
)
writeLines(html, html_out)
cat(sprintf("GO enrichment completed. Significant genes: %d, enriched terms: %d\n",
            length(genes), nrow(go_df)))
