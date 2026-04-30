#!/usr/bin/env Rscript
# NGS-Agent v2: R Package Installer
# Installs all required R packages for DESeq2 analysis and plotting

if (!require("BiocManager", quietly = TRUE)) {
  install.packages("BiocManager")
}

# Package list
packages <- c(
  # CRAN packages
  "ggplot2",
  "pheatmap",
  "igraph",
  
  # Bioconductor packages
  "DESeq2",
  "clusterProfiler",
  "org.Hs.eg.db",
  "limma",
  "edgeR",
  "EnhancedVolcano"
)

cat("Installing R packages for NGS-Agent v2...\n")
cat("============================================\n\n")

# Install CRAN packages
cran_packages <- c("ggplot2", "pheatmap", "igraph")

for (pkg in cran_packages) {
  cat("Installing", pkg, "...\n")
  if (!require(pkg, character.only = TRUE, quietly = TRUE)) {
    install.packages(pkg, repos = "https://cran.r-project.org")
    library(pkg, character.only = TRUE)
  }
}

cat("\nInstalling Bioconductor packages...\n")

# Install Bioconductor packages
bioc_packages <- c("DESeq2", "clusterProfiler", "org.Hs.eg.db", "limma", "edgeR", "EnhancedVolcano")

for (pkg in bioc_packages) {
  cat("Installing", pkg, "...\n")
  if (!require(pkg, character.only = TRUE, quietly = TRUE)) {
    BiocManager::install(pkg)
    library(pkg, character.only = TRUE)
  }
}

cat("\n============================================\n")
cat("Installation complete!\n")
cat("All required R packages are now installed.\n")
