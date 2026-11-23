"""Send the Bilibili videos people share in chats.

Finds a Bilibili video link or QQ mini-app card in a message, downloads the
best quality that fits the size limit and sends the video back. The message
can be stopped there, so the chat model and the other plugins never see a
bare link they cannot make sense of.
"""

import asyncio
import io
import json
import re
import shutil
import time
import urllib.parse

import qrcode
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Json, Plain, Video
from astrbot.api.star import Context, Star, StarTools, register

from .bili import BiliClient, BiliError, TooLargeError

PLUGIN_NAME = "astrbot_plugin_bilibili_video_sender"

# Video pages, b23.tv / bili2233.cn short links, with or without "https://".
# The look-behind keeps "evilbilibili.com" or a path that merely contains
# "bilibili.com" from matching; the tail stops at the first character that
# cannot be in a URL, so Chinese text right after a link is not swallowed.
LINK_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._:/-])(?:https?://)?(?:"
    r"(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|[aA][vV]\d+)"
    r"|(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+"
    r")[A-Za-z0-9\-._~:/?#@!$&+,;=%]*"
)
VIDEO_ID_PATTERN = re.compile(r"/video/(?:(BV[0-9A-Za-z]{10})|[aA][vV](\d+))")
# "qn=116" anywhere in the message, for when QQ cut it off the link.
QN_TEXT_PATTERN = re.compile(r"\bqn\s*=\s*(\d+)", re.IGNORECASE)
QN_NAMES = {
    16: "360P 流畅",
    32: "480P 清晰",
    64: "720P 高清",
    74: "720P60 高帧率",
    80: "1080P 高清",
    100: "智能修复",
    112: "1080P+ 高码率",
    116: "1080P60 高帧率",
    120: "4K 超清",
}
# H.264 plays everywhere QQ runs; HEVC and AV1 only where the device can.
CODEC_RANK = {7: 0, 12: 1, 13: 2}


def stream_urls(stream: dict) -> list[str]:
    """Return a stream's main URL followed by its backup URLs."""
    main = stream.get("base_url") or stream.get("baseUrl") or stream.get("url")
    return [url for url in [main, *(stream.get("backup_url") or [])] if url]


