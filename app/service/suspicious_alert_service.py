from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.database.beatmap import Beatmap
from app.database.suspicious_alert import SuspiciousAlert
from app.database.user import User
from app.database.user_login_log import UserLoginLog
from app.database.verification import LoginSession, TrustedDevice
from app.database.statistics import UserStatistics
from app.log import log
from app.models.mods import APIMod
from app.utils import utcnow

from redis.asyncio import Redis
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from app.database.score import Score


logger = log("SuspiciousAlert")

GENERIC_CLIENT_LABELS = {"", "unknown", "osu!", "osu!lazer", "lazer", "osulazer"}


@dataclass(slots=True)
class AlertResult:
    created: bool
    alert: SuspiciousAlert | None = None


class SuspiciousAlertService:
    @staticmethod
    def _fingerprint(*parts: object) -> str:
        payload = "|".join(str(part or "") for part in parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]

    @staticmethod
    async def _create_alert(
        session: AsyncSession,
        *,
        kind: str,
        severity: str,
        fingerprint: str,
        title: str,
        body: str,
        payload: dict[str, Any],
        user_id: int | None = None,
        score_id: int | None = None,
        beatmap_id: int | None = None,
    ) -> AlertResult:
        existing = (
            await session.exec(
                select(SuspiciousAlert).where(SuspiciousAlert.fingerprint == fingerprint)
            )
        ).first()
        if existing is not None:
            return AlertResult(created=False, alert=existing)

        alert = SuspiciousAlert(
            kind=kind,
            severity=severity,
            fingerprint=fingerprint,
            user_id=user_id,
            score_id=score_id,
            beatmap_id=beatmap_id,
            title=title[:200],
            body=body[:4000],
            payload=payload,
        )
        session.add(alert)
        return AlertResult(created=True, alert=alert)

    @staticmethod
    async def _distinct_other_users_by_ip(
        session: AsyncSession,
        ip_address: str,
        *,
        exclude_user_id: int | None = None,
        limit: int = 10,
    ) -> list[int]:
        seen: set[int] = set()

        def _consume(values: Iterable[int | None]) -> None:
            for value in values:
                if value is None:
                    continue
                if exclude_user_id is not None and value == exclude_user_id:
                    continue
                seen.add(int(value))

        login_rows = (
            await session.exec(
                select(UserLoginLog.user_id)
                .where(
                    UserLoginLog.ip_address == ip_address,
                    UserLoginLog.login_success.is_(True),
                    UserLoginLog.user_id > 0,
                )
                .limit(limit * 3)
            )
        ).all()
        _consume(login_rows)

        session_rows = (
            await session.exec(
                select(LoginSession.user_id).where(LoginSession.ip_address == ip_address).limit(limit * 3)
            )
        ).all()
        _consume(session_rows)

        device_rows = (
            await session.exec(
                select(TrustedDevice.user_id).where(TrustedDevice.ip_address == ip_address).limit(limit * 3)
            )
        ).all()
        _consume(device_rows)

        return sorted(seen)[:limit]

    @staticmethod
    async def _distinct_other_users_by_web_uuid(
        session: AsyncSession,
        web_uuid: str,
        *,
        exclude_user_id: int | None = None,
        limit: int = 10,
    ) -> list[int]:
        seen: set[int] = set()

        def _consume(values: Iterable[int | None]) -> None:
            for value in values:
                if value is None:
                    continue
                if exclude_user_id is not None and value == exclude_user_id:
                    continue
                seen.add(int(value))

        session_rows = (
            await session.exec(
                select(LoginSession.user_id).where(LoginSession.web_uuid == web_uuid).limit(limit * 3)
            )
        ).all()
        _consume(session_rows)

        device_rows = (
            await session.exec(
                select(TrustedDevice.user_id).where(TrustedDevice.web_uuid == web_uuid).limit(limit * 3)
            )
        ).all()
        _consume(device_rows)

        return sorted(seen)[:limit]

    @staticmethod
    async def _usernames_for_ids(session: AsyncSession, user_ids: list[int]) -> list[str]:
        if not user_ids:
            return []
        rows = (
            await session.exec(
                select(User.id, User.username).where(col(User.id).in_(user_ids))
            )
        ).all()
        return [f"{username}#{user_id}" for user_id, username in rows]

    @staticmethod
    def _format_mods(mods: list[APIMod] | list[dict[str, Any]] | list[str]) -> str:
        if not mods:
            return "NM"
        if isinstance(mods[0], str):
            result = [str(mod) for mod in mods]
        else:
            result = []
            for mod in mods:
                if isinstance(mod, dict):
                    acronym = mod.get("acronym")
                    if acronym:
                        result.append(str(acronym))
        return "".join(result) or "NM"

    @classmethod
    async def maybe_record_registration_alert(
        cls,
        session: AsyncSession,
        *,
        user: User,
        ip_address: str,
        user_agent: str | None,
        web_uuid: str | None,
    ) -> AlertResult:
        if not settings.enable_suspicious_mod_alerts:
            return AlertResult(created=False)

        ip_address = str(ip_address)
        shared_ip_users = await cls._distinct_other_users_by_ip(session, ip_address, exclude_user_id=user.id)
        shared_uuid_users: list[int] = []
        if web_uuid:
            shared_uuid_users = await cls._distinct_other_users_by_web_uuid(session, web_uuid, exclude_user_id=user.id)

        reasons: list[str] = []
        severity = "warning"
        if len(shared_ip_users) >= settings.suspicious_shared_ip_user_threshold:
            reasons.append(f"IP already seen on {len(shared_ip_users)} other account(s)")
            severity = "critical" if len(shared_ip_users) >= settings.suspicious_shared_ip_critical_threshold else severity
        if shared_uuid_users:
            reasons.append(f"web UUID already seen on {len(shared_uuid_users)} other account(s)")
            severity = "critical"

        if not reasons:
            return AlertResult(created=False)

        related_ids = sorted(set(shared_ip_users + shared_uuid_users))
        related_users = await cls._usernames_for_ids(session, related_ids)
        fingerprint = cls._fingerprint(
            "registration",
            user.id,
            ip_address,
            web_uuid or "",
            ",".join(str(i) for i in related_ids),
        )
        return await cls._create_alert(
            session,
            kind="suspicious_account_created",
            severity=severity,
            fingerprint=fingerprint,
            title=f"Suspicious account created: {user.username}",
            body=(
                f"New account {user.username} (#{user.id}) was created from suspicious signals: "
                + "; ".join(reasons)
            ),
            user_id=user.id,
            payload={
                "username": user.username,
                "user_id": user.id,
                "country_code": user.country_code,
                "ip_address": ip_address,
                "web_uuid": web_uuid,
                "user_agent": (user_agent or "")[:250],
                "reasons": reasons,
                "related_user_ids": related_ids,
                "related_users": related_users,
            },
        )

    @classmethod
    async def maybe_record_login_alert(
        cls,
        session: AsyncSession,
        *,
        user: User,
        ip_address: str,
        user_agent: str | None,
        web_uuid: str | None,
        trusted_device: bool,
        version_hash: str | None,
        client_label: str | None,
        is_new_device: bool,
    ) -> AlertResult:
        if not settings.enable_suspicious_mod_alerts:
            return AlertResult(created=False)

        ip_address = str(ip_address)
        reasons: list[str] = []
        severity = "warning"

        shared_ip_users = await cls._distinct_other_users_by_ip(session, ip_address, exclude_user_id=user.id)
        if len(shared_ip_users) >= settings.suspicious_shared_ip_user_threshold:
            reasons.append(f"login IP already seen on {len(shared_ip_users)} other account(s)")
            severity = "critical" if len(shared_ip_users) >= settings.suspicious_shared_ip_critical_threshold else severity

        shared_uuid_users: list[int] = []
        if web_uuid:
            shared_uuid_users = await cls._distinct_other_users_by_web_uuid(session, web_uuid, exclude_user_id=user.id)
            if shared_uuid_users:
                reasons.append(f"web UUID already seen on {len(shared_uuid_users)} other account(s)")
                severity = "critical"

        normalized_label = (client_label or "").strip().lower()
        if version_hash and normalized_label in GENERIC_CLIENT_LABELS:
            reasons.append("client hash is unknown or unresolved")
            severity = "critical"

        new_device_context = (not trusted_device and is_new_device)

        if not reasons:
            return AlertResult(created=False)

        if new_device_context:
            reasons.append("new device/location login")

        related_ids = sorted(set(shared_ip_users + shared_uuid_users))
        related_users = await cls._usernames_for_ids(session, related_ids)
        fingerprint = cls._fingerprint(
            "login",
            user.id,
            ip_address,
            web_uuid or "",
            version_hash or "",
            utcnow().strftime("%Y%m%d%H"),
        )
        return await cls._create_alert(
            session,
            kind="suspicious_login",
            severity=severity,
            fingerprint=fingerprint,
            title=f"Suspicious login: {user.username}",
            body=f"{user.username} (#{user.id}) logged in with suspicious signals: " + "; ".join(reasons),
            user_id=user.id,
            payload={
                "username": user.username,
                "user_id": user.id,
                "ip_address": ip_address,
                "web_uuid": web_uuid,
                "version_hash": version_hash,
                "client_label": client_label,
                "user_agent": (user_agent or "")[:250],
                "trusted_device": trusted_device,
                "is_new_device": is_new_device,
                "reasons": reasons,
                "related_user_ids": related_ids,
                "related_users": related_users,
            },
        )

    @classmethod
    async def maybe_record_suspicious_score_alert(
        cls,
        session: AsyncSession,
        redis: Redis,
        *,
        score: "Score",
        user: User,
    ) -> AlertResult:
        if not settings.enable_suspicious_mod_alerts:
            return AlertResult(created=False)
        if not score.passed:
            return AlertResult(created=False)

        pp_value = float(score.pp or 0.0)
        accuracy = float(score.accuracy or 0.0)
        reasons: list[str] = []
        severity = "warning"

        if pp_value >= settings.suspicious_alert_pp_threshold:
            reasons.append(f"very high pp ({pp_value:.2f}pp)")
            severity = "critical"

        if (
            pp_value >= settings.suspicious_alert_low_acc_pp_threshold
            and accuracy < settings.suspicious_alert_low_accuracy
        ):
            reasons.append(f"high pp with low accuracy ({accuracy * 100:.2f}%)")
            severity = "critical"

        account_age_days = max(0, (utcnow() - user.join_date).days) if user.join_date else 0
        user_stats = (
            await session.exec(
                select(UserStatistics).where(
                    UserStatistics.user_id == user.id,
                    UserStatistics.mode == score.gamemode,
                )
            )
        ).first()
        play_count = int(user_stats.play_count) if user_stats is not None else 0

        if (
            pp_value >= settings.suspicious_alert_new_account_pp_threshold
            and account_age_days <= settings.suspicious_alert_new_account_days
        ):
            reasons.append(
                f"high pp on a fresh account ({account_age_days} day(s) old)"
            )

        if (
            pp_value >= settings.suspicious_alert_low_playcount_pp_threshold
            and play_count <= settings.suspicious_alert_low_playcount_threshold
        ):
            reasons.append(f"high pp with low playcount ({play_count} plays in {score.gamemode})")

        if not reasons:
            return AlertResult(created=False)

        beatmap = await session.get(Beatmap, score.beatmap_id)
        beatmap_name = f"{beatmap.artist} - {beatmap.title} [{beatmap.version}]" if beatmap else f"beatmap #{score.beatmap_id}"
        last_client_hash = await redis.get(f"metadata:user:last_client_hash:{user.id}")
        if isinstance(last_client_hash, bytes):
            last_client_hash = last_client_hash.decode("utf-8", errors="ignore")
        mods = cls._format_mods(score.mods)
        fingerprint = cls._fingerprint("score", score.id, user.id, score.beatmap_id, f"{pp_value:.2f}")

        return await cls._create_alert(
            session,
            kind="suspicious_score",
            severity=severity,
            fingerprint=fingerprint,
            title=f"Suspicious play: {user.username} {pp_value:.2f}pp",
            body=(
                f"{user.username} submitted {pp_value:.2f}pp on {beatmap_name} "
                f"with {accuracy * 100:.2f}% accuracy (+{mods})."
            ),
            user_id=user.id,
            score_id=score.id,
            beatmap_id=score.beatmap_id,
            payload={
                "username": user.username,
                "user_id": user.id,
                "score_id": score.id,
                "beatmap_id": score.beatmap_id,
                "beatmap_name": beatmap_name,
                "mode": str(score.gamemode),
                "pp": round(pp_value, 2),
                "accuracy": round(accuracy * 100, 2),
                "mods": mods,
                "rank": str(score.rank),
                "combo": score.max_combo,
                "total_score": score.total_score,
                "account_age_days": account_age_days,
                "play_count": play_count,
                "last_client_hash": last_client_hash,
                "reasons": reasons,
            },
        )

    @staticmethod
    async def get_pending_alerts(session: AsyncSession, limit: int = 10) -> list[SuspiciousAlert]:
        return (
            await session.exec(
                select(SuspiciousAlert)
                .where(
                    SuspiciousAlert.dispatched_at.is_(None),
                    SuspiciousAlert.resolved_at.is_(None),
                )
                .order_by(col(SuspiciousAlert.created_at).asc())
                .limit(max(1, min(limit, 50)))
            )
        ).all()

    @staticmethod
    async def mark_alert_dispatched(session: AsyncSession, alert_id: int) -> bool:
        alert = await session.get(SuspiciousAlert, alert_id)
        if alert is None:
            return False
        if alert.dispatched_at is None:
            alert.dispatched_at = utcnow()
            session.add(alert)
            await session.commit()
        return True

