# B 站视频发送

AstrBot 插件：有人在聊天里发 B 站视频链接或「分享到 QQ」的小程序卡片时，下载视频并发回聊天。为 aiocqhttp（OneBot v11）平台编写，在 SnowLuma 上测试。

由 MaiBot 插件 [bilibili_video_sender_plugin](https://github.com/happycola233/bilibili_video_sender_plugin) 重写而来。

## 功能

- **识别链接**：认得 `bilibili.com/video/BV…`、`av…`、`m.bilibili.com`、`b23.tv` 和 `bili2233.cn` 短链接，带不带 `https://` 都行。也认得手机 B 站分享出来的小程序卡片。
  - 只看发送者自己写的内容。引用的消息、合并转发里的链接不会触发。
- **分 P**：链接带 `?p=2` 时下第 2 P，不带时下第 1 P。
- **清晰度**：
  - 配置里选想要的最高清晰度。在链接后面加 `?qn=116`，可以单独指定这一条。
  - 视频没有这个清晰度、账号拿不到时，自动用低一档的。如果是链接里指定的清晰度，会说明原因。
  - 不登录最高 720P，登录后最高 1080P，1080P+、1080P60、4K 要大会员。
  - 同一清晰度有多种编码时，优先 H.264，兼容性最好。
- **大小上限**：
  - 下载前按码率估算大小，超过上限就降清晰度，并在聊天里说明从哪一档降到哪一档。
  - 最低一档也超过时不下载，回一句提示。
- **时长上限**：超过的视频不下载。
- **群聊只在 @ 时解析**：开关，私聊总是解析。
- **拦下消息**：开关，默认开启。带视频链接的这条消息到此为止，不再交给 AI 和其他插件，机器人不会对着一条链接莫名其妙地接话。
- **提示**：开始下载时给消息贴个表情，也可以改成发文字或不提示。私聊没法贴表情，会改成发文字。

## 登录 B 站

管理员私聊机器人发 `/b站登录`，机器人回一张二维码，用 B 站 App 扫码确认即可。

- 登录信息保存在 `data/plugin_data/astrbot_plugin_bilibili_video_sender/auth.json`。这个文件等同于账号登录凭据，不要发给别人。
- 插件每天检查一次是否需要续期，需要时按 B 站网页端的流程自动续期。
- 登录失效或续期失败时，私聊通知 AstrBot 管理员，并改按游客下载。
- `/b站状态` 查看当前登录的账号和是否大会员。
- `/b站退出` 退出登录：先调用 B 站网页端的退出接口，让这份登录在 B 站那边作废，再删掉保存的登录，改按游客下载。B 站没确认退出时（比如登录本来就失效了），本地的登录照样删掉，并提示去 B 站 App 的登录设备管理里把这台设备踢下线。

这三个指令只有 AstrBot 管理员能用，`/b站登录` 还必须私聊。登录信息不会因为重启 AstrBot、重建容器或更新插件而丢失；卸载插件时勾选删除插件数据才会删掉。

## 安装

在 AstrBot WebUI「插件」页用仓库地址安装：

```text
https://github.com/happycola233/astrbot_plugin_bilibili_video_sender
```

需要 ffmpeg 把画面和声音合成一个文件。AstrBot 的 Docker 镜像已经自带。

视频下载到 AstrBot 的 `data/plugin_data/astrbot_plugin_bilibili_video_sender/downloads/`，发出去的是这个文件的本地路径。协议端在另一个容器里时，要把 AstrBot 的 `data` 目录按同一路径挂进去（例如 `./data:/AstrBot/data:ro`）。

## 说明

- 不登录时，DASH 格式只给 480P，接口文档里的 `try_look` 参数已经不起作用。插件会另外请求一次 720P 的普通 MP4，所以游客也有 720P。
- 下载地址的线路顺序沿用 [BiliTools](https://github.com/happycola233/BiliTools)：B 站自家镜像优先，第三方 PCDN 节点放最后。
- 发送超时（协议端 180 秒没回应）时，视频可能还在上传，文件会保留一小时再删。
- 接口用法参考 [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)。

## 许可证

MIT，见 `LICENSE`。
