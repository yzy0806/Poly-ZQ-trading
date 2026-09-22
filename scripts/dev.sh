#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/share/zq-arb/venvs/mac-py314}"
export UV_PYTHON="${UV_PYTHON:-$(cat "$PROJECT_DIR/.python-version")}"
export UV_PYTHON_DOWNLOADS=never
export ZQ_ENV_FILE="${ZQ_ENV_FILE:-$HOME/.config/zq-arb/development.env}"
export PYTHONDONTWRITEBYTECODE=1

run_python() { uv run --locked --extra dev "$@"; }
check_node() {
    ZQ_NODE_EXPECTED="v$(cat "$PROJECT_DIR/.node-version")"
    ZQ_NODE_ACTUAL=$(node --version)
    if [ "$ZQ_NODE_ACTUAL" != "$ZQ_NODE_EXPECTED" ]; then
        printf 'Node %s is installed; the shared toolchain expects %s. Update Mac, CI and production pins together.\n' "$ZQ_NODE_ACTUAL" "$ZQ_NODE_EXPECTED" >&2
        exit 1
    fi
}

case "${1:-help}" in
    sync)
        check_node
        uv sync --locked --extra dev
        (cd web && npm ci --ignore-scripts)
        ;;
    check)
        check_node
        run_python ruff check src tests scripts
        run_python mypy src
        run_python pytest --cov
        (cd web && npm run lint && npm test -- --run && npm run build)
        ;;
    backend)
        exec uv run --locked --extra dev python -m zq_arb.main
        ;;
    web)
        check_node
        cd web
        exec npm run dev
        ;;
    run)
        shift
        [ "$#" -gt 0 ] || { printf '%s\n' 'Usage: scripts/dev.sh run COMMAND...' >&2; exit 1; }
        exec uv run --locked --extra dev "$@"
        ;;
    *)
        printf '%s\n' 'Usage: scripts/dev.sh {sync|check|backend|web|run COMMAND...}'
        ;;
esac
