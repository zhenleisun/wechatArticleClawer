# wxmp_archiver

将微信公众号历史文章全量离线保存到本地（Markdown + HTML + 图片本地化），支持断点续跑与增量更新。

## 合规与风控声明

> **重要：请务必阅读**

1. 本工具仅用于**归档您自己有权访问的公众号内容**，不得用于侵犯他人版权或隐私。
2. 微信公众号页面及接口**可能存在频率限制、验证码、登录要求**，本工具不会也不应绕过任何安全验证。
3. **不得修改本工具以实现绕过验证码、破解登录等行为**。
4. 默认启用限速（每篇文章间隔 2–5 秒随机延迟）、失败重试（指数退避，最多 3 次）、友好报错。
5. 如遇到频控/验证码/登录拦截，工具会**自动停止并提示**，请手动完成验证后重试，或降低抓取频率。

## 环境要求

- macOS (Intel / Apple Silicon)
- Python 3.11+
- Chromium（由 Playwright 自动管理）
- **微信公众号账号**（个人订阅号即可，免费注册）— 用于扫码登录采集文章列表

## 安装

```bash
# 1. 克隆项目
cd /path/to/wxmp_archiver

# 2. 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -e .

# 4. 安装 Playwright 浏览器
playwright install chromium
```

## 快速开始

### 一键运行（推荐）

```bash
wxmp_archiver run \
  --history-url "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=<你的目标公众号biz>&scene=124#wechat_redirect" \
  --out ./out
```

运行后会：
1. **自动打开浏览器**，显示微信公众平台登录页的二维码
2. **用手机微信扫码**，在手机上确认登录
3. 登录成功后，工具自动通过平台 API 枚举目标公众号的全部文章列表
4. 逐篇打开文章页面，抓取正文并保存 Markdown/HTML + 图片

### 分步执行

#### Step 1: 采集文章链接

```bash
wxmp_archiver crawl-links \
  --history-url "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=<BIZ>&scene=124#wechat_redirect" \
  --out ./out \
  --max-pages 999
```

- 浏览器弹出 → 扫码登录 → 通过公众平台 API 枚举文章
- 链接去重、按发布时间排序，写入 `out/links.jsonl`

#### Step 2: 逐篇抓取文章

```bash
wxmp_archiver fetch \
  --links ./out/links.jsonl \
  --out ./out \
  --min-delay 2 \
  --max-delay 5 \
  --resume true
```

- 逐篇打开文章 URL，抓取标题、正文 HTML、作者、发布时间
- 保存 `article.md`（Markdown 主体）、`article.html`（完整 HTML 备份）、`meta.json`
- 图片下载到本地并重写链接为相对路径
- 自动跳过已完成的文章（断点续跑）

#### 重跑失败项

```bash
wxmp_archiver retry-failed --out ./out
```

## CLI 参数速查

| 命令 | 参数 | 默认值 | 说明 |
|------|------|--------|------|
| `crawl-links` | `--history-url` | (必填) | 公众号历史消息页 URL（需含 `__biz`） |
| | `--out` | `./out` | 输出目录 |
| | `--max-pages` | `999` | 最大翻页轮次 |
| | `--headless` | `false` | 无头模式 |
| | `--cookie` | `""` | (高级) Cookie 字符串/文件，跳过扫码直接用 profile_ext |
| `fetch` | `--links` | `./out/links.jsonl` | 链接文件路径 |
| | `--out` | `./out` | 输出目录 |
| | `--min-delay` | `30` | 请求间最小延迟（秒） |
| | `--max-delay` | `120` | 请求间最大延迟（秒） |
| | `--resume` | `true` | 跳过已完成文章 |
| | `--force` | `false` | 强制重新抓取 |
| | `--headless` | `true` | 无头模式 |
| | `--cookie` | `""` | (高级) Cookie 字符串或文件路径 |
| `run` | (合并 crawl-links + fetch 参数) | | |
| `retry-failed` | `--out` | `./out` | 输出目录 |

所有命令均支持 `--verbose / -v` 打开调试日志。

## 登录方式说明

### 默认方式：扫码登录（推荐）

