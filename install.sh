#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.local/bin"
TARGET="${TARGET_DIR}/git-graph"

mkdir -p "${TARGET_DIR}"
chmod +x "${SCRIPT_DIR}/generate.py"
ln -snf "${SCRIPT_DIR}/generate.py" "${TARGET}"

echo "Installed: ${TARGET}"
echo
echo "Usage:"
echo "  git-graph                    # analyze current repo"
echo "  git-graph /path/to/repo      # analyze another repo"
echo "  git-graph /path/to/repo --no-fetch"
echo "  git-graph /path/to/repo --open"
echo

case ":${PATH}:" in
  *":${TARGET_DIR}:"*)
    echo "${TARGET_DIR} is already in PATH."
    ;;
  *)
    echo "Add this to your shell rc if needed:"
    echo "  export PATH=\"${TARGET_DIR}:\$PATH\""
    ;;
esac
