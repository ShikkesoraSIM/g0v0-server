import hashlib
import zipfile
import re
from io import BytesIO
from typing import TYPE_CHECKING
from datetime import datetime

from app.models.beatmapset_upload import BeatmapSetFile
from app.database import Beatmapset, Beatmap, User
from app.models.beatmap import BeatmapRankStatus
from app.models.score import GameMode
from app.calculator import get_calculator

from osupyparser import OsuFile
from sqlmodel import col, select
from PIL import Image

if TYPE_CHECKING:
    from app.storage.base import StorageService
    from sqlmodel.ext.asyncio.session import AsyncSession

class BeatmapsetUploadService:
    @staticmethod
    def _extract_background_filename(osu_content: str) -> str | None:
        # 0,0,"bg.jpg",0,0
        match = re.search(r'0,0,"([^"]+)"', osu_content)
        if match:
            return match.group(1)
        # 0,0,bg.jpg,0,0
        match = re.search(r'0,0,([^,]+)', osu_content)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    async def _generate_covers(storage: "StorageService", beatmapset_id: int, bg_data: bytes) -> dict[str, str]:
        img = Image.open(BytesIO(bg_data))
        if img.mode != "RGB":
            img = img.convert("RGB")

        sizes = {
            "cover": (1080, 260),
            "card": (400, 140),
            "list": (150, 60),
            "slimcover": (1920, 360),
        }

        covers = {}
        for name, size in sizes.items():
            # Resize with cropping (center)
            target_aspect = size[0] / size[1]
            img_aspect = img.width / img.height

            if img_aspect > target_aspect:
                new_width = int(img.height * target_aspect)
                left = (img.width - new_width) // 2
                cropped = img.crop((left, 0, left + new_width, img.height))
            else:
                new_height = int(img.width / target_aspect)
                top = (img.height - new_height) // 2
                cropped = img.crop((0, top, img.width, top + new_height))

            resized = cropped.resize(size, Image.Resampling.LANCZOS)

            out = BytesIO()
            resized.save(out, format="JPEG", quality=90)
            storage_path = f"beatmapsets/{beatmapset_id}/covers/{name}.jpg"
            await storage.write_file(storage_path, out.getvalue(), "image/jpeg")
            covers[name] = await storage.get_file_url(storage_path)

            # @2x
            size2x = (size[0] * 2, size[1] * 2)
            resized2x = cropped.resize(size2x, Image.Resampling.LANCZOS)
            out2x = BytesIO()
            resized2x.save(out2x, format="JPEG", quality=85)
            storage_path2x = f"beatmapsets/{beatmapset_id}/covers/{name}@2x.jpg"
            await storage.write_file(storage_path2x, out2x.getvalue(), "image/jpeg")
            covers[f"{name}@2x"] = await storage.get_file_url(storage_path2x)

        return covers

    @staticmethod
    async def get_beatmapset_files(storage: "StorageService", beatmapset_id: int) -> list[BeatmapSetFile]:
        file_path = f"beatmapsets/{beatmapset_id}.osz"
        if not await storage.is_exists(file_path):
            return []

        content = await storage.read_file(file_path)
        files = []
        with zipfile.ZipFile(BytesIO(content)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                with z.open(info) as f:
                    sha2_hash = hashlib.sha256(f.read()).hexdigest()
                    files.append(BeatmapSetFile(filename=info.filename, sha2_hash=sha2_hash))
        return files

    @staticmethod
    async def allocate_beatmaps(db: "AsyncSession", beatmapset_id: int, user_id: int, count: int) -> list[int]:
        beatmap_ids = []
        beatmapset = await db.get(Beatmapset, beatmapset_id)
        status = beatmapset.beatmap_status if beatmapset is not None else BeatmapRankStatus.WIP
        for _ in range(count):
            beatmap = Beatmap(
                beatmapset_id=beatmapset_id,
                user_id=user_id,
                mode=GameMode.OSU, # Default
                total_length=0,
                version="New Beatmap",
                beatmap_status=status,
                last_updated=datetime.utcnow(),
                is_local=True,
                # Initialize other required fields to avoid database errors
                difficulty_rating=0.0,
                ar=0.0,
                cs=0.0,
                drain=0.0,
                accuracy=0.0,
                bpm=0.0,
                count_circles=0,
                count_sliders=0,
                count_spinners=0,
                hit_length=0,
                checksum="",
            )
            db.add(beatmap)
            await db.flush() # Get the ID without committing
            beatmap_ids.append(beatmap.id)
        return beatmap_ids

    @staticmethod
    async def patch_beatmapset_package(
        storage: "StorageService",
        beatmapset_id: int,
        files_changed: list[tuple[str, bytes]],
        files_deleted: list[str],
    ) -> bytes:
        file_path = f"beatmapsets/{beatmapset_id}.osz"
        existing_content = b""
        if await storage.is_exists(file_path):
            existing_content = await storage.read_file(file_path)

        out_io = BytesIO()
        with zipfile.ZipFile(out_io, "w", zipfile.ZIP_DEFLATED) as out_z:
            if existing_content:
                with zipfile.ZipFile(BytesIO(existing_content)) as in_z:
                    for info in in_z.infolist():
                        if info.filename in files_deleted:
                            continue
                        if any(f[0] == info.filename for f in files_changed):
                            continue
                        out_z.writestr(info, in_z.read(info))

            for filename, content in files_changed:
                out_z.writestr(filename, content)

        new_content = out_io.getvalue()
        await storage.write_file(file_path, new_content)
        return new_content

    @staticmethod
    async def process_beatmapset_package(
        db: "AsyncSession",
        storage: "StorageService",
        beatmapset_id: int,
    ) -> list[int]:
        file_path = f"beatmapsets/{beatmapset_id}.osz"
        if not await storage.is_exists(file_path):
            return []

        content = await storage.read_file(file_path)
        beatmapset = await db.get(Beatmapset, beatmapset_id)
        if not beatmapset:
            return []
        owner = await db.get(User, beatmapset.user_id)
        if owner is not None:
            beatmapset.creator = owner.username

        updated_beatmap_ids = []
        files_to_update = {}
        with zipfile.ZipFile(BytesIO(content)) as z:
            osu_files = [f for f in z.namelist() if f.endswith(".osu")]
            if not osu_files:
                return []
            processed_osu_files: set[str] = set()

            bg_filename = None

            # Update beatmapset metadata from the first valid .osu file
            for osu_file in osu_files:
                with z.open(osu_file) as f:
                    original_osu_bytes = f.read()
                    osu_content = original_osu_bytes.decode("utf-8", errors="ignore")
                    final_osu_bytes = original_osu_bytes

                    # Manual extraction fallback for Artist/Title if osupyparser fails
                    extracted_artist = None
                    extracted_title = None
                    extracted_version = None
                    extracted_source = None
                    extracted_tags = None
                    extracted_bid = None
                    extracted_mode = 0
                    extracted_ar = 0.0
                    extracted_cs = 0.0
                    extracted_drain = 0.0
                    extracted_accuracy = 0.0
                    extracted_bpm = 0.0

                    section = None
                    for line in osu_content.splitlines():
                        line = line.strip()
                        if not line: continue
                        if line.startswith("[") and line.endswith("]"):
                            section = line[1:-1]
                            continue

                        if section == "Metadata":
                            if ":" in line:
                                key, value = line.split(":", 1)
                                key, value = key.strip(), value.strip()
                                if key == "Artist": extracted_artist = value
                                elif key == "Title": extracted_title = value
                                elif key == "Version": extracted_version = value
                                elif key == "Source": extracted_source = value
                                elif key == "Tags": extracted_tags = value
                                elif key == "BeatmapID":
                                    try: extracted_bid = int(value)
                                    except: pass
                        elif section == "General":
                            if ":" in line:
                                key, value = line.split(":", 1)
                                key, value = key.strip(), value.strip()
                                if key == "Mode":
                                    try: extracted_mode = int(value)
                                    except: pass
                        elif section == "Difficulty":
                            if ":" in line:
                                key, value = line.split(":", 1)
                                key, value = key.strip(), value.strip()
                                try:
                                    if key == "ApproachRate": extracted_ar = float(value)
                                    elif key == "CircleSize": extracted_cs = float(value)
                                    elif key == "HPDrainRate": extracted_drain = float(value)
                                    elif key == "OverallDifficulty": extracted_accuracy = float(value)
                                except: pass
                        elif section == "TimingPoints":
                            if "," in line and extracted_bpm == 0:
                                parts = line.split(",")
                                if len(parts) >= 2:
                                    try:
                                        ms_per_beat = float(parts[1])
                                        if ms_per_beat > 0:
                                            extracted_bpm = 60000 / ms_per_beat
                                    except: pass

                    try:
                        parsed = OsuFile(content=osu_content.encode("utf-8")).parse_file()
                    except Exception:
                        # If parser fails but we have manual extraction, continue with manual
                        if not extracted_artist or not extracted_title:
                            continue
                        parsed = None

                    # Always update metadata from .osu files during processing
                    if parsed:
                        beatmapset.artist = parsed.metadata.artist
                        beatmapset.artist_unicode = getattr(parsed.metadata, "artist_unicode", parsed.metadata.artist)
                        beatmapset.title = parsed.metadata.title
                        beatmapset.title_unicode = getattr(parsed.metadata, "title_unicode", parsed.metadata.title)
                        beatmapset.source = parsed.metadata.source
                        beatmapset.tags = parsed.metadata.tags

                        bid = parsed.metadata.beatmap_id
                        version = parsed.metadata.version
                        mode_val = parsed.mode
                    else:
                        beatmapset.artist = extracted_artist or beatmapset.artist
                        beatmapset.artist_unicode = extracted_artist or beatmapset.artist_unicode
                        beatmapset.title = extracted_title or beatmapset.title
                        beatmapset.title_unicode = extracted_title or beatmapset.title_unicode
                        beatmapset.source = extracted_source or beatmapset.source
                        beatmapset.tags = extracted_tags or beatmapset.tags

                        bid = extracted_bid
                        version = extracted_version or "New Beatmap"
                        mode_val = extracted_mode

                    # Ensure unicode versions are set if missing
                    if not beatmapset.artist_unicode: beatmapset.artist_unicode = beatmapset.artist
                    if not beatmapset.title_unicode: beatmapset.title_unicode = beatmapset.title

                    beatmapset.last_updated = datetime.utcnow()
                    beatmapset.is_local = True

                    # Find background from any file if not found yet
                    if not bg_filename:
                        bg_filename = BeatmapsetUploadService._extract_background_filename(osu_content)

                    # Try to find existing beatmap
                    beatmap = None
                    if bid:
                        beatmap = await db.get(Beatmap, bid)
                        # Ensure this beatmap belongs to this set
                        if beatmap and beatmap.beatmapset_id != beatmapset_id:
                            beatmap = None

                    if not beatmap:
                        # Find by version within this beatmapset
                        stmt = select(Beatmap).where(
                            Beatmap.beatmapset_id == beatmapset_id,
                            Beatmap.version == version
                        )
                        beatmap = (await db.exec(stmt)).first()

                    if not beatmap:
                        # Try to find a placeholder beatmap (one with "New Beatmap" version)
                        stmt = select(Beatmap).where(
                            Beatmap.beatmapset_id == beatmapset_id,
                            Beatmap.version == "New Beatmap"
                        )
                        beatmap = (await db.exec(stmt)).first()

                    if not beatmap:
                        # Create a new beatmap if no placeholders are available
                        beatmap = Beatmap(
                            beatmapset_id=beatmapset_id,
                            user_id=beatmapset.user_id,
                            version=version,
                            mode=GameMode.OSU,
                            total_length=0,
                            beatmap_status=beatmapset.beatmap_status,
                            last_updated=datetime.utcnow(),
                            is_local=True,
                            # Initialize other required fields to avoid database errors
                            difficulty_rating=0.0,
                            ar=0.0,
                            cs=0.0,
                            drain=0.0,
                            accuracy=0.0,
                            bpm=0.0,
                            count_circles=0,
                            count_sliders=0,
                            count_spinners=0,
                            hit_length=0,
                            checksum="",
                        )
                        db.add(beatmap)
                        await db.flush()

                    if beatmap:
                        # Ensure the .osu file has the correct BeatmapID
                        file_modified = False
                        if bid != beatmap.id:
                            # Update the .osu content with the correct ID
                            if "[Metadata]" in osu_content:
                                if "BeatmapID:" in osu_content:
                                    osu_content = re.sub(r"BeatmapID:\s*\d*", f"BeatmapID:{beatmap.id}", osu_content)
                                else:
                                    osu_content = osu_content.replace("[Metadata]", f"[Metadata]\nBeatmapID:{beatmap.id}")

                            # Update BeatmapSetID as well
                            if "BeatmapSetID:" in osu_content:
                                osu_content = re.sub(r"BeatmapSetID:\s*\d*", f"BeatmapSetID:{beatmapset_id}", osu_content)
                            else:
                                osu_content = osu_content.replace("[Metadata]", f"[Metadata]\nBeatmapSetID:{beatmapset_id}")
                            file_modified = True

                        beatmap.version = version
                        beatmap.beatmap_status = beatmapset.beatmap_status
                        try:
                            beatmap.mode = GameMode.from_int(mode_val)
                        except (ValueError, KeyError):
                            beatmap.mode = GameMode.OSU

                        # Update other fields if parsed successfully
                        if parsed:
                            # Hit object counts
                            circles = 0
                            sliders = 0
                            spinners = 0
                            for obj in parsed.hit_objects:
                                obj_type = getattr(obj, "type", 0)
                                if obj_type & 1: circles += 1
                                elif obj_type & 2: sliders += 1
                                elif obj_type & 8: spinners += 1
                                else: circles += 1

                            beatmap.count_circles = circles
                            beatmap.count_sliders = sliders
                            beatmap.count_spinners = spinners

                            # Calculate lengths
                            if parsed.hit_objects:
                                first_obj = parsed.hit_objects[0]
                                last_obj = parsed.hit_objects[-1]
                                beatmap.total_length = int(last_obj.start_time / 1000)
                                beatmap.hit_length = int((last_obj.start_time - first_obj.start_time) / 1000)

                            beatmap.ar = parsed.difficulty.approach_rate
                            beatmap.cs = parsed.difficulty.circle_size
                            beatmap.drain = parsed.difficulty.hp_drain_rate
                            beatmap.accuracy = parsed.difficulty.overall_difficulty

                            if parsed.timing_points:
                                uninherited = [p for p in parsed.timing_points if not p.inherited]
                                if uninherited:
                                    beatmap.bpm = 60000 / uninherited[0].ms_per_beat
                        else:
                            # Fallback to manual extraction
                            beatmap.ar = extracted_ar
                            beatmap.cs = extracted_cs
                            beatmap.drain = extracted_drain
                            beatmap.accuracy = extracted_accuracy
                            beatmap.bpm = extracted_bpm

                            # Estimate hit object counts from [HitObjects] section if possible
                            if "[HitObjects]" in osu_content:
                                hit_objects_content = osu_content.split("[HitObjects]")[1].strip()
                                lines = [l for l in hit_objects_content.splitlines() if l.strip()]
                                circles = 0
                                sliders = 0
                                spinners = 0
                                last_time = 0
                                first_time = None

                                for line in lines:
                                    parts = line.split(",")
                                    if len(parts) >= 4:
                                        try:
                                            t = int(parts[2])
                                            if first_time is None: first_time = t
                                            last_time = t
                                            obj_type = int(parts[3])
                                            if obj_type & 1: circles += 1
                                            elif obj_type & 2: sliders += 1
                                            elif obj_type & 8: spinners += 1
                                            else: circles += 1
                                        except: circles += 1

                                beatmap.count_circles = circles
                                beatmap.count_sliders = sliders
                                beatmap.count_spinners = spinners
                                if first_time is not None:
                                    beatmap.total_length = int(last_time / 1000)
                                    beatmap.hit_length = int((last_time - first_time) / 1000)

                        # Calculate difficulty attributes (Star Rating and Max Combo) using performance calculator
                        try:
                            calculator = get_calculator()
                            diff_attrs = await calculator.calculate_difficulty(osu_content)
                            beatmap.difficulty_rating = diff_attrs.star_rating
                            beatmap.max_combo = diff_attrs.max_combo
                        except Exception:
                            # Set difficulty_rating from beatmapset.difficulty_rating if it's 0
                            if beatmap.difficulty_rating == 0 and hasattr(beatmapset, "difficulty_rating"):
                                beatmap.difficulty_rating = beatmapset.difficulty_rating

                        if file_modified:
                            final_osu_bytes = osu_content.encode("utf-8")
                        beatmap.checksum = hashlib.md5(final_osu_bytes).hexdigest()
                        beatmap.last_updated = datetime.utcnow()
                        beatmap.is_local = True
                        updated_beatmap_ids.append(beatmap.id)
                        processed_osu_files.add(osu_file)
                        db.add(beatmap) # Explicitly ensure beatmap is in session

                        # Store updated .osu content back to the zip if modified
                        if file_modified:
                            files_to_update[osu_file] = final_osu_bytes

            files_to_delete = [name for name in osu_files if name not in processed_osu_files]

            # If .osu files were modified or stale .osu files were found, re-pack the OSZ
            if files_to_update or files_to_delete:
                await BeatmapsetUploadService.patch_beatmapset_package(
                    storage,
                    beatmapset_id,
                    [(name, content) for name, content in files_to_update.items()],
                    files_to_delete
                )

            # Remove stale placeholders that were allocated but never matched to any uploaded .osu file.
            # These rows can confuse lazer lookup/link generation (e.g. #osu without difficulty id).
            if updated_beatmap_ids:
                stale_stmt = select(Beatmap).where(
                    Beatmap.beatmapset_id == beatmapset_id,
                    ~col(Beatmap.id).in_(updated_beatmap_ids),
                    col(Beatmap.is_local).is_(True),
                    col(Beatmap.version) == "New Beatmap",
                    (col(Beatmap.checksum) == "") | col(Beatmap.checksum).is_(None),
                )
                stale_beatmaps = (await db.exec(stale_stmt)).all()
                for stale in stale_beatmaps:
                    await db.delete(stale)

            # Update beatmapset BPM (max of beatmaps)
            stmt = select(Beatmap.bpm).where(Beatmap.beatmapset_id == beatmapset_id)
            bpms = (await db.exec(stmt)).all()
            if bpms:
                beatmapset.bpm = max(bpms)

            # Process background and covers.
            # Fallback: if [Events] did not provide a valid background, use first image in package.
            cover_source = bg_filename if bg_filename in z.namelist() else None
            if cover_source is None:
                image_exts = (".jpg", ".jpeg", ".png", ".webp")
                for name in z.namelist():
                    lower = name.lower()
                    if lower.endswith(image_exts):
                        cover_source = name
                        break

            if cover_source is not None:
                with z.open(cover_source) as f:
                    bg_data = f.read()
                    try:
                        covers = await BeatmapsetUploadService._generate_covers(storage, beatmapset_id, bg_data)
                        beatmapset.covers = covers
                    except Exception:
                        pass

            await db.commit()
            return updated_beatmap_ids
