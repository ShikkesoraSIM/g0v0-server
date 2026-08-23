import mimetypes

from app.dependencies.storage import StorageService as StorageServiceDep
from app.storage import LocalStorageService

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

file_router = APIRouter(prefix="/file", include_in_schema=False)

# las portadas de los sets subidos aca se piden de a decenas en cada scroll del
# listado. Sin Cache-Control el cliente y el navegador las vuelven a bajar TODAS cada
# vez (eran ~770 ms por imagen, cada vez), asi que el listado parecia trabado y las
# portadas quedaban en negro un rato largo. El contenido de una portada no cambia sin
# cambiar de nombre: una semana de cache es conservador.
_IMAGE_CACHE = "public, max-age=604800, stale-while-revalidate=86400"
_DEFAULT_CACHE = "public, max-age=3600"


@file_router.get("/{path:path}")
async def get_file(path: str, storage: StorageServiceDep):
    if not isinstance(storage, LocalStorageService):
        raise HTTPException(404, "Not Found")
    if not await storage.is_exists(path):
        raise HTTPException(404, "Not Found")

    # el tipo de verdad, no octet-stream: asi el navegador la trata como imagen y
    # cualquier cache intermedia la guarda. Y sin filename, porque mandar
    # Content-Disposition: attachment en una portada es pedirle al browser que la baje.
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    is_image = media_type.startswith("image/")

    try:
        return FileResponse(
            path=storage._get_file_path(path),
            media_type=media_type,
            headers={"Cache-Control": _IMAGE_CACHE if is_image else _DEFAULT_CACHE},
        )
    except FileNotFoundError:
        raise HTTPException(404, "Not Found")
