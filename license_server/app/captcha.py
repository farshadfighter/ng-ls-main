"""The admin login's own picture check (CAPTCHA_MODE=builtin, the default).

This server draws 5 characters in a distorted picture; the login must carry them back. Nothing
outside the server is asked, so it works where Cloudflare (Turnstile) cannot be reached.

* Each picture has a random id and lives in Redis for TTL_SECONDS, as the SHA-256 of its answer
  (never the answer itself).
* A picture is spent by the first login that names it, right or wrong (GETDEL): a bot gets one
  guess per picture and must fetch a new one, under the per-IP rate limit.
* The characters avoid look-alikes (no 0/O, 1/I/L/7, 2/Z, 4/A, 5/S, 8/B, 6/G, U/V) and the answer
  is read without regard to case or spaces.
* Fails closed: without Redis no picture can be made or checked, and the login answers 503
  rather than let anyone through unchecked (CaptchaUnavailable).
"""
import hashlib
import hmac
import io
import math
import re
import secrets
from functools import lru_cache

import redis
from PIL import Image, ImageDraw, ImageFont

from .core.redis_client import redis_client

ALPHABET = "ACDEFHJKMNPRTVWXY39"
LENGTH = 5
TTL_SECONDS = 120
KEY_PREFIX = "captcha:"
WIDTH, HEIGHT = 220, 72

_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_rng = secrets.SystemRandom()


class CaptchaUnavailable(Exception):
    """Redis could not be reached: no picture can be made or checked."""


def _digest(answer: str) -> str:
    return hashlib.sha256(answer.encode()).hexdigest()


def normalise(answer: str) -> str:
    """What the person typed, as compared: no spaces, upper case."""
    return "".join((answer or "").split()).upper()


def new_challenge() -> tuple[str, bytes]:
    """A new picture: its id and the PNG. The answer is kept (hashed) for TTL_SECONDS."""
    answer = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    challenge_id = secrets.token_urlsafe(24)
    try:
        redis_client.set(KEY_PREFIX + challenge_id, _digest(answer), ex=TTL_SECONDS)
    except redis.RedisError as exc:
        raise CaptchaUnavailable(str(exc)) from exc
    return challenge_id, render(answer)


def check(challenge_id: str, answer: str) -> bool:
    """True when `answer` is what the picture showed. Spends the picture either way."""
    if not challenge_id or not _ID.match(challenge_id):
        return False
    try:
        stored = redis_client.getdel(KEY_PREFIX + challenge_id)
    except redis.RedisError as exc:
        raise CaptchaUnavailable(str(exc)) from exc
    if not stored:
        return False  # unknown, expired or already used
    return hmac.compare_digest(stored, _digest(normalise(answer)))


# -- drawing --------------------------------------------------------------------------------------

@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont:
    # Pillow's own embedded TrueType face: nothing to install in the image.
    return ImageFont.load_default(size=size)


def _wave(img: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Shift every column up or down along a sine, so the letters bend."""
    amp = _rng.uniform(2.5, 4.5)
    period = _rng.uniform(45, 75)
    phase = _rng.uniform(0, 2 * math.pi)
    out = Image.new("RGB", img.size, background)
    for x in range(img.width):
        dy = round(amp * math.sin(2 * math.pi * x / period + phase))
        out.paste(img.crop((x, 0, x + 1, img.height)), (x, dy))
    return out


def render(text: str) -> bytes:
    """The PNG of `text`: tilted, overlapping characters on speckle, crossed by two curves, bent."""
    background = tuple(_rng.randint(232, 248) for _ in range(3))
    ink = (_rng.randint(20, 70), _rng.randint(30, 80), _rng.randint(90, 140))
    img = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(img)
    for _ in range(350):
        shade = _rng.randint(150, 215)
        draw.point((_rng.randrange(WIDTH), _rng.randrange(HEIGHT)), fill=(shade, shade, shade))

    x = _rng.randint(6, 12)
    for ch in text:
        glyph = Image.new("L", (64, HEIGHT), 0)
        ImageDraw.Draw(glyph).text((32, HEIGHT // 2), ch, font=_font(_rng.randint(38, 46)), fill=255, anchor="mm")
        glyph = glyph.rotate(_rng.uniform(-24, 24), resample=Image.Resampling.BICUBIC)
        img.paste(ink, (x, _rng.randint(-6, 6)), glyph)
        x += _rng.randint(32, 37)

    for width in (2, 1):
        y0, amp = _rng.uniform(20, HEIGHT - 20), _rng.uniform(6, 14)
        period, phase = _rng.uniform(60, 140), _rng.uniform(0, 2 * math.pi)
        points = [(px, y0 + amp * math.sin(2 * math.pi * px / period + phase)) for px in range(0, WIDTH + 1, 4)]
        draw.line(points, fill=ink, width=width, joint="curve")

    out = io.BytesIO()
    _wave(img, background).save(out, "PNG", optimize=True)
    return out.getvalue()
