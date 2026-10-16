# Dola Pool · AI 视频生成池化平台

把一个散落在服务器上的 Dola 视频生成脚本，整理成了一个**可以直接运行的程序**：
单进程 FastAPI 服务 + 内置 Web 控制台，覆盖「账号池 → 任务队列 → 视频生成 → 成片管理」的完整闭环。

```
浏览器控制台 ──► FastAPI ──► 任务队列（SQLite + 线程池）──► 生成器
                                            ├── gen/dola_gen_clean.py（真实调用 Dola）
                                            └── 演示渲染（DOLA_DEMO=1，无外网也可跑通）
```

## 两种用法

| 用法 | 启动方式 | 适合 |
| --- | --- | --- |
| **桌面应用**（推荐个人使用） | `python desktop.py` 或双击打包好的程序 | 只想在自己电脑上用，不折腾浏览器和端口 |
| **Web 服务** | `python app.py` | 部署到服务器，多人/多设备访问 |

两者共用同一套后端与控制台，功能完全一致。

---

## 桌面应用

```bash
pip install -r requirements-desktop.txt
python desktop.py            # 默认演示模式，双击即可用
python desktop.py --no-demo  # 生产模式：调用真实 Dola 生成
```

窗口用系统自带浏览器内核渲染（Windows WebView2 / macOS WKWebView / Linux WebKitGTK），
后端只监听 `127.0.0.1` 的随机空闲端口，**不对外暴露**。

- 首次运行的用户数据在：Windows `%APPDATA%\DolaPool`、macOS `~/Library/Application Support/DolaPool`、Linux `~/.local/share/dola-pool`
- 界面「设置 → 桌面应用」里可以在**演示 / 生产模式之间一键切换**（写入 `config.json` 后应用自动重启）
- 成片播放弹窗里可以直接「📂 打开所在文件夹」
- 关掉窗口即退出，后台不会残留服务

命令行参数：

| 参数 | 说明 |
| --- | --- |
| `--demo` / `--no-demo` | 强制演示 / 生产模式（不写则用上次保存的选择） |
| `--port 8123` | 指定端口（默认自动选空闲端口） |
| `--browser` | 不开原生窗口，用系统默认浏览器打开 |
| `--headless` | 只起服务不弹窗（调试用） |
| `--width/--height` | 初始窗口尺寸 |

系统依赖：Windows / macOS 开箱即用；Linux 需要 `sudo apt install python3-gi gir1.2-webkit2-4.1`，
或改用 Qt 后端 `pip install "pywebview[qt]"`。缺 GUI 库时会打印安装提示并自动降级到浏览器模式。

### 装不上依赖怎么办（国内网络常见）

`uvicorn[standard]` 会拉入 pyyaml 等需要编译的包，在国内网络下容易失败。本项目已改用
**纯 uvicorn**（用不到 YAML 日志与 uvloop），并且三个平台的启动器都会自动依次尝试
官方源 → 清华镜像 → 阿里云镜像。手动安装时也可以直接指定镜像：

