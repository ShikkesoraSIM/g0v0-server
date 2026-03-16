from datetime import timedelta
import re
from typing import Annotated, Literal

from app.auth import (
    authenticate_user,
    create_access_token,
    generate_refresh_token,
    get_password_hash,
    get_token_by_refresh_token,
    get_user_by_authorization_code,
    store_token,
    validate_password,
    validate_username,
)
from app.config import settings
from app.const import BANCHOBOT_ID, SUPPORT_TOTP_VERIFICATION_VER
from app.database import DailyChallengeStats, OAuthClient, User
from app.database.auth import TotpKeys
from app.database.statistics import UserStatistics
from app.dependencies.api_version import APIVersion
from app.dependencies.database import Database, Redis
from app.dependencies.client_verification import ClientVerificationService
from app.dependencies.geoip import GeoIPService, IPAddress
from app.dependencies.user_agent import UserAgentInfo
from app.log import log
from app.models.extended_auth import ExtendedTokenResponse
from app.models.oauth import (
    OAuthErrorResponse,
    RegistrationRequestErrors,
    TokenResponse,
    UserRegistrationErrors,
)
from app.models.score import GameMode
from app.service.login_log_service import LoginLogService
from app.service.password_reset_service import password_reset_service
from app.service.turnstile_service import turnstile_service
from app.service.verification_service import (
    EmailVerificationService,
    LoginSessionService,
)
from app.utils import utcnow

from fastapi import APIRouter, Form, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import exists, select

logger = log("Auth")
LAST_CLIENT_HASH_KEY = "metadata:user:last_client_hash:{user_id}"
CLIENT_BUILD_PATTERN = re.compile(r"\d{4}\.\d+\.\d+(?:-[\w.]+)?", re.IGNORECASE)
HEX_HASH_PATTERN = re.compile(r"^[0-9a-f]{8,128}$", re.IGNORECASE)
CLIENT_HASH_FORM_KEYS = (
    "version_hash",
    "versionHash",
    "client_hash",
    "clientHash",
    "client_version_hash",
    "clientVersionHash",
    "build_hash",
    "buildHash",
    "hash",
)
CLIENT_VERSION_FORM_KEYS = (
    "version",
    "client_version",
    "clientVersion",
    "build",
    "build_version",
    "buildVersion",
)
CLIENT_HASH_HEADER_KEYS = (
    "x-version-hash",
    "x-client-version-hash",
    "x-client-hash",
)
CLIENT_VERSION_HEADER_KEYS = (
    "x-client-version",
    "x-build-version",
)


def _format_client_label_from_validation(validation) -> str | None:
    if validation is None:
        return None
    name = (validation.client_name or "").strip()
    version = (validation.version or "").strip()
    os_name = (validation.os or "").strip()
    if not any((name, version, os_name)):
        return None
    base = " ".join(part for part in (name, version) if part).strip()
    if os_name:
        return f"{base} ({os_name})" if base else os_name
    return base or None


def _extract_client_label_from_user_agent(raw_user_agent: str) -> str | None:
    ua = (raw_user_agent or "").strip()
    if not ua:
        return None

    ua_lower = ua.lower()
    if "mozilla/" in ua_lower and all(marker not in ua_lower for marker in ("osu", "tachyon", "shigetiro")):
        return None

    if ua_lower in {"osu!", "osu", "osu!lazer", "lazer"}:
        return "osu!"

    return ua[:180]


def _derive_login_client_label(
    validation,
    raw_user_agent: str,
    fallback_display_name: str = "",
    version_hint: str = "",
) -> str | None:
    mapped_label = _format_client_label_from_validation(validation)
    ua_label = _extract_client_label_from_user_agent(raw_user_agent)
    clean_version_hint = (version_hint or "").strip()
    has_version_hint = bool(clean_version_hint and CLIENT_BUILD_PATTERN.search(clean_version_hint))

    # Prefer richer UA labels with explicit build numbers, because some custom
    # clients can share hashes across versions.
    if ua_label and CLIENT_BUILD_PATTERN.search(ua_label):
        return ua_label

    if has_version_hint:
        name = ""
        os_name = ""
        if validation is not None:
            name = (validation.client_name or "").strip()
            os_name = (validation.os or "").strip()
        if not name:
            name = (fallback_display_name or "").strip() or "osu!"
        base = f"{name} {clean_version_hint}".strip()
        return f"{base} ({os_name})" if os_name else base

    if mapped_label:
        return mapped_label
    if ua_label:
        return ua_label

    fallback = (fallback_display_name or "").strip()
    if fallback and fallback.lower() != "unknown":
        return fallback
    return None


