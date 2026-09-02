# Douyin Claw MCP

一个面向本机运行的抖音 MCP 服务。它把 `DouYin_Spider` 的采集能力封装成 MCP 工具，并提供本地网页控制台，用于查看运行状态、抓取结果、评论、直播事件和私信监听状态。

项目当前以 Windows 为主要使用环境，推荐通过 `start_mcp.bat` 启动。MCP 使用 `stdio` 传输，不需要把服务暴露到公网。

## 功能

- 登录管理：扫码登录、登录会话状态查询、认证状态查询、退出登录
- 内容采集：视频/图集详情、用户信息、用户作品、视频搜索、用户搜索、直播间搜索
- 互动数据：评论及回复、收藏列表、推荐流
- 文件能力：下载视频/图集，截取视频指定时间点的画面
- 直播能力：查询直播间信息，启动/轮询/停止直播事件监听
- 私信能力：启动/轮询/停止私信监听
- 账号操作：点赞、收藏、发表评论、发送直播消息、发送私信
- 本地控制台：展示运行状态、工具调用、统计数据和已保存结果

搜索视频和获取评论对相同请求提供 5 分钟进程内缓存；结果和统计会写入本地文件，控制台可以持续读取这些数据。涉及点赞、收藏、评论和消息发送的工具要求显式传入 `confirm=true`。

## 环境要求

- Windows
- Python 3.10+（建议使用 Python 3.12）
- 网络连接和一个已登录的抖音账号
- Playwright Chromium；安装脚本会自动安装

Node.js 18+ 仅在需要构建 `DouYin_Spider` 的 Electron 桌面版时使用，运行 MCP 本身不要求 Node.js。

## 安装

最简单的方式是在项目目录双击：

```text
install.bat
```

安装脚本会创建 `.venv`，安装根目录 `requirements.txt`，并安装 Playwright Chromium。也可以手动执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 启动 MCP 和控制台

推荐双击：

```text
start_mcp.bat
```

脚本会选择可用的 Python 并启动 MCP。MCP 客户端实际调用工具后，控制台会自动记录调用状态和结果。控制台地址为：

```text
http://127.0.0.1:8765/
```

如果只需要启动网页控制台，可双击 `start_dashboard.bat`，然后在控制台中启动 MCP。也可以直接运行：

```powershell
.\.venv\Scripts\python.exe .\mcp_server.py
```

如果项目移动到了其他位置，请同步修改 `mcp_config.example.json` 和 `workbuddy_mcp.example.json` 中的绝对路径。

## 配置 MCP 客户端

复制并修改 [mcp_config.example.json](./mcp_config.example.json)，把其中的 `D:\\Desktop\\douyin_claw` 替换为实际项目路径，再将 `mcpServers.douyin-spider` 添加到 MCP 客户端配置中。

示例配置使用 Windows 的 `cmd.exe` 调用 `start_mcp.bat`，并将统计文件固定到项目的 `work/mcp_stats.json`。服务默认使用 `stdio`，不要把它改成公网 HTTP 服务。

## 首次登录

启动 MCP 后，在 MCP 客户端依次调用：

1. `douyin_login`：打开可见浏览器，扫码或完成登录。
2. `douyin_login_status`：查询本次登录会话是否完成。
3. `douyin_auth_status`：确认普通抖音和直播登录状态。

登录信息保存在本机 `DouYin_Spider/.env` 中。Cookie 不会作为工具结果返回，也不应提交到 Git 或发送给其他人。退出登录可调用 `douyin_logout`。

## 工具速查

| 类别 | 工具 |
| --- | --- |
| 登录 | `douyin_login`、`douyin_login_status`、`douyin_auth_status`、`douyin_logout` |
| 视频/用户 | `get_work_info`、`get_user_info`、`get_user_works`、`search_videos`、`search_users` |
| 互动数据 | `get_comments`、`get_favorite_list`、`get_feed`、`download_work` |
| 视频画面 | `capture_work_frame`、`capture_video_frame` |
| 直播 | `search_live_rooms`、`get_live_info`、`start_live_monitor`、`poll_live_events`、`stop_live_monitor` |
| 私信 | `start_private_message_monitor`、`poll_private_messages`、`stop_private_message_monitor` |
| 账号操作 | `like_work`、`collect_work`、`publish_comment`、`send_live_message`、`send_private_message` |

监听采用“启动 / 轮询 / 停止”模式，避免单个 MCP 调用长期阻塞。`capture_video_frame` 适合接在 `search_videos` 后使用；`capture_work_frame` 则直接接收作品链接。截图需要 FFmpeg，程序会优先查找项目内 `.browser` 的随包版本，也可以通过 `FFMPEG_PATH` 指定本机 FFmpeg。

## 项目结构

```text
.
├── mcp_server.py                  # MCP stdio 服务和工具定义
├── mcp_auth.py                    # 本地扫码登录及 Cookie 管理
├── mcp_stats.py                   # 统计、日志和结果记录
├── dashboard_server.py            # 本地网页控制台服务
├── dashboard.html                 # 控制台页面
├── live_monitor.py                # 直播事件监听适配
├── private_monitor.py             # 私信 WebSocket 监听适配
├── install.bat                    # 安装依赖
├── start_mcp.bat                  # 启动 MCP
├── start_dashboard.bat            # 只启动控制台
├── mcp_config.example.json         # MCP 客户端配置示例
└── DouYin_Spider/                 # 原始抖音 API、签名、直播和 Electron 代码
```

运行时数据默认位于 `work/`、`outputs/` 和 `DouYin_Spider/datas/`，这些目录已加入 `.gitignore`。

## Electron 桌面版（可选）

`DouYin_Spider` 内含 Electron 桌面端代码。需要 Node.js 18+ 时，在该目录执行：

```powershell
cd .\DouYin_Spider
npm install
npm start
```

构建 Windows 安装包：

```powershell
npm run dist
```

安装包会输出到 `DouYin_Spider/release/`，该目录不纳入版本控制。

## 安全与合规

- 仅在本机使用，建议保持 `stdio` 传输，不要暴露公网端口。
- 仅使用本人账号或已获授权的数据，并遵守抖音平台规则及适用法律。
- 不要提交 `.env`、Cookie、登录文件、运行结果或下载的媒体文件。
- 抓取和互动接口可能受抖音页面、接口和风控策略变化影响；遇到失败时请先确认登录状态和网络环境。

## 致谢

采集核心代码位于 [`DouYin_Spider`](./DouYin_Spider/)，其原始项目说明和许可证信息请参阅该目录下的 [README.md](./DouYin_Spider/README.md) 与 `package.json`。