```bash
pip install -r requirements-desktop.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# 永久换源：
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

若提示 `free-threaded`（自由线程版 Python）导致缺预编译包，请改用普通版 Python 3.12。

### 打包成免安装程序

```bash
pip install -r requirements-desktop.txt pyinstaller
python tools/build_desktop.py            # 单目录版，启动快（推荐）
python tools/build_desktop.py --onefile  # 单文件版，便于分发
```

产物在 `dist/DolaPool/`（Windows 为 `DolaPool.exe`，macOS 为 `DolaPool.app`）。

> `dist/` 是**运行打包命令后现场生成**的输出目录，不在仓库里（`.gitignore` 已排除
> `build/`、`dist/`、`*.spec`）。仓库里只有打包脚本本身 `tools/build_desktop.py`。

PyInstaller 不能跨平台打包，请在目标系统上执行；若不想在本地装环境，可以用仓库自带的
云端工作流：推到 GitHub 后打开 **Actions → Build Desktop App → Run workflow**，
跑完在 Artifacts 里下载 `DolaPool-Windows`，解压即得 `DolaPool.exe`
（打 `v*` tag 推送时还会自动创建 Release 挂上三平台产物）。

自检：
- `python tools/desktop_test.py` —— 桌面版端到端（启动 / 模式切换重启 / 退出 / 路径校验）
- `python tools/check_launcher.py` —— 批处理启动器检查，含一个小型批处理解释器，
  会在多种环境（无 Python / 官方源失败 / 缺 WebView / 窗口崩溃…）下模拟执行，
  确认任何路径都不会出现「窗口一闪即关」

---

## 30 秒跑起来

```bash
# 1. 依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 演示模式启动（不访问外网，任务用内置示例成片）
DOLA_DEMO=1 python app.py
# 打开 http://127.0.0.1:8000
```

演示模式下可以完整体验：提交任务 → 排队 → 逐帧进度 → 出片 → 播放 / 下载 / 重试 /
统计 / 分镜工作台 / 节点画布 / 资产库 / 日志，全部可用。

```bash
# 3. 真实生成：关闭演示模式，导入 Dola 账号 Cookie
DOLA_DEMO=0 DOLA_ADMIN_TOKEN=你的令牌 python app.py
# 控制台「账号池 → 批量导入」粘贴 Cookie（每行一个），然后到「生成」页提任务
```

Docker 方式：

```bash
cp .env.example .env      # 按需修改
docker compose up -d --build
```

---

## 功能一览

| 模块 | 说明 |
| --- | --- |
| **生成** | 单条 / 整行批量提交；模型（SD2.0 / SD2.5）、时长（5/10/15/30s）、比例（9:16 / 16:9 / 1:1）、画质、参考图（最多 9 张）、指定账号或自动选号 |
| **任务队列** | SQLite 持久化，并发可热调整（1-100）；实时进度、断线重连后自动恢复未完成任务、失败一键重试、完成后在线播放 / 下载 |
| **分镜工作台** | 粘贴文案 → AI 推理拆镜（Agnes AI；未配置 Key 时自动降级为本地规则模板）→ 逐镜编辑提示词 → 一键全量提交 → 卡片内跟踪进度与成片 |
| **账号池** | Cookie 批量导入（txt / csv / json）、格式与接口双重验活、每日限额、同号单任务、按今日用量最少自动轮询 |
| **代理与 IP** | 出口 IP 连通性检测；真实生成时由生成器按任务自动轮换住宅 IP（同任务同 IP，粘性 50 分钟）；可一键刷新代理 |
| **资产库** | 角色 / 场景 / 道具参考图与特征描述，便于复用（图片走 `/uploads` 静态服务） |
| **节点画布** | 可视化节点编排工作流，可保存到服务端（`/api/workflows`）并可加载回来，画布内可直接建任务 |
| **API 密钥** | 生成外部调用用密钥（列表脱敏，创建后仅显示一次），配套 curl 示例 |
| **设置** | 并发数、每号每日上限、成片最小体积、AI Key / 接口 / 模型、运行信息（数据目录、数据库、版本） |
| **日志** | 错误日志与审计日志（谁在什么时候创建了任务 / 改了设置），可一键清空 |
| **运维** | `cleanup.sh` 定期清理老成片与老日志；`/api/health` 健康检查；Dockerfile 内置 HEALTHCHECK |

界面为深色毛玻璃风，响应式：桌面端左侧导航、移动端顶部抽屉，背景图按小时自动轮播（15 张）。

---

## 配置

全部通过环境变量（见 `.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOLA_DEMO` | `0` | `1` = 演示模式，不访问外网，任务用内置示例成片 |
| `DOLA_HOST_BIND` / `DOLA_PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `DOLA_DESKTOP` | 桌面版自动设为 `1` | 启用 `/api/desktop/*`（模式切换 / 打开目录 / 退出） |
| `DOLA_ADMIN_TOKEN` | 空 | 设置后控制台与 API 需携带令牌（登录页 / 请求头 `X-Admin-Token` / `?token=`） |
| `DOLA_DATA_DIR` | `./data` | 数据根目录，内含 `dola.db`、`output/`、`uploads/` |
| `DOLA_OUTPUT_DIR` / `DOLA_UPLOAD_DIR` / `DOLA_DB` | 派生自上面 | 可分别覆盖 |
| `VIDEO_MAX_CONCURRENCY` | `6` | 并发任务数（控制台里可再热调，1-100） |
| `MAX_DAILY_PER_ACCOUNT` | `20` | 每个账号每天最多提交多少条 |
| `VIDEO_PROVIDER_URL` / `VIDEO_PROVIDER_TOKEN` | 空 | 改为调用外部视频供应商接口，而不是本机生成器 |
| `DOLA_PROXY` / `DOLA_PROXY_REGION` | 空 | 统一出口代理（真实生成时生成器还会自行轮换住宅 IP） |
| `DOLA_PROXY_REFRESH_URL` | 空 | 配置后「刷新代理 IP」会执行 `scripts/refresh-proxy.sh` |
| `AGNES_API_KEY` / `AGNES_BASE_URL` / `AGNES_MODEL` | 空 | AI 分镜推理（任意 OpenAI 兼容 `/chat/completions`）；留空则用本地规则 |
| `DOLA_ACCOUNT_CHECK_URL` | Dola 账号信息接口 | 账号「验活」调用的地址 |

设置页里修改的项会写入 SQLite，重启后仍然生效。

---

## API

