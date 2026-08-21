"""Detecta el patron exacto que tumbo el submit de scores 10 horas.

Si una funcion ASIGNA un nombre que tambien es un global del modulo, python
trata ese nombre como local en TODA la funcion. Cualquier lectura anterior a la
asignacion tira UnboundLocalError en runtime, y solo cuando se ejecuta esa rama.
La sintaxis compila perfecto, asi que ni el build ni un "levanto healthy" lo ven.

Paso con `settings = dict(...)` adentro de process_score, que pisaba el settings
de config usado mas abajo.

Corre sobre app/ y sale != 0 si encuentra alguno.
"""
from __future__ import annotations

import ast
import pathlib
import sys


def globales_del_modulo(arbol: ast.Module) -> set[str]:
    """Nombres que el modulo importa o define arriba de todo."""
    nombres: set[str] = set()
    for nodo in arbol.body:
        if isinstance(nodo, (ast.Import, ast.ImportFrom)):
            for alias in nodo.names:
                if alias.name != "*":
                    nombres.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(nodo, ast.Assign):
            for t in nodo.targets:
                if isinstance(t, ast.Name):
                    nombres.add(t.id)
    return nombres


def asignaciones_condicionales(fn) -> dict[str, int]:
    """Nombres asignados DENTRO de un if/for/while/try/with, con su linea.

    Solo esas importan. Una asignacion en el cuerpo directo de la funcion corre
    siempre antes de las lecturas de mas abajo, asi que es inofensiva aunque
    pise un global (pasa con `bot = await session.get(...)`). El peligro es la
    que puede NO ejecutarse: ahi el nombre ya es local para toda la funcion pero
    se queda sin valor.
    """
    encontrados: dict[str, int] = {}

    def caminar(nodo, dentro_de_bloque: bool):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # otra scope, no es asunto nuestro
            anida = isinstance(hijo, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith))
            if dentro_de_bloque and isinstance(hijo, ast.Name) and isinstance(hijo.ctx, ast.Store):
                encontrados.setdefault(hijo.id, hijo.lineno)
            caminar(hijo, dentro_de_bloque or anida)

    caminar(fn, False)
    return encontrados


def revisar(path: pathlib.Path) -> list[str]:
    try:
        arbol = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    globales = globales_del_modulo(arbol)
    problemas: list[str] = []

    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        declarados_global: set[str] = set()
        leidos: dict[str, int] = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Global):
                declarados_global.update(n.names)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                leidos.setdefault(n.id, n.lineno)

        for nombre, linea in asignaciones_condicionales(fn).items():
            if nombre not in globales or nombre in declarados_global:
                continue
            if nombre not in leidos:
                continue
            problemas.append(
                f"{path}:{linea}: '{nombre}' se asigna dentro de un bloque condicional en "
                f"{fn.name}() y pisa el global del modulo (se lee en la linea {leidos[nombre]}) "
                f"-> UnboundLocalError si esa rama no corre"
            )

    return problemas


def main() -> int:
    raiz = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "app")
    problemas: list[str] = []
    for f in sorted(raiz.rglob("*.py")):
        problemas.extend(revisar(f))

    if problemas:
        print("SHADOW LINT: %d problema(s)" % len(problemas))
        for p in problemas:
            print("  " + p)
        return 1

    print("SHADOW LINT ok: ningun local pisa un global que se lea")
    return 0


if __name__ == "__main__":
    sys.exit(main())
