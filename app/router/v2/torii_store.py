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
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.database import User
from app.database.torii_store import ToriiOwnedCosmetic, ToriiStoreConfig, record_owned_cosmetics
from app.dependencies.database import Database
from app.dependencies.user import get_current_user
from app.log import log
from app.models.torii_cosmetic_prices import price_for
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

    # Snapshot before commit: commit expires every ORM row (expire_on_commit is on),
    # and reading an expired attribute in async SQLAlchemy does implicit IO with no
    # greenlet -> MissingGreenlet (500). current_user.id is the only ORM read left.
    admin_id = current_user.id

    await db.commit()

    logger.info(
        "Admin {admin_id} set store disabled list ({n} items)",
        admin_id=admin_id,
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
    # current_user is bound to this request's session, so any db.commit()/db.rollback()
    # below expires it; reading current_user.id afterwards would do implicit IO with no
    # greenlet -> MissingGreenlet (500). Capture the id as a plain int up front and use
    # that everywhere, so the whole handler is immune to the expire-on-commit trap.
    uid = current_user.id
    buyer_name = current_user.username

    cid = body.cosmetic_id.strip()
    if not cid:
        raise HTTPException(status_code=400, detail="Missing cosmetic")
    if cid in await _read_disabled(db):
        raise HTTPException(status_code=400, detail="That cosmetic isn't available")

    # Authoritative price: the SERVER owns the price; the client's claim is ignored.
    # An id with no server price is not for sale.
    price = price_for(cid)
    if price is None:
        raise HTTPException(status_code=400, detail="That cosmetic isn't for sale")
    if body.price != price:
        logger.warning(
            "Purchase price mismatch for user {uid} on {cid}: client said {cp}, server charges {sp}",
            uid=uid,
            cid=cid,
            cp=body.price,
            sp=price,
        )

    # Lock the user row so this user's purchases serialise, then re-check ownership
    # UNDER the lock. Stops a concurrent double-buy (double charge) and the unique
    # (user_id, cosmetic_id) collision that would otherwise surface as a 500.
    locked = (await db.exec(select(User).where(User.id == uid).with_for_update())).first()
    if locked is None:
        raise HTTPException(status_code=404, detail="Unknown user")

    already = (
        await db.exec(
            select(ToriiOwnedCosmetic.id).where(
                ToriiOwnedCosmetic.user_id == uid,
                ToriiOwnedCosmetic.cosmetic_id == cid,
            )
        )
    ).first()
    if already is not None:
        return {"owned": True, "already_owned": True, "balance": await get_balance(db, uid)}

    if price > 0 and not await spend(
        db, uid, price, PointReason.STORE_PURCHASE, ref=f"cosmetic:{cid}"
    ):
        raise HTTPException(status_code=400, detail="Not enough points")

    # torii: un aura es un cosmetico que se desbloquea y listo. Comprarlo solo registra la posesion
    # (torii_owned_cosmetics); el entitlement (available/allowed) lee esas filas ademas de los grupos,
    # asi que NO tocamos torii_titles. Antes la compra appendeaba el owning_group del aura como
    # titulo — lo cual, para un aura como admin-embers, le habria dado el GRUPO admin (badge/rol) al
    # comprador. Ahora la posesion y el rol son cosas separadas: el rol otorga el aura como bonus,
    # pero poseerla no otorga el rol.
    await record_owned_cosmetics(db, uid, [cid], "store")

    try:
        await db.commit()
    except IntegrityError:
        # Lost a race to own this exact cosmetic; treat as already owned (the
        # rolled-back transaction means no points were charged).
        await db.rollback()
        return {"owned": True, "already_owned": True, "balance": await get_balance(db, uid)}

    logger.info(
        "User {uid} bought cosmetic {cid} for {price} points",
        uid=uid,
        cid=cid,
        price=price,
    )

    # evento al #feed de discord (best-effort, no bloquea la compra)
    try:
        from app.service.discord_feed import notify_cosmetic_purchase

        notify_cosmetic_purchase(username=buyer_name, user_id=uid, cosmetic_id=cid, price=price)
    except Exception:
        pass
    return {"owned": True, "balance": await get_balance(db, uid)}