不带 `--cookie` 时，工具会自动打开 `mp.weixin.qq.com` 登录页：

1. 浏览器弹出二维码
2. 用手机微信扫码 → 确认登录
3. 工具自动获取 session，通过平台内部 API 枚举文章

**前提**：你的微信需要绑定一个公众号（个人订阅号即可，[免费注册](https://mp.weixin.qq.com/cgi-bin/registermidpage?action=index&lang=zh_CN)）。

### 高级方式：Cookie 注入

如果你已有微信客户端的 cookies（通过 mitmproxy/Charles 等抓包获取），可以直接注入，跳过扫码：

```bash
wxmp_archiver run \
  --history-url "..." \
  --cookie "wxuin=xxx; wxsid=xxx; wxtokenkey=xxx" \
  --out ./out
```

`--cookie` 支持传原始字符串或文件路径。此模式使用 `profile_ext` 页面而非平台 API。

## 输出目录结构

```
out/
├── links.jsonl                          # 文章链接列表
├── failed.jsonl                         # 失败记录
├── articles/
│   ├── 20250315_文章标题/
│   │   ├── article.md                   # Markdown 正文（含 YAML frontmatter）
│   │   ├── article.html                 # 完整 HTML 备份
│   │   └── meta.json                    # 元数据（标题/作者/时间/article_id 等）
│   └── 20250320_另一篇文章/
│       ├── article.md
│       ├── article.html
│       └── meta.json
└── assets/
    ├── <article_id>/
    │   ├── a1b2c3d4e5f6.jpg             # 图片（sha1(url) 命名）
    │   └── f7e8d9c0b1a2.png
    └── <article_id>/
        └── ...
```

Markdown 和 HTML 中的图片链接均已替换为 `../../assets/<article_id>/<filename>` 相对路径，**离线可读**。

## 断点续跑 & 增量更新

- **article_id** = `sha1(url)` — 同一篇文章 URL 始终映射到相同 ID
- `fetch` 默认开启 `--resume`：若 `meta.json` 存在且 `completed: true`，自动跳过
- 使用 `--force` 可强制重新抓取所有文章
- 再次运行 `crawl-links` 会与已有 `links.jsonl` 合并去重（增量追加新文章）

## 常见问题排查

### 1. 扫码后提示"没有绑定公众号"

**原因**：QR 登录方式要求你的微信绑定了一个公众号账号。

**解决**：
- 去 [mp.weixin.qq.com](https://mp.weixin.qq.com/cgi-bin/registermidpage?action=index&lang=zh_CN) 注册一个免费的**个人订阅号**
- 注册完成后重新扫码即可

### 2. 频率限制 / 接口报错

**原因**：请求过于频繁触发了微信风控。

**解决**：
- 增大延迟：`--min-delay 5 --max-delay 10`
- 平台 API 被限流时工具会自动等待 60 秒后重试
- 严重限流时等待一段时间后重新运行

### 3. Token 过期

**原因**：登录 session 过期（通常几小时有效）。

**解决**：重新运行命令，再次扫码登录即可。

### 4. 图片无法加载

**原因**：微信图片 CDN 有防盗链策略。

**解决**：
- 工具默认优先从 Playwright 网络拦截获取图片（避免二次请求）
- 若拦截失败，会用 httpx 重新下载（带重试）
- 检查 `out/assets/<article_id>/` 目录确认图片是否下载成功

### 5. Playwright 浏览器未安装

```bash
playwright install chromium
```

### 6. macOS 权限问题

如果 Chromium 首次运行被 Gatekeeper 阻止：

```bash
xattr -cr $(python -c "import playwright; print(playwright.__file__.replace('__init__.py', ''))")
```

## 项目结构

```
wxmp_archiver/
├── __init__.py       # 版本号
├── cli.py            # Typer CLI 入口（crawl-links / fetch / run / retry-failed）
├── history.py        # 文章列表采集（QR 扫码 + 平台 API / profile_ext + 网络拦截）
├── article.py        # 单篇文章抓取与正文提取
├── assets.py         # 图片下载、离线化、链接重写
└── storage.py        # 存储工具（ID/目录/JSONL/meta.json/断点续跑/cookie 解析）
```

## License

MIT
