#!/bin/bash
# Prepara o ambiente nas sessões do Claude Code na nuvem: .venv com as
# dependências, assets sintéticos (efeitos e música) e um .env a partir do
# exemplo. No PC do usuário (Windows) não faz nada.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

# Gera efeitos e trilha sintéticos que faltarem em assets/ (fora do Git)
.venv/bin/python setup_assets.py > /dev/null

# .env sem chaves: as chaves reais vêm das variáveis do ambiente da nuvem
if [ ! -f .env ]; then
  cp .env.example .env
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
