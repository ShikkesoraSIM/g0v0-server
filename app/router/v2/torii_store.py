"""/api/v2/torii/store — cosmetic store curation.

Surfaces
--------
``GET /torii/store/config``         the store pool config every client reads
``PUT /torii/store/admin/config``   (admin) replace the disabled-id list

The ``disabled`` list is the set of catalog ids (trail / name-colour / aura ids)
an admin has pulled OUT of the store pool, so they aren't offered for sale.
Empty = everything sellable. Admin status is the same server-side ``is_admin``
gate the points-admin endpoints use, so it can't be spoofed from a client.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import HTTPException, Security
from pydantic import BaseModel
from sqlmodel import select

from app.database import User
from app.database.torii_store import ToriiOwnedCosmetic, ToriiStoreConfig, record_owned_cosmetics
from app.dependencies.database import Database
from app.dependencies.user import get_current_user
from app.log import log
from app.models.torii_points import PointReason
from app.service.points_service import get_balance, spend
from app.utils import utcnow

from .router import router

logger = log("Store")

_DISABLED_KEY = "disabled"


def _require_admin(user: User) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")


async def _read_disabled(db) -> list[str]:
    row = (
        await db.exec(select(ToriiStoreConfig).where(ToriiStoreConfig.config_key == _DISABLED_KEY))
    ).first()
    if row is None or not row.value:
        return []
    try:
        data = json.loads(row.value)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


class StoreConfigResp(BaseModel):
    disabled: list[str]


@router.get(
    "/torii/store/config",
    tags=["Torii"],
    name="Get cosmetic store config",
    description="The cosmetic store pool config (currently the disabled-id list). Read by every client to filter the store.",
)
async def get_store_config(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> StoreConfigResp:
    return StoreConfigResp(disabled=await _read_disabled(db))


class SetStoreConfigRequest(BaseModel):
    disabled: list[str]


@router.put(
    "/torii/store/admin/config",
    tags=["Torii"],
    name="Set cosmetic store config (admin)",
    description="Replace the disabled-id list (catalog ids pulled from the store pool). Admin only.",
)
async def set_store_config(
    body: SetStoreConfigRequest,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> StoreConfigResp:
    _require_admin(current_user)

    # De-dupe + drop blanks, sorted for a stable stored value.
    cleaned = sorted({str(x).strip() for x in body.disabled if str(x).strip()})

    row = (
        await db.exec(select(ToriiStoreConfig).where(ToriiStoreConfig.config_key == _DISABLED_KEY))
    ).first()
    if row is None:
        row = ToriiStoreConfig(
            config_key=_DISABLED_KEY,
            value=json.dumps(cleaned),
            updated_by=current_user.id,
            updated_at=utcnow(),
        )
        db.add(row)
    else:
        row.value = json.dumps(cleaned)
        row.updated_by = current_user.id
        row.updated_at = utcnow()

    await db.commit()

    logger.info(
        "Admin {admin_id} set store disabled list ({n} items)",
        admin_id=current_user.id,
        n=len(cleaned),
    )
    return StoreConfigResp(disabled=cleaned)


@router.get(
    "/torii/store/owned",
    tags=["Torii"],
    name="Get my owned cosmetics",
    description="Catalog ids the authenticated user owns server-side (bought or granted). The client mirrors these into its local owned set.",
)
async def get_owned(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict:
    rows = (
        await db.exec(select(ToriiOwnedCosmetic.cosmetic_id).where(ToriiOwnedCosmetic.user_id == current_user.id))
    ).all()
    return {"owned": sorted({str(r) for r in rows})}


class PurchaseRequest(BaseModel):
    cosmetic_id: str
    price: int = 0


@router.post(
    "/torii/store/purchase",
    tags=["Torii"],
    name="Buy a cosmetic with points",
    description="Spend points (server-authoritative balance) to own a cosmetic. Idempotent: re-buying something you already own is a no-op success and never double-charges.",
)
async def purchase(
    body: PurchaseRequest,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict:
    cid = body.cosmetic_id.strip()
    if not cid:
        raise HTTPException(status_code=400, detail="Missing cosmetic")
    # NOTE: the price still comes from the client (the catalog lives client-side).
    # It's bounded here; moving the price table server-side is the proper hardening.
    if body.price < 0 or body.price > 100_000:
        raise HTTPException(status_code=400, detail="Invalid price")
    if cid in await _read_disabled(db):
        raise HTTPException(status_code=400, detail="That cosmetic isn't available")

    already = (
        await db.exec(
            select(ToriiOwnedCosmetic.id).where(
                ToriiOwnedCosmetic.user_id == current_user.id,
                ToriiOwnedCosmetic.cosmetic_id == cid,
            )
        )
    ).first()
    if already is not None:
        return {"owned": True, "already_owned": True, "balance": await get_balance(db, current_user.id)}

    if body.price > 0 and not await spend(
        db, current_user.id, body.price, PointReason.STORE_PURCHASE, ref=f"cosmetic:{cid}"
    ):
        raise HTTPException(status_code=400, detail="Not enough points")

    await record_owned_cosmetics(db, current_user.id, [cid], "store")
    await db.commit()

    logger.info(
        "User {uid} bought cosmetic {cid} for {price} points",
        uid=current_user.id,
        cid=cid,
        price=body.price,
    )
    return {"owned": True, "balance": await get_balance(db, current_user.id)}
