#!/usr/bin/env Rscript
# GO over-representation analysis for differentially expressed genes.
#
# Usage:
#   Rscript enrichment_analysis.R <input_csv> <organism> <output_dir>
#
# input_csv: DESeq2 results CSV (deseq2_results.csv) containing `gene` and
#            `padj` columns, or a single-column CSV of gene symbols.
# organism:  "human" or "mouse".
# output_dir: receives go_enrichment.csv and go_enrichment.html.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  stop("Usage: Rscript enrichment_analysis.R <input_csv> <organism> <output_dir>")
}
input_csv <- args[1]
organism <- tolower(args[2])
out_dir <- args[3]

dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

suppressPackageStartupMessages({
  library(clusterProfiler)
})

org_db <- switch(organism,
  human = "org.Hs.eg.db",
  mouse = "org.Mm.eg.db",
  stop("Unsupported organism: ", organism, " (use human or mouse)")
)
suppressPackageStartupMessages(library(org_db, character.only = TRUE))
org_db_obj <- get(org_db)

read_table_auto <- function(path) {
  if (grepl("\\.csv$", path, ignore.case = TRUE)) {
    utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
  } else {
    utils::read.delim(path, check.names = FALSE, stringsAsFactors = FALSE)
  }
}

input_df <- read_table_auto(input_csv)
if ("padj" %in% names(input_df) && "gene" %in% names(input_df)) {
  sig <- input_df[!is.na(input_df$padj) & input_df$padj < 0.05, "gene"]
  universe <- input_df[!is.na(input_df$padj), "gene"]
} else {
  sig <- input_df[[1]]
  universe <- NULL
}
sig <- as.character(unique(sig[!is.na(sig) & nzchar(sig)]))
if (length(sig) < 2) {
  utils::write.csv(data.frame(), file.path(out_dir, "go_enrichment.csv"), row.names = FALSE)
  cat("Fewer than 2 significant genes; no enrichment performed.\n")
  quit(save = "no", status = 0)
}

ego <- enrichGO(
  gene = sig,
  universe = universe,
  OrgDb = org_db_obj,
  keyType = "SYMBOL",
  ont = "BP",
  pAdjustMethod = "BH",
  pvalueCutoff = 0.05,
  qvalueCutoff = 0.10,
  readable = TRUE
)

result_df <- as.data.frame(ego)
utils::write.csv(result_df, file.path(out_dir, "go_enrichment.csv"), row.names = FALSE)

# Self-contained HTML summary (no external JS dependencies).
if (nrow(result_df) > 0) {
  top_n <- head(result_df, 25)
  rows <- paste0(
    "<tr><td>", top_n$ID,
    "</td><td>", top_n$Description,
    "</td><td>", top_n$GeneRatio,
    "</td><td>", signif(top_n$p.adjust, 3),
    "</td><td>", top_n$Count, "</td></tr>",
    collapse = "\n"
  )
  html <- paste0(
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>GO Enrichment</title>",
    "<style>body{font-family:Arial,sans-serif;margin:2rem;}table{border-collapse:collapse;width:100%;}",
    "th,td{border:1px solid #ccc;padding:0.4rem;text-align:left;}th{background:#f3f4f6;}</style></head>",
    "<body><h1>GO Biological Process Enrichment</h1>",
    "<p>Organism: ", organism, "; significant genes: ", length(sig), "; terms shown: ", nrow(top_n), "</p>",
    "<table><tr><th>GO ID</th><th>Description</th><th>Gene ratio</th><th>Adj. p</th><th>Genes</th></tr>",
    rows, "</table></body></html>"
  )
  writeLines(html, file.path(out_dir, "go_enrichment.html"))
} else {
  writeLines("<!DOCTYPE html><html><body><h1>GO Enrichment</h1><p>No significantly enriched terms.</p></body></html>",
             file.path(out_dir, "go_enrichment.html"))
}

cat("Enrichment analysis complete:", file.path(out_dir, "go_enrichment.csv"), "\n")
