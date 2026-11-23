"""Bilibili web API client used by the plugin.

Only what the plugin needs: QR-code login, the official cookie refresh flow,
WBI signing, video info, play URLs and stream downloads. Endpoints follow
https://github.com/SocialSisterYi/bilibili-API-collect.
"""

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

import aiohttp
from astrbot.api import logger
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
)
# Ask the cookie/info endpoint at most once a day whether a refresh is due,
# as the documented web flow does.
REFRESH_CHECK_INTERVAL = 24 * 3600
WBI_KEY_TTL = 3600
MIXIN_KEY_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]  # fmt: skip
REFRESH_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""
# DASH streams plus 4K. HDR, Dolby Vision and 8K are left out on purpose:
# QQ cannot show them properly.
PLAY_FNVAL = 16 | 128


def cdn_priority(url: str) -> int:
    """Rank stream URLs like BiliTools does, lower first.

    Bilibili's own mirror hosts come first, then other UPOS storage, then
    "cn-" bcache nodes; PCDN nodes run by third parties are often slow or
    refuse connections, so they come last.

    Args:
        url: A stream URL.

    Returns:
        0 to 3.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host.endswith((".bilivideo.com", ".bilivideo.cn")):
        return 3
    storage = (urllib.parse.parse_qs(parts.query).get("os") or [""])[0]
    if "mirror" in host and storage.endswith("bv"):
        return 0
    if storage == "upos":
        return 1
    if host.startswith("cn-") and storage == "bcache":
        return 2
    return 3


class BiliError(Exception):
    """A Bilibili request failed. The message is shown in the chat."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class TooLargeError(BiliError):
    """A download would go over the size budget."""


