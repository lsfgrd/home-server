#!/usr/bin/env bash
# Uso: ./up.sh <servico>
# Cria, como o seu usuário, as pastas de /srv/appdata que o compose
# do serviço usa e que ainda não existem. Depois sobe o serviço.
set -euo pipefail

servico="${1:?Uso: ./up.sh <servico>}"
compose="$(dirname "$0")/$servico/compose.yaml"

[ -f "$compose" ] || { echo "Não encontrei $compose"; exit 1; }

if [ "$(id -u)" != "1000" ]; then
  echo "Aviso: seu UID é $(id -u), mas os composes usam 1000:1000."
fi

docker compose -f "$compose" config \
  | grep -oP 'source: \K/srv/appdata/\S+' \
  | while read -r pasta; do
      if [ ! -e "$pasta" ]; then
        echo "Criando $pasta"
        mkdir -p "$pasta"
      fi
    done

docker compose -f "$compose" up -d
