#!/usr/bin/env bash
# Deploy de g0v0 con chequeos y rollback automatico.
#
# Existe por un incidente: un deploy levanto "healthy" y estuvo 10 horas sin
# poder guardar un solo score, porque el error solo aparecia cuando alguien
# mandaba uno. "El contenedor arranco" no prueba nada del camino de submit.
#
# Uso:  ./deploy.sh          (pull + build + up + chequeos)
#       ./deploy.sh --no-pull
set -uo pipefail

cd "$(dirname "$0")"
rojo() { printf "\033[31m%s\033[0m\n" "$*"; }
verde() { printf "\033[32m%s\033[0m\n" "$*"; }
paso() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }

ANTERIOR=$(git rev-parse --short HEAD)
IMAGEN_ANTERIOR="g0v0-server-app:rollback-$(date -u +%Y%m%d-%H%M%S)"

abortar() {
  rojo "FALLO: $*"
  paso "Volviendo a $ANTERIOR"
  git reset --hard -q "$ANTERIOR"
  if docker image inspect "$IMAGEN_ANTERIOR" >/dev/null 2>&1; then
    docker tag "$IMAGEN_ANTERIOR" g0v0-server-app:latest
    docker compose up -d app >/dev/null 2>&1
    verde "Rollback hecho: corriendo $ANTERIOR de nuevo"
  else
    rojo "No habia imagen previa para restaurar; revisa a mano"
  fi
  exit 1
}

# 1. Lint estatico ANTES de construir nada. Barato y atrapa la clase de bug que
#    compila bien y revienta en runtime.
paso "Lint de shadowing"
python3 deploy/lint_shadow.py app || abortar "el lint encontro locals que pisan globals"

# 2. Guardar la imagen actual para poder volver.
paso "Guardando la imagen actual como $IMAGEN_ANTERIOR"
docker tag g0v0-server-app:latest "$IMAGEN_ANTERIOR" 2>/dev/null || echo "  (no habia imagen previa)"

# 3. Traer y construir.
if [[ "${1:-}" != "--no-pull" ]]; then
  paso "git pull"
  git pull --ff-only origin master || abortar "el pull fallo"
fi

paso "Build"
docker compose build app || abortar "el build fallo"

paso "Levantando"
docker compose up -d app || abortar "no levanto"

# 4. Esperar a que este healthy.
paso "Esperando health"
for _ in $(seq 1 40); do
  estado=$(docker inspect osu_api_server --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  [[ "$estado" == "healthy" ]] && break
  sleep 3
done
[[ "$estado" == "healthy" ]] || abortar "no llego a healthy (quedo en '$estado')"
verde "  healthy"

# 5. EL chequeo que importa: que un score se pueda procesar de verdad.
#    Hace rollback, no deja nada escrito.
paso "Smoke test de submit"
docker compose exec -T app python deploy/smoke_test.py 2>&1 | grep -viE "UserWarning|warnings.warn" || abortar "el smoke test de submit fallo"

# 6. Que no haya aparecido nada feo en el arranque.
paso "Revisando el log"
sucio=$(docker compose logs app --since 2m 2>&1 | sed 's/\x1b\[[0-9;]*m//g' \
        | grep -cE "UnboundLocalError|AttributeError|ImportError|NameError" || true)
[[ "$sucio" -eq 0 ]] || abortar "aparecieron $sucio excepciones sospechosas en el log"

verde ""
verde "Deploy ok: $(git rev-parse --short HEAD)"
verde "Imagen anterior guardada como $IMAGEN_ANTERIOR por si hay que volver"
