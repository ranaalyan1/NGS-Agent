#!/usr/bin/env bash
set -euo pipefail

log() {
  printf "[ngs-install] %s\n" "$1"
}

fail() {
  printf "[ngs-install] ERROR: %s\n" "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

install_micromamba() {
  local install_dir="${HOME}/.local/bin"
  mkdir -p "${install_dir}"

  if command -v curl >/dev/null 2>&1; then
    curl -Ls https://micro.mamba.pm/install.sh | bash >/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://micro.mamba.pm/install.sh | bash >/dev/null
  else
    fail "Neither curl nor wget is available to install micromamba"
  fi

  export PATH="${HOME}/.local/bin:${PATH}"
  command -v micromamba >/dev/null 2>&1 || fail "micromamba install failed"
}

select_conda_cmd() {
  if command -v mamba >/dev/null 2>&1; then
    echo "mamba"
    return
  fi
  if command -v micromamba >/dev/null 2>&1; then
    echo "micromamba"
    return
  fi
  if command -v conda >/dev/null 2>&1; then
    echo "conda"
    return
  fi

  log "No conda-compatible tool found, installing micromamba"
  install_micromamba
  echo "micromamba"
}

main() {
  require_cmd git
  require_cmd bash

  local repo_dir="${1:-$PWD}"
  if [ ! -f "${repo_dir}/environment.yml" ]; then
    fail "environment.yml not found in ${repo_dir}. Run installer from repo root or pass the path as argument."
  fi

  local conda_cmd
  conda_cmd="$(select_conda_cmd)"
  log "Using environment manager: ${conda_cmd}"

  if [ "${conda_cmd}" = "micromamba" ]; then
    "${conda_cmd}" create -y -n ngs-agent -f "${repo_dir}/environment.yml"
    eval "$(micromamba shell hook -s bash)"
    micromamba activate ngs-agent
  else
    "${conda_cmd}" env update -n ngs-agent -f "${repo_dir}/environment.yml" --prune || \
    "${conda_cmd}" env create -n ngs-agent -f "${repo_dir}/environment.yml"
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate ngs-agent
  fi

  python -m pip install --upgrade pip
  python -m pip install -e "${repo_dir}"

  if [ ! -f "${repo_dir}/ngs.toml" ] && [ -f "${repo_dir}/ngs.toml.example" ]; then
    cp "${repo_dir}/ngs.toml.example" "${repo_dir}/ngs.toml"
    log "Created ${repo_dir}/ngs.toml from template"
  fi

  log "Installation complete."
  log "Run: conda activate ngs-agent && ngs doctor"
}

main "$@"