@register(
    PLUGIN_NAME,
    "happycola233",
    "解析 B 站视频链接和小程序卡片，按清晰度和大小上限下载视频发到聊天里",
    "1.0.0",
)
class BilibiliVideoSender(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        # data/ is mounted into SnowLuma at the same path, so it can read
        # the downloaded files directly.
        self.download_dir = data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.bili = BiliClient(data_dir / "auth.json")
        self.jobs = asyncio.Semaphore(2)
        self.admins_warned = False

    async def terminate(self):
        await self.bili.close()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=200)
    async def on_message(self, event: AstrMessageEvent):
        """Send the video when a message carries a Bilibili video link.

        Runs before AngelHeart and QQ 消息增强 (priority 100). Only the
        sender's own text and cards are searched: quoted messages are separate
        components and merged forwards are not expanded yet, so links in them
        are left alone.

        Args:
            event: The incoming message event.
        """
        self_id = str(event.get_self_id())
        if str(event.get_sender_id()) == self_id:
            return
        chain = event.get_messages()
        texts = [comp.text for comp in chain if isinstance(comp, Plain)]
        if self.config.get("parse_card", True):
            texts += [
                json.dumps(comp.data, ensure_ascii=False)
                for comp in chain
                if isinstance(comp, Json)
            ]
        text = "\n".join(texts)
        match = LINK_PATTERN.search(text)
        if not match:
            return
        if (
            self.config.get("group_at_only", False)
            and not event.is_private_chat()
            and not any(isinstance(c, At) and str(c.qq) == self_id for c in chain)
        ):
            return

        link = match.group(0).rstrip(".,;:!?")
        if not link.startswith("http"):
            link = "https://" + link
        try:
            target = await self.bili.resolve_short_link(link)
        except BiliError as e:
            logger.warning(f"Could not open Bilibili short link {link}: {e}")
            return
        video_id = VIDEO_ID_PATTERN.search(target)
        if not video_id:
            return  # A short link to a live room, an article and so on.
        if self.config.get("block_message", True):
            event.stop_event()

        # A qn or p typed on the short link itself wins over the target's.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        query |= urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        qn = (query.get("qn") or [""])[0]
        if not qn and (qn_match := QN_TEXT_PATTERN.search(text)):
            qn = qn_match.group(1)
        page = (query.get("p") or [""])[0]
        try:
            await self._send_video(
                event,
                video_id.group(1) or "",
                video_id.group(2) or "",
                int(page) if page.isdigit() else 1,
                int(qn) if qn.isdigit() else 0,
            )
        except BiliError as e:
            await self._reply(event, f"B 站视频没发出来：{e}")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send a Bilibili video")
            await self._reply(event, "B 站视频没发出来，出错了，详情见日志。")

    async def _send_video(
        self,
        event: AstrMessageEvent,
        bvid: str,
        aid: str,
        page: int,
        requested_qn: int,
    ) -> None:
        """Pick the streams, download them, merge and send the video.

        Args:
            event: The message that carried the link.
            bvid: The BV id, or empty when the link used an av id.
            aid: The av id, or empty.
            page: The part to send, starting at 1; out of range means part 1.
            requested_qn: Quality asked for on the link, 0 when not given.

        Raises:
            BiliError: A Bilibili request, the download or the merge failed.
        """
        problem = await self.bili.refresh_if_due()
        if problem:
            await self._warn_admins(event, problem)
        await self.bili.ensure_buvid()

        view = await self.bili.get_view(bvid, aid)
        pages = view.get("pages") or []
        if not pages:
            raise BiliError("这个视频没有能播放的分 P")
        part = pages[page - 1] if 0 < page <= len(pages) else pages[0]
        title = view.get("title", "")
        if len(pages) > 1:
            title = f"{title} P{part.get('page')} {part.get('part', '')}".rstrip()
        duration = int(part.get("duration") or view.get("duration") or 0)
        max_minutes = self.config.get("max_duration_minutes", 10)
        if max_minutes and duration > max_minutes * 60:
            await self._reply(
                event,
                f"视频时长 {duration // 60} 分 {duration % 60} 秒，"
                f"超过了 {max_minutes} 分钟的上限，不下载了。",
            )
            return

        target_qn = requested_qn or int(self.config.get("qn", "80"))
        play = await self.bili.get_play(view["aid"], part["cid"])
        plays = [play]
        if not self.bili.logged_in and target_qn >= 64:
            # Guests get 480P at most over DASH but 720P as a plain MP4.
            try:
                plays.append(
                    await self.bili.get_play(view["aid"], part["cid"], mp4=True)
                )
            except BiliError as e:
                logger.warning(f"Could not get the guest 720P MP4: {e}")
        names = {
            fmt.get("quality"): fmt.get("new_description")
            for p in plays
            for fmt in p.get("support_formats") or []
        }

        def name(qn: int) -> str:
            return names.get(qn) or QN_NAMES.get(qn) or f"qn={qn}"

        # Every quality the account can get, best first, as (qn, video URLs,
        # audio URLs, estimated bytes, needs merging). DASH streams carry
        # their average bitrate, close enough to choose before downloading.
        options: list[tuple[int, list[str], list[str], int, bool]] = []
        for p in plays:
            dash = p.get("dash")
            if dash:
                seconds = dash.get("duration") or duration
                audios = dash.get("audio") or []
                audio = max(audios, key=lambda a: a.get("bandwidth", 0), default={})
                best_by_qn: dict[int, dict] = {}
                for stream in dash.get("video") or []:
                    current = best_by_qn.get(stream["id"])
                    if current is None or CODEC_RANK.get(
                        stream.get("codecid"), 9
                    ) < CODEC_RANK.get(current.get("codecid"), 9):
                        best_by_qn[stream["id"]] = stream
                for qn, stream in best_by_qn.items():
                    bandwidth = stream.get("bandwidth", 0) + audio.get("bandwidth", 0)
                    options.append(
                        (
                            qn,
                            stream_urls(stream),
                            stream_urls(audio),
                            bandwidth * seconds // 8,
                            True,
                        )
                    )
            else:
                # A plain MP4 has the sound in it and comes in one piece nowadays.
                for item in (p.get("durl") or [])[:1]:
                    options.append(
                        (
                            p.get("quality", 0),
                            stream_urls(item),
                            [],
                            item.get("size", 0),
                            False,
                        )
                    )
        if not options:
            raise BiliError("拿不到播放地址")
        options.sort(key=lambda opt: opt[0], reverse=True)

        allowed = [opt for opt in options if opt[0] <= target_qn] or options[-1:]
        limit_mb = self.config.get("max_size_mb", 500)
        limit = limit_mb * 1024 * 1024
        fitting = [opt for opt in allowed if opt[3] <= limit]

        notes = []
        if requested_qn and allowed[0][0] < requested_qn:
            if requested_qn not in (play.get("accept_quality") or []):
                reason = "视频没有这个清晰度"
            elif requested_qn > 80:
                reason = "需要大会员"
            else:
                reason = "需要登录 B 站"
            notes.append(
                f"拿不到 {name(requested_qn)}（{reason}），改用 {name(allowed[0][0])}。"
            )
        if not fitting:
            lowest = allowed[-1]
            notes.append(
                f"视频太大了：最低的 {name(lowest[0])} 也要约 {lowest[3] / 1048576:.0f} MB，"
                f"超过了 {limit_mb} MB 的上限，不下载了。"
            )
            await self._reply(event, "\n".join(notes))
            return
        if fitting[0][0] < allowed[0][0]:
            notes.append(
                f"视频比较大，清晰度从 {name(allowed[0][0])} 降到了 {name(fitting[0][0])}"
                f"（约 {fitting[0][3] / 1048576:.0f} MB，上限 {limit_mb} MB）。"
            )

        mode = self.config.get("notify_mode", "emoji")
        reacted = False
        if mode == "emoji" and not event.is_private_chat():
            reacted = await self._react(event)
        if mode == "text" or (mode == "emoji" and not reacted):
            notes.insert(0, f"正在下载：{title}（{name(fitting[0][0])}）")
        if notes:
            await self._reply(event, "\n".join(notes))

        async with self.jobs:
            # Files left by failed or timed-out sends.
            for old in self.download_dir.iterdir():
                if time.time() - old.stat().st_mtime > 3600:
                    old.unlink(missing_ok=True)
            base = f"{view.get('bvid', aid)}_{part['cid']}_{time.time_ns()}"
            video_path = self.download_dir / f"{base}.video.m4s"
            audio_path = self.download_dir / f"{base}.audio.m4s"
            out_path = self.download_dir / f"{base}.mp4"
            keep_output = False
            try:
                # After the break, qn, audio_urls and merge describe the
                # option that was downloaded.
                for index, (qn, video_urls, audio_urls, _, merge) in enumerate(fitting):
                    if index:
                        await self._reply(
                            event, f"实际文件比估计的大，清晰度又降到了 {name(qn)}。"
                        )
                    try:
                        used = 0
                        if audio_urls:
                            used = await self.bili.download(
                                audio_urls, audio_path, limit
                            )
                        await self.bili.download(
                            video_urls, video_path if merge else out_path, limit - used
                        )
                        break
                    except TooLargeError:
                        continue
                else:
                    await self._reply(
                        event,
                        f"视频太大了，每种清晰度都超过了 {limit_mb} MB 的上限，不发了。",
                    )
                    return

                if merge:
                    ffmpeg = shutil.which("ffmpeg")
                    if not ffmpeg:
                        raise BiliError("服务器上没有 ffmpeg，没法把画面和声音合在一起")
                    inputs = ["-i", str(video_path)]
                    if audio_urls:
                        inputs += ["-i", str(audio_path)]
                    proc = await asyncio.create_subprocess_exec(
                        ffmpeg, "-y", "-loglevel", "error", *inputs,
                        "-c", "copy", "-movflags", "+faststart", str(out_path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )  # fmt: skip
                    _, stderr = await proc.communicate()
                    if proc.returncode:
                        message = stderr.decode(errors="replace").strip()[-200:]
                        raise BiliError(f"合成视频失败（{message}）")
                    video_path.unlink(missing_ok=True)
                    audio_path.unlink(missing_ok=True)

                try:
                    await event.send(
                        MessageChain(chain=[Video.fromFileSystem(str(out_path))])
                    )
                except Exception as e:
                    if "timeout" not in str(e).lower():
                        raise BiliError(f"发送失败（{e}）") from e
                    # SnowLuma may still be uploading it, so the file is
                    # left for the hourly sweep instead of being deleted.
                    keep_output = True
                    await self._reply(event, "视频还在上传，可能要过一会儿才出来。")
            finally:
                video_path.unlink(missing_ok=True)
                audio_path.unlink(missing_ok=True)
                if not keep_output:
                    out_path.unlink(missing_ok=True)

    async def _react(self, event: AstrMessageEvent) -> bool:
        """Put an emoji reaction on the message. Returns whether it worked."""
        if event.get_platform_name() != "aiocqhttp":
            return False
        message_id = str(event.message_obj.message_id)
        try:
            await event.bot.call_action(
                "set_msg_emoji_like",
                message_id=int(message_id)
                if message_id.lstrip("-").isdigit()
                else message_id,
                emoji_id=str(self.config.get("reaction_emoji_id", 76)),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to react to the message: {e}")
            return False

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain().message(text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to send a message: {e}")

    async def _warn_admins(self, event: AstrMessageEvent, text: str) -> None:
        """Tell the AstrBot admins in private chat, once until the next login."""
        logger.warning(text)
        if self.admins_warned:
            return
        self.admins_warned = True
        for admin in self.context.get_config().get("admins_id", []):
            if not str(admin).isdigit():
                continue
            try:
                await self.context.send_message(
                    f"{event.get_platform_id()}:FriendMessage:{admin}",
                    MessageChain().message(text),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Failed to tell admin {admin}: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("b站登录", alias={"B站登录"})
    async def login(self, event: AstrMessageEvent):
        """Log in to Bilibili with a QR code. Admins only, in private chat."""
        event.stop_event()
        if not event.is_private_chat():
            await self._reply(event, "登录二维码不能发在群里，请私聊我发 /b站登录。")
            return
        try:
            url, key = await self.bili.qr_generate()
            buffer = io.BytesIO()
            qrcode.make(url).save(buffer, format="PNG")
            await event.send(
                MessageChain(
                    chain=[
                        Image.fromBytes(buffer.getvalue()),
                        Plain("用 B 站 App 扫这个码登录，3 分钟内有效。"),
                    ]
                )
            )
            scanned = False
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                code = await self.bili.qr_poll(key)
                if code == 0:
                    break
                if code == 86038:
                    deadline = 0
                elif code == 86090 and not scanned:
                    scanned = True
                    await self._reply(event, "扫到了，请在手机上点确认登录。")
            else:
                await self._reply(event, "二维码过期了，重新发 /b站登录。")
                return
            await self.bili.ensure_buvid()
            nav = await self.bili.get_nav()
        except BiliError as e:
            await self._reply(event, f"登录失败：{e}")
            return
        self.admins_warned = False
        vip = "大会员" if nav.get("vipStatus") else "普通账号，最高 1080P"
        await self._reply(event, f"登录成功：{nav.get('uname', '')}（{vip}）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("b站状态", alias={"B站状态"})
    async def status(self, event: AstrMessageEvent):
        """Show which Bilibili account the plugin uses."""
        event.stop_event()
        if not self.bili.logged_in:
            await self._reply(
                event,
                "没有登录 B 站，按游客下载（最高 720P）。私聊我发 /b站登录 扫码登录。",
            )
            return
        try:
            nav = await self.bili.get_nav()
        except BiliError as e:
            await self._reply(event, f"查不到登录状态：{e}")
            return
        if not nav.get("isLogin"):
            await self._reply(event, "B 站登录已失效，私聊我发 /b站登录 重新扫码。")
            return
        vip = "大会员" if nav.get("vipStatus") else "普通账号，最高 1080P"
        await self._reply(event, f"已登录：{nav.get('uname', '')}（{vip}）。")