class BiliClient:
    """Keeps the login state and talks to the Bilibili web API.

    Args:
        auth_path: JSON file holding the login cookies and refresh token.
    """

    def __init__(self, auth_path: Path):
        self.auth_path = auth_path
        self.cookies: dict[str, str] = {}
        self.refresh_token = ""
        self.checked_at = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._mixin_key = ""
        self._mixin_key_at = 0.0
        self._refresh_lock = asyncio.Lock()
        try:
            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.cookies = {
                k: v for k, v in saved.get("cookies", {}).items() if k in COOKIE_NAMES
            }
            self.refresh_token = saved.get("refresh_token", "")
            self.checked_at = saved.get("checked_at", 0.0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.warning(f"Failed to read the saved Bilibili login: {e}")

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def _save(self) -> None:
        tmp = self.auth_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "cookies": self.cookies,
                    "refresh_token": self.refresh_token,
                    "checked_at": self.checked_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.auth_path)

    def _http(self) -> aiohttp.ClientSession:
        # Cookies are sent by hand so stream downloads from the CDN never
        # carry the login cookies.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=HEADERS,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(sock_connect=15, sock_read=60),
            )
        return self._session

    async def _api(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        allow_codes: tuple[int, ...] = (0,),
    ) -> tuple[dict, dict[str, str]]:
        """Call a JSON endpoint with the saved cookies.

        Args:
            method: HTTP method.
            url: Endpoint URL.
            params: Query parameters.
            data: Form body for POST requests.
            allow_codes: Response codes that count as success.

        Returns:
            The "data" object and the Bilibili cookies set by the response.

        Raises:
            BiliError: The request failed or returned another code.
        """
        headers = {}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            async with self._http().request(
                method, url, params=params, data=data, headers=headers
            ) as resp:
                payload = await resp.json(content_type=None)
                cookies = {
                    name: morsel.value
                    for name, morsel in resp.cookies.items()
                    if name in COOKIE_NAMES and morsel.value not in ("", "deleted")
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise BiliError(f"连不上 B 站（{e or type(e).__name__}）") from e
        code = payload.get("code")
        if code not in allow_codes:
            raise BiliError(payload.get("message") or f"错误码 {code}", code)
        return payload.get("data") or {}, cookies

    async def get_nav(self) -> dict:
        """Return the current user info; also works when not logged in."""
        data, _ = await self._api(
            "GET", "https://api.bilibili.com/x/web-interface/nav", allow_codes=(0, -101)
        )
        return data

    async def ensure_buvid(self) -> None:
        """Get the buvid3/buvid4 device cookies once; requests without them
        are more likely to hit risk control."""
        if self.cookies.get("buvid3"):
            return
        data, _ = await self._api(
            "GET", "https://api.bilibili.com/x/frontend/finger/spi"
        )
        self.cookies["buvid3"] = data.get("b_3", "")
        self.cookies["buvid4"] = data.get("b_4", "")
        self._save()

    async def _wbi_sign(self, params: dict) -> dict:
        if time.time() - self._mixin_key_at > WBI_KEY_TTL:
            wbi = (await self.get_nav()).get("wbi_img") or {}
            raw = "".join(
                wbi.get(key, "").rsplit("/", 1)[-1].split(".")[0]
                for key in ("img_url", "sub_url")
            )
            if len(raw) < 64:
                raise BiliError("拿不到 WBI 签名密钥")
            self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_TABLE)[:32]
            self._mixin_key_at = time.time()
        signed = {
            key: re.sub(r"[!'()*]", "", str(value))
            for key, value in sorted({**params, "wts": int(time.time())}.items())
        }
        query = urllib.parse.urlencode(signed)
        signed["w_rid"] = hashlib.md5((query + self._mixin_key).encode()).hexdigest()
        return signed

    async def resolve_short_link(self, url: str) -> str:
        """Follow a b23.tv / bili2233.cn short link to its target URL.

        Only the Location headers are read, with a curl User-Agent: from a
        server IP a browser User-Agent gets 412 from risk control.

        Args:
            url: The short link.

        Returns:
            The first URL that is not a short link.
        """
        for _ in range(5):
            if not re.match(r"https?://(?:b23\.tv|bili2233\.cn)/", url, re.I):
                return url
            try:
                async with self._http().get(
                    url, headers={"User-Agent": "curl/8.0"}, allow_redirects=False
                ) as resp:
                    location = resp.headers.get("Location")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise BiliError(f"短链接打不开（{e or type(e).__name__}）") from e
            if not location:
                return url
            url = urllib.parse.urljoin(url, location)
        return url

    async def get_view(self, bvid: str = "", aid: str = "") -> dict:
        params = {"bvid": bvid} if bvid else {"aid": aid}
        data, _ = await self._api(
            "GET", "https://api.bilibili.com/x/web-interface/view", params=params
        )
        return data

    async def get_play(self, aid: int, cid: int, mp4: bool = False) -> dict:
        """Return play URL data.

        Args:
            aid: The video's av id.
            cid: The part's cid.
            mp4: Ask for the single-file 720P MP4 instead of every DASH
                stream. Guests get 480P at most over DASH but 720P this way.

        Returns:
            The "data" object of the playurl response.
        """
        params = {
            "avid": aid,
            "cid": cid,
            "fnval": 1 if mp4 else PLAY_FNVAL,
            "fnver": 0,
            "fourk": 1,
            "otype": "json",
            "platform": "pc",
        }
        if mp4:
            params["qn"] = 64
        if self.cookies.get("buvid3"):
            ms = str(int(time.time() * 1000))
            params["session"] = hashlib.md5(
                (self.cookies["buvid3"] + ms).encode()
            ).hexdigest()
        if not self.logged_in:
            # The documented try_look=1 that used to give guests 720P/1080P
            # over DASH no longer does (checked 2026-11).
            params["gaia_source"] = "view-card"
        data, _ = await self._api(
            "GET",
            "https://api.bilibili.com/x/player/wbi/playurl",
            params=await self._wbi_sign(params),
        )
        return data

    async def download(self, urls: list[str], path: Path, budget: int) -> int:
        """Download one stream, trying the backup URLs in turn.

        Args:
            urls: Main URL followed by backup URLs.
            path: File to write.
            budget: Byte limit for this stream.

        Returns:
            The number of bytes written.

        Raises:
            TooLargeError: The stream is larger than the budget.
            BiliError: No URL could be downloaded.
        """
        last_error: Exception | None = None
        for url in sorted(dict.fromkeys(urls), key=cdn_priority):
            try:
                async with self._http().get(url) as resp:
                    resp.raise_for_status()
                    expected = resp.content_length
                    if expected and expected > budget:
                        raise TooLargeError(f"{expected} > {budget}")
                    size = 0
                    with path.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(1 << 20):
                            size += len(chunk)
                            if size > budget:
                                raise TooLargeError(f"{size} > {budget}")
                            f.write(chunk)
                    if expected and size < expected:
                        raise BiliError(f"只下载到 {size}/{expected} 字节")
                    return size
            except TooLargeError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, BiliError) as e:
                last_error = e
                logger.warning(f"Bilibili stream download failed, trying next URL: {e}")
        raise BiliError(f"视频下载失败（{last_error}）")

    async def qr_generate(self) -> tuple[str, str]:
        """Start a QR-code login.

        Returns:
            The URL to put in the QR code and the key to poll with.
        """
        data, _ = await self._api(
            "GET", "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        )
        return data["url"], data["qrcode_key"]

    async def qr_poll(self, key: str) -> int:
        """Check a QR-code login and save the login when it succeeded.

        Args:
            key: The key from qr_generate.

        Returns:
            0 when logged in, 86101 not scanned, 86090 scanned but not
            confirmed, 86038 expired.
        """
        data, cookies = await self._api(
            "GET",
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": key},
        )
        if data.get("code") != 0:
            return data.get("code", -1)
        if not cookies.get("SESSDATA"):
            raise BiliError("扫码成功了，但 B 站没有返回登录 Cookie")
        self.cookies = {
            k: v for k, v in self.cookies.items() if k.startswith("buvid")
        } | cookies
        self.refresh_token = data.get("refresh_token", "")
        self.checked_at = time.time()
        self._save()
        return 0

    async def refresh_if_due(self) -> str:
        """Refresh the login cookies when Bilibili asks for it.

        Checks at most once a day. Follows the documented web flow: check,
        get refresh_csrf through the correspond page, refresh, then confirm
        so the old cookies stop working.

        Returns:
            An empty string, or a problem the admins should be told about.
        """
        if not self.logged_in or time.time() - self.checked_at < REFRESH_CHECK_INTERVAL:
            return ""
        async with self._refresh_lock:
            if time.time() - self.checked_at < REFRESH_CHECK_INTERVAL:
                return ""
            self.checked_at = time.time()
            self._save()
            try:
                info, _ = await self._api(
                    "GET",
                    "https://passport.bilibili.com/x/passport-login/web/cookie/info",
                    params={"csrf": self.cookies.get("bili_jct", "")},
                )
            except BiliError as e:
                if e.code == -101:
                    return "B 站登录已失效，现在按游客下载（最高 720P）。私聊我发 /b站登录 重新扫码。"
                logger.warning(f"Bilibili login check failed: {e}")
                return ""
            if not info.get("refresh"):
                return ""
            try:
                key = serialization.load_pem_public_key(REFRESH_PUBLIC_KEY)
                correspond_path = key.encrypt(
                    f"refresh_{info['timestamp']}".encode(),
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                ).hex()
                cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
                async with self._http().get(
                    f"https://www.bilibili.com/correspond/1/{correspond_path}",
                    headers={"Cookie": cookie_header},
                ) as resp:
                    page = await resp.text()
                match = re.search(r'<div id="1-name">([^<]+)</div>', page)
                if not match:
                    raise BiliError("拿不到 refresh_csrf")
                data, cookies = await self._api(
                    "POST",
                    "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
                    data={
                        "csrf": self.cookies.get("bili_jct", ""),
                        "refresh_csrf": match.group(1).strip(),
                        "source": "main_web",
                        "refresh_token": self.refresh_token,
                    },
                )
                if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
                    raise BiliError("续期响应里没有新 Cookie")
            except (BiliError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                return f"B 站登录续期失败（{e}）。私聊我发 /b站登录 重新扫码。"
            old_token = self.refresh_token
            self.cookies |= cookies
            self.refresh_token = data.get("refresh_token", "")
            self._save()
            logger.info("Bilibili login cookies refreshed")
            try:
                await self._api(
                    "POST",
                    "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
                    data={
                        "csrf": self.cookies["bili_jct"],
                        "refresh_token": old_token,
                    },
                )
            except BiliError as e:
                # The new cookies already work; this only retires the old ones.
                logger.warning(f"Bilibili refresh confirmation failed: {e}")
            return ""
