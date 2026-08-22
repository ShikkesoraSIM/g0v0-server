from . import beatmap  # noqa: F401
from .beatmapset import BeatmapsetModel

from sqlmodel import SQLModel

# tags va aca porque el cliente y la web lo miran para el badge de mapas hechos
# con IA; sin declararlo, la respuesta lo descarta aunque lo llenemos.
SearchBeatmapset = BeatmapsetModel.generate_typeddict(("beatmaps.max_combo", "pack_tags", "tags"))


class SearchBeatmapsetsResp(SQLModel):
    beatmapsets: list[SearchBeatmapset]  # pyright: ignore[reportInvalidTypeForm]
    total: int
    cursor: dict[str, int | float | str] | None = None
    cursor_string: str | None = None
