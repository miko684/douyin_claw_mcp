# 抖音内容抓取 MCP（小白版）

这是一个自包含的本地 `stdio MCP`。原始采集代码已经放在本目录的 `DouYin_Spider` 中，不需要再配置或寻找其他项目。

## 安装

最简单的方式是双击本目录中的 `install.bat`。它会优先使用系统 Python 创建本地环境；如果系统没有可用 Python，就使用本目录内置的 `.runtime`。随后自动安装依赖和浏览器组件。浏览器组件也会安装到本目录的 `.browser`，不会依赖用户另外配置。

如果需要手动安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

配置示例见 [mcp_config.example.json](./mcp_config.example.json)。它通过 `start_mcp.bat` 启动，因此 Codex 不需要知道用户是否安装了 Python。如果把整个文件夹移动到了其他位置，只需把配置中的 `D:\\Desktop\\douyin_claw` 替换成新的实际路径。

## 浏览器控制台（推荐小白使用）

推荐直接双击 `start_mcp.bat`。它会自动选择 Python、启动 MCP 和网页控制台，但不会强制唤起 Windows 外部浏览器。请在 Codex 内置浏览器中访问：

<http://127.0.0.1:8765/>

如果确实需要临时调用系统默认浏览器，可在启动前设置 `DOUYIN_OPEN_DASHBOARD=1`；一次启动只会尝试打开一次。

也可以在本目录打开 PowerShell 执行：

```powershell
.\.venv\Scripts\python.exe .\mcp_server.py
```

如果使用网页上的“启动 MCP”按钮，则先双击 `start_dashboard.bat`，再打开上述地址。网页不提供 Cookie 或业务输入框，只显示运行状态、视频、评论、直播间和弹幕统计。

注意：网页启动的是 MCP 服务本身，统计数字会在 MCP 客户端实际调用抓取/监听工具后增长。网页不会凭空决定要抓哪个视频或直播间。

总览页顶部的“一键清空”会清除所有采集结果 JSON、统计数字和结果记录；每个统计卡片以及对应详情页也提供板块清空。清空操作会先确认，并且不会删除登录 Cookie、原始采集代码或运行环境。

## 小白登录

1. 在 Codex 中调用 `douyin_login`。
2. 等待浏览器自动打开。
3. 在浏览器中扫码或完成登录。
4. 调用 `douyin_login_status` 查询结果。
5. 调用 `douyin_auth_status` 检查登录状态。

服务器会自动把登录状态保存在用户自己的本机，工具结果不会包含 Cookie 值。不要把登录文件或 Cookie 发给别人。

## 工具

- 登录：`douyin_login`、`douyin_login_status`、`douyin_auth_status`、`douyin_logout`
- 采集：`get_work_info`、`get_user_info`、`get_user_works`、`search_videos`、`search_users`、`search_live_rooms`、`get_comments`、`get_favorite_list`、`get_feed`

`search_videos` 和 `get_comments` 对完全相同的请求会在当前 MCP 进程内缓存 5 分钟，避免重复抓取；需要强制刷新时传 `refresh=true`。控制台统计和结果文件也会按视频 ID、评论 ID 去重。

所有抓取类 MCP 工具都会先在控制台登记运行中的操作，成功后必须把结果写入 `work/mcp_stats.json` 和 `work/results` 才会向客户端返回成功。统计或结果文件写入失败时，MCP 调用会直接报错，避免出现“客户端拿到数据但控制台没有记录”的情况。视频封面下载失败只会记录警告并保留原始结果，不会阻止视频和评论数据落盘。

`refresh=true` 只表示跳过 5 分钟结果缓存并重新请求抖音；网页控制台本身每秒读取一次本地统计文件，不需要额外的网页刷新参数。抓取函数只允许通过 MCP 工具入口调用；直接在 Python 中调用抓取函数，或通过 `__wrapped__` 绕开 MCP 装饰器，会在发起网络请求前被拒绝，避免产生未记录的抓取结果。
- 下载：`download_work`
- 视频截图：`capture_work_frame`（传入视频链接和 `timestamp_seconds`，默认截取第 1 秒）
- 直链截图：`capture_video_frame`（传入 `search_videos` 返回的 `video_addr` 和 `timestamp_seconds`）
- 直播：`get_live_info`、`start_live_monitor`、`poll_live_events`、`stop_live_monitor`
- 私信接收：`start_private_message_monitor`、`poll_private_messages`、`stop_private_message_monitor`
- 账号操作：`like_work`、`collect_work`、`publish_comment`、`send_live_message`、`send_private_message`

所有会改变账号状态的工具都要求 `confirm=true`。

## 安全边界

- MCP 只在本机运行，建议使用 `stdio`，不要暴露公网 HTTP 端口。
- Cookie 只在本地认证管理器和抖音请求之间流转，不返回给模型，不写入日志。
- 监听使用“启动 / 轮询 / 停止”模式，避免 MCP 工具长期阻塞。
- 视频截图会优先使用本目录 `.browser` 中随包提供的 FFmpeg；如果该文件夹被删掉，也可以安装 FFmpeg 或通过 `FFMPEG_PATH` 指定可执行文件路径。
- 仅用于本人账号和已获授权的数据，并遵守平台规则和适用法律。