async def _extract_client_hints_from_request(
    request: Request,
    explicit_version_hash: str | None,
) -> tuple[str | None, str | None, list[str]]:
    form_values: dict[str, str] = {}
    try:
        form_data = await request.form()
        for key, value in form_data.multi_items():
            if key not in form_values and value is not None:
                form_values[key] = str(value).strip()
    except Exception:
        form_values = {}

    observed_keys = sorted(
        {
            key
            for key in form_values
            if any(token in key.lower() for token in ("version", "hash", "client", "build"))
        }
    )[:20]

    hash_candidates: list[str] = []
    if explicit_version_hash:
        hash_candidates.append(explicit_version_hash)

    for key in CLIENT_HASH_FORM_KEYS:
        value = form_values.get(key)
        if value:
            hash_candidates.append(value)
    for key in CLIENT_HASH_HEADER_KEYS:
        value = request.headers.get(key)
        if value:
            hash_candidates.append(value)

    normalized_hash: str | None = None
    for candidate in hash_candidates:
        value = (candidate or "").strip().lower()
        if value and HEX_HASH_PATTERN.fullmatch(value):
            normalized_hash = value
            break

    version_candidates: list[str] = []
    for key in CLIENT_VERSION_FORM_KEYS:
        value = form_values.get(key)
        if value:
            version_candidates.append(value)
    for key in CLIENT_VERSION_HEADER_KEYS:
        value = request.headers.get(key)
        if value:
            version_candidates.append(value)

    version_hint: str | None = None
    for candidate in version_candidates:
        value = (candidate or "").strip()
        if value and CLIENT_BUILD_PATTERN.search(value):
            version_hint = value[:64]
            break

    return normalized_hash, version_hint, observed_keys


def create_oauth_error_response(error: str, description: str, hint: str, status_code: int = 400):
    """åˆ›å»ºæ ‡å‡†çš„ OAuth é”™è¯¯å“åº”"""
    error_data = OAuthErrorResponse(error=error, error_description=description, hint=hint, message=description)
    return JSONResponse(status_code=status_code, content=error_data.model_dump())


def validate_email(email: str) -> list[str]:
    """éªŒè¯é‚®ç®±"""
    errors = []

    if not email:
        errors.append("Email is required")
        return errors

    # åŸºæœ¬çš„é‚®ç®±æ ¼å¼éªŒè¯
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_pattern, email):
        errors.append("Please enter a valid email address")

    return errors


router = APIRouter(tags=["osu! OAuth è®¤è¯"])