```bash
# 提交任务（单条）
curl -X POST http://127.0.0.1:8000/api/tasks \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $DOLA_ADMIN_TOKEN" \
  -d '{"prompt":"9:16 竖屏，雨夜霓虹街道，慢动作水花","duration":15,"ratio":"9:16","quality":"720p"}'

# 批量（单次最多 500 条）
curl -X POST http://127.0.0.1:8000/api/tasks -H "Content-Type: application/json" \
  -d '{"tasks":[{"prompt":"镜头一…","duration":10},{"prompt":"镜头二…","duration":30}],"ratio":"9:16"}'
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（版本、模式、生成器状态） |
| GET | `/api/stats` | 控制台统计 |
| GET | `/api/queue` | 队列深度 / 并发 / 各状态数量 |
| POST/GET | `/api/tasks` | 创建（单个或批量）/ 列表（`?status=&q=&limit=`） |
| GET | `/api/tasks/{id}` | 任务详情（含 `video_url`） |
| POST | `/api/tasks/{id}/retry` · `/api/tasks/retry-failed` | 重试单个 / 全部失败任务 |
| DELETE | `/api/tasks/{id}` | 删除任务（连同成片文件） |
| GET/POST/DELETE | `/api/accounts` · `/api/accounts/{id}/check` · `/api/accounts/check-all` · `/api/import` | 账号池 |
| GET/POST/DELETE | `/api/proxies` · `/api/proxies/refresh` · `/api/proxy-status` | 代理 |
| GET/POST/DELETE | `/api/assets` · `/api/upload-image` | 资产库与图片上传 |
| POST | `/api/ai-reason` | 分镜推理 `{script, style, duration, image_url}` → `{scenes, engine}` |
| GET/POST/DELETE | `/api/workflows` | 节点画布工作流（服务端保存） |
| GET/POST | `/api/settings` | 读写设置 |
| GET/DELETE | `/api/errors` · `/api/audits` · `/api/keys` | 日志与密钥 |
| GET/POST | `/api/desktop/config` · `/api/desktop/mode` · `/api/desktop/quit` · `/api/desktop/reveal` | 桌面版专用（Web 版返回「仅桌面版可用」） |

交互式文档：`/docs`（FastAPI 自带）。

---

## 项目结构

```
dola-pool/
├── app.py                     # 主程序：API + 任务队列 + 账号池 + 控制台路由
├── desktop.py                 # 桌面版启动器（pywebview 原生窗口 + 内置后端）
├── gen/
│   └── dola_gen_clean.py      # 视频生成核心（提交 / 轮询 / 去水印下载 / IP 轮换）
├── templates/
│   ├── index.html             # 控制台单页（11 个页面，深色毛玻璃 + 响应式）
│   └── node-canvas.html       # 节点画布
├── static/
│   ├── bg_images/bg_01..15.jpg  # 控制台背景（按小时轮换）
│   └── demo/sample_*.mp4        # 演示模式用的示例成片
├── assets_src/                # 背景图源图（可用自己的图片替换后重建）
├── tools/make_assets.py       # 重建背景图 / 演示视频
├── tools/smoke_test.py        # 端到端自检（只依赖标准库）
├── tools/desktop_test.py      # 桌面版无头自检
├── tools/build_desktop.py     # 打包成免安装桌面程序（PyInstaller）
├── requirements-desktop.txt   # 桌面版依赖
├── scripts/refresh-proxy.sh   # 代理刷新（cron 友好）
├── cleanup.sh                 # 成片与日志清理（建议每 5 小时一次）
├── data/                      # 运行时生成：dola.db、output/、uploads/（已 gitignore）
├── requirements.txt · Dockerfile · docker-compose.yml · .env.example
```

自检（服务启动后执行，演示模式下约 10 秒跑完 26 项）：

```bash
python tools/smoke_test.py
# DOLA_BASE=http://127.0.0.1:9000 DOLA_ADMIN_TOKEN=xxx python tools/smoke_test.py
```

重建静态资源（可选）：

```bash
pip install pillow imageio-ffmpeg
python tools/make_assets.py        # 由 assets_src/ 生成 15 张背景图与 2 个演示视频
```

---

## 部署建议

```ini
# /etc/systemd/system/dola-pool.service
[Unit]
Description=Dola Pool
After=network.target

[Service]
WorkingDirectory=/opt/dola-pool
Environment=DOLA_DEMO=0
Environment=DOLA_ADMIN_TOKEN=请改成随机长字符串
Environment=VIDEO_MAX_CONCURRENCY=6
ExecStart=/opt/dola-pool/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```cron
# 每 5 小时清理一次老成片
0 */5 * * * DOLA_DATA_DIR=/opt/dola-pool/data /opt/dola-pool/cleanup.sh
# 每 50 分钟刷新一次代理 IP（与住宅 IP 粘性时长对齐）
*/50 * * * * DOLA_PROXY_REFRESH_URL='http://你的代理/update?...' /opt/dola-pool/scripts/refresh-proxy.sh
```

对外提供服务时建议放在 Nginx 后面并开启 HTTPS + 鉴权：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_read_timeout 300s;
}
```

---

## 安全与合规

- **务必设置 `DOLA_ADMIN_TOKEN`**：否则任何能访问该端口的人都可以读取账号池、提交任务、下载成片。
- `data/dola.db` 中保存着账号 Cookie（明文，与平台登录态等价），请限制文件权限（`chmod 600`），不要提交到 Git（`.gitignore` 已排除 `data/`）。
- 本仓库的生成器会调用账号 Cookie 并去除视频水印；这类操作很可能**违反 Dola 的服务条款**，也可能影响账号安全。请只在你有权操作的账号上使用，并自行承担风险。
- 项目仅供学习与技术研究，请勿用于侵权、刷量、转售等用途；生成内容的版权与合规责任由使用者承担。

## 许可证

MIT License
