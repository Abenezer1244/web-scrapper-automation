"""Profile photo + profile fields (migration 111): /auth/avatar and /auth/profile.

Real images built with Pillow, real DB, real Redis. No mocks.
"""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import generate_api_key
from src.db.models import User, UserAvatar
from src.utils.avatar import MAX_UPLOAD_BYTES, OUTPUT_SIDE


def _image(fmt: str, size=(800, 600), mode="RGB", **save) -> bytes:
    img = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else None)
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    return buf.getvalue()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(client: AsyncClient, token: str, data: bytes, ctype: str):
    return await client.post(
        "/auth/avatar", content=data, headers={**_auth(token), "Content-Type": ctype}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "ctype"), [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]
)
async def test_upload_valid_formats_returns_256_webp(
    client: AsyncClient, starter_token: str, fmt: str, ctype: str
) -> None:
    r = await _upload(client, starter_token, _image(fmt), ctype)
    assert r.status_code == 200, r.text
    version = r.json()["avatar_version"]
    assert version

    got = await client.get(f"/auth/avatar?v={version}", headers=_auth(starter_token))
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/webp"
    assert "immutable" in got.headers["cache-control"]
    img = Image.open(io.BytesIO(got.content))
    assert img.format == "WEBP" and img.size == (OUTPUT_SIDE, OUTPUT_SIDE)


@pytest.mark.asyncio
async def test_png_transparency_is_kept(client: AsyncClient, starter_token: str) -> None:
    r = await _upload(client, starter_token, _image("PNG", mode="RGBA"), "image/png")
    got = await client.get(
        f"/auth/avatar?v={r.json()['avatar_version']}", headers=_auth(starter_token)
    )
    assert Image.open(io.BytesIO(got.content)).mode == "RGBA"


@pytest.mark.asyncio
async def test_exif_and_appended_payload_are_stripped(
    client: AsyncClient, starter_token: str
) -> None:
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"  # Make
    marker = b"<script>alert(1)</script>PK\x03\x04polyglot"
    data = _image("JPEG", exif=exif.tobytes()) + marker
    r = await _upload(client, starter_token, data, "image/jpeg")
    assert r.status_code == 200, r.text
    got = await client.get(
        f"/auth/avatar?v={r.json()['avatar_version']}", headers=_auth(starter_token)
    )
    assert b"SecretCameraMaker" not in got.content
    assert b"polyglot" not in got.content and b"<script>" not in got.content
    assert not Image.open(io.BytesIO(got.content)).getexif()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "ctype", "status"),
    [
        (b"MZ\x90\x00 this is a windows executable", "image/png", 422),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/png", 422),
        (_image("GIF"), "image/png", 422),          # real image, disallowed decoder
        (_image("PNG"), "text/plain", 415),          # MIME outside the allowlist
        (_image("PNG"), "image/gif", 415),
        (b"", "image/png", 422),
        (_image("JPEG")[:400], "image/jpeg", 422),    # truncated
        (_image("PNG", size=(32, 32)), "image/png", 422),     # too small
        (_image("PNG", size=(4097, 64)), "image/png", 422),   # too large a side
    ],
    ids=["exe", "svg", "gif", "text-mime", "gif-mime", "empty", "truncated", "tiny", "huge-side"],
)
async def test_invalid_uploads_are_refused(
    client: AsyncClient, starter_token: str, data: bytes, ctype: str, status: int
) -> None:
    r = await _upload(client, starter_token, data, ctype)
    assert r.status_code == status, r.text
    me = await client.get("/auth/me", headers=_auth(starter_token))
    assert me.json()["avatar_version"] is None


@pytest.mark.asyncio
async def test_decompression_bomb_refused_without_decoding(
    client: AsyncClient, starter_token: str
) -> None:
    # 20000 x 20000 one-bit canvas compresses to a few KB but declares 400 MP.
    bomb = _image("PNG", size=(20000, 20000), mode="1")
    assert len(bomb) < MAX_UPLOAD_BYTES
    r = await _upload(client, starter_token, bomb, "image/png")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_animated_image_refused(client: AsyncClient, starter_token: str) -> None:
    frames = [Image.new("RGB", (100, 100), c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:])
    r = await _upload(client, starter_token, buf.getvalue(), "image/webp")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_oversized_body_is_413(client: AsyncClient, starter_token: str) -> None:
    r = await _upload(client, starter_token, b"\0" * (MAX_UPLOAD_BYTES + 1), "image/png")
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_replace_then_remove(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
) -> None:
    first = (await _upload(client, starter_token, _image("PNG"), "image/png")).json()
    second = (await _upload(client, starter_token, _image("JPEG"), "image/jpeg")).json()
    v1, v2 = first["avatar_version"], second["avatar_version"]
    assert v1 != v2, "a replacement must get a new version (cache busting)"

    stale = await client.get(f"/auth/avatar?v={v1}", headers=_auth(starter_token))
    assert stale.headers["cache-control"] == "private, no-store"
    fresh = await client.get(f"/auth/avatar?v={v2}", headers=_auth(starter_token))
    assert "immutable" in fresh.headers["cache-control"]

    removed = await client.delete("/auth/avatar", headers=_auth(starter_token))
    assert removed.status_code == 200 and removed.json()["avatar_version"] is None
    assert (await client.get("/auth/avatar", headers=_auth(starter_token))).status_code == 404
    image = (
        await db.execute(select(UserAvatar.image).where(UserAvatar.user_id == starter_user.id))
    ).scalar_one()
    assert image is None, "remove must erase the bytes, not just hide them"