@router.post(
    "/users",
    name="æ³¨å†Œç”¨æˆ·",
    description="ç”¨æˆ·æ³¨å†ŒæŽ¥å£",
)
async def register_user(
    db: Database,
    user_username: Annotated[str, Form(..., alias="user[username]", description="ç”¨æˆ·å")],
    user_email: Annotated[str, Form(..., alias="user[user_email]", description="ç”µå­é‚®ç®±")],
    user_password: Annotated[str, Form(..., alias="user[password]", description="å¯†ç ")],
    geoip: GeoIPService,
    client_ip: IPAddress,
    user_agent: UserAgentInfo,
    cf_turnstile_response: Annotated[
        str, Form(description="Cloudflare Turnstile å“åº” token")
    ] = "XXXX.DUMMY.TOKEN.XXXX",
):
    # Turnstile éªŒè¯ï¼ˆä»…å¯¹éž osu! å®¢æˆ·ç«¯ï¼‰
    if settings.enable_turnstile_verification and not user_agent.is_client:
        success, error_msg = await turnstile_service.verify_token(cf_turnstile_response, client_ip)
        logger.info(f"Turnstile verification result: {success}, error_msg: {error_msg}")
        if not success:
            errors = RegistrationRequestErrors(message=f"Verification failed: {error_msg}")
            return JSONResponse(status_code=400, content={"form_error": errors.model_dump()})

    username_errors = validate_username(user_username)
    email_errors = validate_email(user_email)
    password_errors = validate_password(user_password)

    result = await db.exec(select(exists()).where(User.username == user_username))
    existing_user = result.first()
    if existing_user:
        username_errors.append("Username is already taken")

    result = await db.exec(select(exists()).where(User.email == user_email))
    existing_email = result.first()
    if existing_email:
        email_errors.append("Email is already taken")

    if username_errors or email_errors or password_errors:
        errors = RegistrationRequestErrors(
            user=UserRegistrationErrors(
                username=username_errors,
                user_email=email_errors,
                password=password_errors,
            )
        )

        return JSONResponse(status_code=422, content={"form_error": errors.model_dump()})

    try:
        # èŽ·å–å®¢æˆ·ç«¯ IP å¹¶æŸ¥è¯¢åœ°ç†ä½ç½®
        country_code = None  # é»˜è®¤å›½å®¶ä»£ç 

        try:
            # æŸ¥è¯¢ IP åœ°ç†ä½ç½®
            geo_info = geoip.lookup(client_ip)
            if geo_info and (country_code := geo_info.get("country_iso")):
                logger.info(f"User {user_username} registering from {client_ip}, country: {country_code}")
            else:
                logger.warning(f"Could not determine country for IP {client_ip}")
        except Exception as e:
            logger.warning(f"GeoIP lookup failed for {client_ip}: {e}")
        if country_code is None:
            country_code = "CN"

        # åˆ›å»ºæ–°ç”¨æˆ·
        # ç¡®ä¿ AUTO_INCREMENT å€¼ä»Ž3å¼€å§‹ï¼ˆID=2æ˜¯BanchoBotï¼‰
        result = await db.execute(
            text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'lazer_users'"
            )
        )
        next_id = result.one()[0]
        if next_id <= 2:
            await db.execute(text("ALTER TABLE lazer_users AUTO_INCREMENT = 3"))
            await db.commit()

        new_user = User(
            username=user_username,
            email=user_email,
            pw_bcrypt=get_password_hash(user_password),
            priv=1,  # æ™®é€šç”¨æˆ·æƒé™
            country_code=country_code,
            join_date=utcnow(),
            last_visit=utcnow(),
            is_supporter=settings.enable_supporter_for_all_users,
            support_level=int(settings.enable_supporter_for_all_users),
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
        for i in [GameMode.OSU, GameMode.TAIKO, GameMode.FRUITS, GameMode.MANIA]:
            statistics = UserStatistics(mode=i, user_id=new_user.id)
            db.add(statistics)
        if settings.enable_rx:
            for mode in (GameMode.OSURX, GameMode.TAIKORX, GameMode.FRUITSRX):
                statistics_rx = UserStatistics(mode=mode, user_id=new_user.id)
                db.add(statistics_rx)
        if settings.enable_ap:
            statistics_ap = UserStatistics(mode=GameMode.OSUAP, user_id=new_user.id)
            db.add(statistics_ap)
        daily_challenge_user_stats = DailyChallengeStats(user_id=new_user.id)
        db.add(daily_challenge_user_stats)
        await db.commit()
    except Exception:
        await db.rollback()
        # æ‰“å°è¯¦ç»†é”™è¯¯ä¿¡æ¯ç”¨äºŽè°ƒè¯•
        logger.exception(f"Registration error for user {user_username}")

        # è¿”å›žé€šç”¨é”™è¯¯
        errors = RegistrationRequestErrors(message="An error occurred while creating your account. Please try again.")

        return JSONResponse(status_code=500, content={"form_error": errors.model_dump()})


@router.post(
    "/oauth/token",
    response_model=TokenResponse | ExtendedTokenResponse,
    name="èŽ·å–è®¿é—®ä»¤ç‰Œ",
    description="OAuth ä»¤ç‰Œç«¯ç‚¹ï¼Œæ”¯æŒå¯†ç ã€åˆ·æ–°ä»¤ç‰Œå’ŒæŽˆæƒç ä¸‰ç§æŽˆæƒæ–¹å¼ã€‚",
)
async def oauth_token(
    db: Database,
    request: Request,
    user_agent: UserAgentInfo,
    ip_address: IPAddress,
    verification_service: ClientVerificationService,
    grant_type: Annotated[
        Literal["authorization_code", "refresh_token", "password", "client_credentials"],
        Form(..., description="æŽˆæƒç±»åž‹ï¼šå¯†ç ã€åˆ·æ–°ä»¤ç‰Œå’ŒæŽˆæƒç ä¸‰ç§æŽˆæƒæ–¹å¼ã€‚"),
    ],
    client_id: Annotated[int, Form(..., description="å®¢æˆ·ç«¯ ID")],
    client_secret: Annotated[str, Form(..., description="å®¢æˆ·ç«¯å¯†é’¥")],
    redis: Redis,
    geoip: GeoIPService,
    api_version: APIVersion,
    code: Annotated[str | None, Form(description="æŽˆæƒç ï¼ˆä»…æŽˆæƒç æ¨¡å¼éœ€è¦ï¼‰")] = None,
    version_hash: Annotated[str | None, Form(description="Client version hash (optional)")] = None,
    scope: Annotated[str, Form(description="æƒé™èŒƒå›´ï¼ˆç©ºæ ¼åˆ†éš”ï¼Œé»˜è®¤ä¸º '*'ï¼‰")] = "*",
    username: Annotated[str | None, Form(description="ç”¨æˆ·åï¼ˆä»…å¯†ç æ¨¡å¼éœ€è¦ï¼‰")] = None,
    password: Annotated[str | None, Form(description="å¯†ç ï¼ˆä»…å¯†ç æ¨¡å¼éœ€è¦ï¼‰")] = None,
    refresh_token: Annotated[str | None, Form(description="åˆ·æ–°ä»¤ç‰Œï¼ˆä»…åˆ·æ–°ä»¤ç‰Œæ¨¡å¼éœ€è¦ï¼‰")] = None,
    web_uuid: Annotated[str | None, Header(include_in_schema=False, alias="X-UUID")] = None,
    cf_turnstile_response: Annotated[
        str, Form(description="Cloudflare Turnstile å“åº” token")
    ] = "XXXX.DUMMY.TOKEN.XXXX",
):
    # Turnstile éªŒè¯ï¼ˆä»…å¯¹éž osu! å®¢æˆ·ç«¯çš„å¯†ç æŽˆæƒæ¨¡å¼ï¼‰
    if grant_type == "password" and settings.enable_turnstile_verification and not user_agent.is_client:
        logger.debug(
            f"Turnstile check: grant_type={grant_type}, token={cf_turnstile_response[:20]}..., "
            f"enabled={settings.enable_turnstile_verification}, is_client={user_agent.is_client}"
        )
        success, error_msg = await turnstile_service.verify_token(cf_turnstile_response, ip_address)
        logger.info(f"Turnstile verification result: success={success}, error={error_msg}, ip={ip_address}")
        if not success:
            return create_oauth_error_response(
                error="invalid_request",
                description=f"Verification failed: {error_msg}",
                hint="Invalid or expired verification token",
            )

    scopes = scope.split(" ")

    client = (
        await db.exec(
            select(OAuthClient).where(
                OAuthClient.client_id == client_id,
                OAuthClient.client_secret == client_secret,
            )
        )
    ).first()
    #is_game_client = (client_id, client_secret) in [
    #    (settings.osu_client_id, settings.osu_client_secret),
    #    (settings.osu_web_client_id, settings.osu_web_client_secret),
    #]
    is_game_client = True

    if client is None and not is_game_client:
        return create_oauth_error_response(
            error="invalid_client",
            description=(
                "Client authentication failed (e.g., unknown client, "
                "no client authentication included, "
                "or unsupported authentication method)."
            ),
            hint="Invalid client credentials",
            status_code=401,
        )

    if grant_type == "password":
        if not username or not password:
            return create_oauth_error_response(
                error="invalid_request",
                description=(
                    "The request is missing a required parameter, includes an "
                    "invalid parameter value, "
                    "includes a parameter more than once, or is otherwise malformed."
                ),
                hint="Username and password required",
            )
        if scopes != ["*"]:
            return create_oauth_error_response(
                error="invalid_scope",
                description=(
                    "The requested scope is invalid, unknown, "
                    "or malformed. The client may not request "
                    "more than one scope at a time."
                ),
                hint="Only '*' scope is allowed for password grant type",
            )

        raw_user_agent = request.headers.get("user-agent") or user_agent.raw_ua or ""
        normalized_version_hash, request_version_hint, observed_hint_keys = await _extract_client_hints_from_request(
            request,
            version_hash,
        )
        version_validation = None
        if normalized_version_hash:
            version_validation = await verification_service.validate_client_version(normalized_version_hash)

        client_label_for_log = _derive_login_client_label(
            version_validation,
            raw_user_agent,
            fallback_display_name=user_agent.displayed_name,
            version_hint=request_version_hint or "",
        )

        # éªŒè¯ç”¨æˆ·
        user = await authenticate_user(db, username, password)
        if not user:
            # è®°å½•å¤±è´¥çš„ç™»å½•å°è¯•
            await LoginLogService.record_failed_login(
                db=db,
                request=request,
                attempted_username=username,
                login_method="password",
                user_agent=raw_user_agent,
                client_hash=normalized_version_hash or None,
                client_label=client_label_for_log,
                notes="Invalid credentials",
            )

            return create_oauth_error_response(
                error="invalid_grant",
                description=(
                    "The provided authorization grant (e.g., authorization code, "
                    "resource owner credentials) "
                    "or refresh token is invalid, expired, revoked, "
                    "does not match the redirection URI used in "
                    "the authorization request, or was issued to another client."
                ),
                hint="Incorrect sign in",
            )

        # ç¡®ä¿ç”¨æˆ·å¯¹è±¡ä¸Žå½“å‰ä¼šè¯å…³è”
        await db.refresh(user)

        user_id = user.id
        totp_key: TotpKeys | None = await user.awaitable_attrs.totp_key
        if normalized_version_hash:
            validation = version_validation or await verification_service.validate_client_version(normalized_version_hash)
            if not any((validation.client_name, validation.version, validation.os)):
                await verification_service.record_unknown_hash(
                    normalized_version_hash,
                    user_agent=raw_user_agent,
                    user_id=user_id,
                    source="oauth_token",
                )
            try:
                await redis.set(
                    LAST_CLIENT_HASH_KEY.format(user_id=user_id),
                    normalized_version_hash,
                    ex=60 * 60 * 24 * 120,
                )
            except Exception:
                logger.debug(f"Failed to store login hash for user {user_id}")
        elif user_agent.is_client:
            logger.info(
                "Client login without version hash: "
                f"user_id={user_id} client_id={client_id} ua={raw_user_agent!r} "
                f"hint_keys={observed_hint_keys} version_hint={request_version_hint!r}",
            )

        # ç”Ÿæˆä»¤ç‰Œ
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        access_token = create_access_token(data={"sub": str(user_id)}, expires_delta=access_token_expires)
        refresh_token_str = generate_refresh_token()
        token = await store_token(
            db,
            user_id,
            client_id,
            scopes,
            access_token,
            refresh_token_str,
            settings.access_token_expire_minutes * 60,
            settings.refresh_token_expire_minutes * 60,
            allow_multiple_devices=settings.enable_multi_device_login,  # ä½¿ç”¨é…ç½®å†³å®šæ˜¯å¦å¯ç”¨å¤šè®¾å¤‡æ”¯æŒ
        )
        token_id = token.id

        # èŽ·å–å›½å®¶ä»£ç 
        geo_info = geoip.lookup(ip_address)
        country_code = geo_info.get("country_iso", "XX")

        # æ£€æŸ¥æ˜¯å¦ä¸ºæ–°ä½ç½®ç™»å½•
        trusted_device = await LoginSessionService.check_trusted_device(db, user_id, ip_address, user_agent, web_uuid)

        # æ ¹æ® osu-web é€»è¾‘ç¡®å®šéªŒè¯æ–¹æ³•ï¼š
        # 1. å¦‚æžœ API ç‰ˆæœ¬æ”¯æŒ TOTP ä¸”ç”¨æˆ·å¯ç”¨äº† TOTPï¼Œåˆ™å§‹ç»ˆè¦æ±‚ TOTP éªŒè¯ï¼ˆæ— è®ºæ˜¯å¦ä¸ºä¿¡ä»»è®¾å¤‡ï¼‰
        # 2. å¦åˆ™ï¼Œå¦‚æžœæ˜¯æ–°è®¾å¤‡ä¸”å¯ç”¨äº†é‚®ä»¶éªŒè¯ï¼Œåˆ™è¦æ±‚é‚®ä»¶éªŒè¯
        # 3. å¦åˆ™ï¼Œä¸éœ€è¦éªŒè¯æˆ–è‡ªåŠ¨éªŒè¯
        session_verification_method = None
        if api_version >= SUPPORT_TOTP_VERIFICATION_VER and settings.enable_totp_verification and totp_key is not None:
            # TOTP éªŒè¯ä¼˜å…ˆï¼ˆå‚è€ƒ osu-web State.php:36ï¼‰
            session_verification_method = "totp"
            await LoginLogService.record_login(
                db=db,
                user_id=user_id,
                request=request,
                user_agent=raw_user_agent,
                client_hash=normalized_version_hash or None,
                client_label=client_label_for_log,
                login_success=True,
                login_method="password_pending_verification",
                notes="éœ€è¦ TOTP éªŒè¯",
            )
        elif not trusted_device and settings.enable_email_verification:
            # å¦‚æžœæ˜¯æ–°è®¾å¤‡ç™»å½•ï¼Œéœ€è¦é‚®ä»¶éªŒè¯
            # åˆ·æ–°ç”¨æˆ·å¯¹è±¡ä»¥ç¡®ä¿å±žæ€§å·²åŠ è½½
            await db.refresh(user)
            session_verification_method = "mail"
            await EmailVerificationService.send_verification_email(
                db,
                redis,
                user_id,
                user.username,
                user.email,
                ip_address,
                user_agent,
                user.country_code,
            )

            # è®°å½•éœ€è¦äºŒæ¬¡éªŒè¯çš„ç™»å½•å°è¯•
            await LoginLogService.record_login(
                db=db,
                user_id=user_id,
                request=request,
                user_agent=raw_user_agent,
                client_hash=normalized_version_hash or None,
                client_label=client_label_for_log,
                login_success=True,
                login_method="password_pending_verification",
                notes=(
                    f"é‚®ç®±éªŒè¯: User-Agent: {user_agent.raw_ua}, å®¢æˆ·ç«¯: {user_agent.displayed_name} "
                    f"IP: {ip_address}, å›½å®¶: {country_code}"
                ),
            )
        elif not trusted_device:
            # æ–°è®¾å¤‡ç™»å½•ä½†é‚®ä»¶éªŒè¯åŠŸèƒ½è¢«ç¦ç”¨ï¼Œç›´æŽ¥æ ‡è®°ä¼šè¯ä¸ºå·²éªŒè¯
            await LoginSessionService.mark_session_verified(
                db, redis, user_id, token_id, ip_address, user_agent, web_uuid
            )
            logger.debug(f"New location login detected but email verification disabled, auto-verifying user {user_id}")
        else:
            # ä¸æ˜¯æ–°è®¾å¤‡ç™»å½•ï¼Œæ­£å¸¸ç™»å½•
            await LoginLogService.record_login(
                db=db,
                user_id=user_id,
                request=request,
                user_agent=raw_user_agent,
                client_hash=normalized_version_hash or None,
                client_label=client_label_for_log,
                login_success=True,
                login_method="password",
                notes=f"æ­£å¸¸ç™»å½• - IP: {ip_address}, å›½å®¶: {country_code}",
            )

        if session_verification_method:
            await LoginSessionService.create_session(
                db, user_id, token_id, ip_address, user_agent.raw_ua, not trusted_device, web_uuid, False
            )
            await LoginSessionService.set_login_method(user_id, token_id, session_verification_method, redis)
        else:
            await LoginSessionService.create_session(
                db, user_id, token_id, ip_address, user_agent.raw_ua, not trusted_device, web_uuid, True
            )

        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106
            expires_in=settings.access_token_expire_minutes * 60,
            refresh_token=refresh_token_str,
            scope=scope,
        )

    elif grant_type == "refresh_token":
        # åˆ·æ–°ä»¤ç‰Œæµç¨‹
        if not refresh_token:
            return create_oauth_error_response(
                error="invalid_request",
                description=(
                    "The request is missing a required parameter, "
                    "includes an invalid parameter value, "
                    "includes a parameter more than once, or is otherwise malformed."
                ),
                hint="Refresh token required",
            )

        # éªŒè¯åˆ·æ–°ä»¤ç‰Œ
        token_record = await get_token_by_refresh_token(db, refresh_token)
        if not token_record:
            return create_oauth_error_response(
                error="invalid_grant",
                description=(
                    "The provided authorization grant (e.g., authorization code, "
                    "resource owner credentials) or refresh token is "
                    "invalid, expired, revoked, "
                    "does not match the redirection URI used "
                    "in the authorization request, or was issued to another client."
                ),
                hint="Invalid refresh token",
            )

        # ç”Ÿæˆæ–°çš„è®¿é—®ä»¤ç‰Œ
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        access_token = create_access_token(data={"sub": str(token_record.user_id)}, expires_delta=access_token_expires)
        new_refresh_token = generate_refresh_token()

        # æ›´æ–°ä»¤ç‰Œ
        await store_token(
            db,
            token_record.user_id,
            client_id,
            scopes,
            access_token,
            new_refresh_token,
            settings.access_token_expire_minutes * 60,
            settings.refresh_token_expire_minutes * 60,
            allow_multiple_devices=settings.enable_multi_device_login,  # ä½¿ç”¨é…ç½®å†³å®šæ˜¯å¦å¯ç”¨å¤šè®¾å¤‡æ”¯æŒ
        )
        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106
            expires_in=settings.access_token_expire_minutes * 60,
            refresh_token=new_refresh_token,
            scope=scope,
        )
    elif grant_type == "authorization_code":
        if client is None:
            return create_oauth_error_response(
                error="invalid_client",
                description=(
                    "Client authentication failed (e.g., unknown client, "
                    "no client authentication included, "
                    "or unsupported authentication method)."
                ),
                hint="Invalid client credentials",
                status_code=401,
            )

        if not code:
            return create_oauth_error_response(
                error="invalid_request",
                description=(
                    "The request is missing a required parameter, "
                    "includes an invalid parameter value, "
                    "includes a parameter more than once, or is otherwise malformed."
                ),
                hint="Authorization code required",
            )

        code_result = await get_user_by_authorization_code(db, redis, client_id, code)
        if not code_result:
            return create_oauth_error_response(
                error="invalid_grant",
                description=(
                    "The provided authorization grant (e.g., authorization code, "
                    "resource owner credentials) or refresh token is invalid, "
                    "expired, revoked, does not match the redirection URI used in "
                    "the authorization request, or was issued to another client."
                ),
                hint="Invalid authorization code",
            )
        user, scopes = code_result

        # ç¡®ä¿ç”¨æˆ·å¯¹è±¡ä¸Žå½“å‰ä¼šè¯å…³è”
        await db.refresh(user)

        # ç”Ÿæˆä»¤ç‰Œ
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        user_id = user.id
        access_token = create_access_token(data={"sub": str(user_id)}, expires_delta=access_token_expires)
        refresh_token_str = generate_refresh_token()

        # å­˜å‚¨ä»¤ç‰Œ
        await store_token(
            db,
            user_id,
            client_id,
            scopes,
            access_token,
            refresh_token_str,
            settings.access_token_expire_minutes * 60,
            settings.refresh_token_expire_minutes * 60,
            allow_multiple_devices=settings.enable_multi_device_login,  # ä½¿ç”¨é…ç½®å†³å®šæ˜¯å¦å¯ç”¨å¤šè®¾å¤‡æ”¯æŒ
        )

        # æ‰“å°jwt
        logger.info(f"Generated JWT for user {user_id}: {access_token}")

        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106
            expires_in=settings.access_token_expire_minutes * 60,
            refresh_token=refresh_token_str,
            scope=" ".join(scopes),
        )
    elif grant_type == "client_credentials":
        if client is None:
            return create_oauth_error_response(
                error="invalid_client",
                description=(
                    "Client authentication failed (e.g., unknown client, "
                    "no client authentication included, "
                    "or unsupported authentication method)."
                ),
                hint="Invalid client credentials",
                status_code=401,
            )
        elif scopes != ["public"]:
            return create_oauth_error_response(
                error="invalid_scope",
                description="The requested scope is invalid, unknown, or malformed.",
                hint="Scope must be 'public'",
                status_code=400,
            )

        # ç”Ÿæˆä»¤ç‰Œ
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        access_token = create_access_token(data={"sub": "3"}, expires_delta=access_token_expires)
        refresh_token_str = generate_refresh_token()

        # å­˜å‚¨ä»¤ç‰Œ
        await store_token(
            db,
            BANCHOBOT_ID,
            client_id,
            scopes,
            access_token,
            refresh_token_str,
            settings.access_token_expire_minutes * 60,
            settings.refresh_token_expire_minutes * 60,
            allow_multiple_devices=settings.enable_multi_device_login,  # ä½¿ç”¨é…ç½®å†³å®šæ˜¯å¦å¯ç”¨å¤šè®¾å¤‡æ”¯æŒ
        )

        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106
            expires_in=settings.access_token_expire_minutes * 60,
            refresh_token=refresh_token_str,
            scope=" ".join(scopes),
        )


@router.post(
    "/password-reset/request",
    name="è¯·æ±‚å¯†ç é‡ç½®",
    description="é€šè¿‡é‚®ç®±è¯·æ±‚å¯†ç é‡ç½®éªŒè¯ç ",
)
async def request_password_reset(
    request: Request,
    email: Annotated[str, Form(..., description="é‚®ç®±åœ°å€")],
    redis: Redis,    verification_service: ClientVerificationService,
    user_agent: UserAgentInfo,
    cf_turnstile_response: Annotated[
        str, Form(description="Cloudflare Turnstile å“åº” token")
    ] = "XXXX.DUMMY.TOKEN.XXXX",
):
    """
    è¯·æ±‚å¯†ç é‡ç½®
    """
    # Turnstile éªŒè¯ï¼ˆä»…å¯¹éž osu! å®¢æˆ·ç«¯ï¼‰
    if settings.enable_turnstile_verification and not user_agent.is_client:
        success, error_msg = await turnstile_service.verify_token(cf_turnstile_response, ip_address)
        if not success:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"Verification failed: {error_msg}"},
            )

    # èŽ·å–å®¢æˆ·ç«¯ä¿¡æ¯
    user_agent_str = request.headers.get("User-Agent", "")

    # è¯·æ±‚å¯†ç é‡ç½®
    success, message = await password_reset_service.request_password_reset(
        email=email.lower().strip(),
        ip_address=ip_address,
        user_agent=user_agent_str,
        redis=redis,
    )

    if success:
        return JSONResponse(status_code=200, content={"success": True, "message": message})
    else:
        return JSONResponse(status_code=400, content={"success": False, "error": message})


@router.post("/password-reset/reset", name="é‡ç½®å¯†ç ", description="ä½¿ç”¨éªŒè¯ç é‡ç½®å¯†ç ")
async def reset_password(
    email: Annotated[str, Form(..., description="é‚®ç®±åœ°å€")],
    reset_code: Annotated[str, Form(..., description="é‡ç½®éªŒè¯ç ")],
    new_password: Annotated[str, Form(..., description="æ–°å¯†ç ")],
    redis: Redis,    verification_service: ClientVerificationService,
):
    """
    é‡ç½®å¯†ç 
    """
    # èŽ·å–å®¢æˆ·ç«¯ä¿¡æ¯
    # é‡ç½®å¯†ç 
    success, message = await password_reset_service.reset_password(
        email=email.lower().strip(),
        reset_code=reset_code.strip(),
        new_password=new_password,
        ip_address=ip_address,
        redis=redis,
    )

    if success:
        return JSONResponse(status_code=200, content={"success": True, "message": message})
    else:
        return JSONResponse(status_code=400, content={"success": False, "error": message})