@pytest.mark.asyncio
async def test_photo_is_strictly_per_user(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_user: User, business_token: str,
) -> None:
    # A smuggled user_id (query or body) cannot redirect the write.
    r = await client.post(
        f"/auth/avatar?user_id={business_user.id}", content=_image("PNG"),
        headers={**_auth(starter_token), "Content-Type": "image/png"},
    )
    assert r.status_code == 200
    me_b = await client.get("/auth/me", headers=_auth(business_token))
    assert me_b.json()["avatar_version"] is None
    assert (await client.get(
        f"/auth/avatar?user_id={starter_user.id}", headers=_auth(business_token)
    )).status_code == 404, "user B must never receive user A's photo"
    assert (await client.delete(
        f"/auth/avatar?user_id={starter_user.id}", headers=_auth(business_token)
    )).status_code == 200
    me_a = await client.get("/auth/me", headers=_auth(starter_token))
    assert me_a.json()["avatar_version"] == r.json()["avatar_version"], (
        "B's remove touched A's photo"
    )


@pytest.mark.asyncio
async def test_upload_needs_a_session_not_an_api_key(
    client: AsyncClient, db: AsyncSession, business_user: User
) -> None:
    raw, key_hash = generate_api_key()
    await db.execute(update(User).where(User.id == business_user.id).values(api_key_hash=key_hash))
    await db.commit()
    for method in ("post", "delete"):
        r = await getattr(client, method)(
            "/auth/avatar", headers={"Authorization": f"Bearer {raw}", "Content-Type": "image/png"},
            **({"content": _image("PNG")} if method == "post" else {}),
        )
        assert r.status_code == 403, (method, r.text)
    assert (await client.post("/auth/avatar", content=_image("PNG"),
                              headers={"Content-Type": "image/png"})).status_code == 401


@pytest.mark.asyncio
async def test_avatar_writes_are_rate_limited(client: AsyncClient, starter_token: str) -> None:
    codes = [
        (await client.delete("/auth/avatar", headers=_auth(starter_token))).status_code
        for _ in range(21)
    ]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# ─── Profile fields ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_profile_names_unicode_and_timezone(
    client: AsyncClient, starter_token: str
) -> None:
    body = {"first_name": "  Zoë  ", "last_name": "Nguyễn-O’Brien", "timezone": "America/Los_Angeles"}
    r = await client.put("/auth/profile", json=body, headers=_auth(starter_token))
    assert r.status_code == 200, r.text
    me = (await client.get("/auth/me", headers=_auth(starter_token))).json()
    assert (me["first_name"], me["last_name"]) == ("Zoë", "Nguyễn-O’Brien")
    assert me["timezone"] == "America/Los_Angeles"

    # Omitted timezone = unchanged (the name-only gate must not clear it).
    r = await client.put("/auth/profile", json={"first_name": "Ann", "last_name": "Lee"},
                         headers=_auth(starter_token))
    assert r.json()["timezone"] == "America/Los_Angeles"
    # Explicit null clears it.
    r = await client.put("/auth/profile", json={"first_name": "Ann", "last_name": "Lee",
                                                "timezone": None}, headers=_auth(starter_token))
    assert r.json()["timezone"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"first_name": "Ann", "last_name": "Lee", "timezone": "Mars/Olympus"},
        {"first_name": "Ann", "last_name": "Lee", "timezone": "../../etc/passwd"},
        {"first_name": "   ", "last_name": "Lee"},
        {"first_name": "Ann", "last_name": "x" * 121},
        {"first_name": "Ann", "last_name": "Lee", "plan": "agency"},
    ],
)
async def test_profile_rejects_invalid(client: AsyncClient, starter_token: str, body) -> None:
    r = await client.put("/auth/profile", json=body, headers=_auth(starter_token))
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_profile_save_keeps_the_photo_in_its_response(
    client: AsyncClient, starter_token: str
) -> None:
    """The FE writes this response straight into its cached profile; a response
    without the photo version would blank the avatar until the next refetch."""
    v = (await _upload(client, starter_token, _image("PNG"), "image/png")).json()["avatar_version"]
    r = await client.put("/auth/profile", json={"first_name": "Ann", "last_name": "Lee"},
                         headers=_auth(starter_token))
    assert r.json()["avatar_version"] == v
