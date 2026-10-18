# cc98-digest · CC98 主题词每日摘要

给 CC98（浙江大学校网论坛，https://www.cc98.org ）配置一组**主题词**，每天定时跑一次，
输出**过去 24 小时**内所有命中主题词的主题帖汇总报告。

- 只调用 CC98 官方**只读**接口（`/topic/search`），**不会发帖、回帖、点赞、签到**。
- 只依赖 Python 标准库，无需 `pip install`。
- 报告输出 Markdown（人看）与 CSV（Excel 看），文件按天归档，同时更新 `latest.md`。

---

## 1. 快速开始

**方式 A：网页界面（推荐，最省事）** —— 不用编辑任何文件：

```powershell
cd D:\dev\cc98-digest
python -m cc98digest gui
```

首次启动若没有 `config.json` 会自动生成一份；浏览器打开后在侧栏「账号登录」输入自己的
CC98 账号密码（登录后自动保存、之后免登录），再点「立即抓取」即可。

**方式 B：命令行** —— 适合脚本 / 计划任务 / 排障：

```powershell
cd D:\dev\cc98-digest

# 1) 生成配置文件（已存在则跳过）
python -m cc98digest init

# 2) 编辑 config.json 填入账号密码（见下方"配置"），或在当前会话里设置环境变量：
$env:CC98_USERNAME = "你的CC98用户名"
$env:CC98_PASSWORD = "你的CC98密码"

# 3) 验证登录 + 试跑一次（把报告直接打屏）
python -m cc98digest auth-test
python -m cc98digest once --print
```

注册每天 **23:00** 自动运行的计划任务（当前检索规则）：

```powershell
python -m cc98digest install-task --time 23:00
```

> 也可以用 `python cc98_digest.py ...`，两个入口完全等价。

---

## 2. 配置（config.json）

`init` 会从 `config.example.json` 复制一份 `config.json`（已被 `.gitignore` 忽略，不会进版本库）；
启动网页界面（`gui`）时若 `config.json` 不存在，也会自动生成一份。账号密码既可以在网页界面里登录
填写，也可以手工写进 `credentials.username` / `credentials.password`。

```jsonc
{
  "credentials": {
    "username": "",              // ← 填这里，或设置环境变量 CC98_USERNAME
    "password": "",              // ← 填这里，或设置环境变量 CC98_PASSWORD
    "api_base": "https://api.cc98.org"
    // client_id / client_secret / token_endpoint / scope 使用 CC98 第三方客户端通用公开值，
    // 一般不需要改；详见"鉴权说明"。
  },
  "search": {
    "keywords": ["AI", "人工智能", "黑客松", "考研搭子"],  // ← 改这里换主题词
    "match_mode": "any",         // any=命中任意一个词就录入；all=必须全部命中
    "match_style": "auto",       // auto=ASCII 词按整词边界匹配（"AI" 不再命中 "AirPods"）；
                                 // substring=纯子串；word=一律要求词边界
    "case_sensitive": false,     // 默认忽略大小写（"ai" 也能命中 "AI"）
    "hours": 24,                 // 时间窗口长度（小时）
    "page_size": 20,             // 每页条数；CC98 实测上限就是 20（写大了会被自动降到 20）
    "max_pages_per_keyword": 2,  // 每个词最多看 2 页（当前检索规则；硬上限锁死 2，写大了也无效）
    "run_time_budget_seconds": 180, // 整轮抓取时间预算；到点停止未完成的词并按已抓到的内容出报告（0=不限）
    "recent_feed_pages": 0,      // 是否额外扫"最新主题"作为补充；0=关闭（当前规则只用关键词检索）
    "rate_limit_seconds": 1.0    // 请求间隔；别调太小，太密会被 CC98 限流（403）
  },
  "output": {
    "dir": "reports",
    "formats": ["md", "csv"],    // 只想要 Markdown 就写 ["md"]
    "keep_days": 3,              // 检索记录保留天数：更早的报告/任务日志自动删除；0 = 不清理
    "max_report_sets": 10,       // 额外上限：reports/ 最多留 10 组（同一次运行的 md/csv/json 算一组）；0 = 不限
    "max_scheduler_logs": 3,     // 计划任务日志最多留几个文件
    "include_url": true
  },
  "runtime": {
    "state_file": "state.json",  // 记录"这帖报告过没有"，用于同天重跑标注 🔄
    "log_dir": "logs",
    "log_level": "INFO",
    "token_cache": ".token.json",
    "user_agent": "cc98-digest/1.0 (personal read-only digest)",
    "timeout_seconds": 20
  }
}
```

凭据优先级：**环境变量 `CC98_USERNAME` / `CC98_PASSWORD` > `config.json`**。
密码、client_secret、令牌**永远不写日志、不写报告、不进计划任务**（日志里统一显示 `***`）。

---

## 3. 命令一览

| 命令 | 作用 |
|------|------|
| `python -m cc98digest init [--force]` | 生成 `config.json`（已存在时默认不覆盖；`gui` 启动时也会自动生成） |
| `python -m cc98digest auth-test [--force-login]` | 验证配置与登录，并打一次搜索接口自检 |
| `python -m cc98digest run` | **标准入口**，计划任务调用它；按配置跑一次 |
| `python -m cc98digest once [--hours N] [--all-time] [--keyword K ...] [--max-pages N] [--page-size N] [--print] [--dry-run]` | 一次性运行，可临时改窗口/主题词/翻页上限，不改配置文件 |
| `python -m cc98digest install-task [--time HH:MM] [--name 任务名]` | 注册/更新 Windows 每日计划任务 |
| `python -m cc98digest uninstall-task [--name 任务名]` | 删除计划任务 |

全局参数（写在子命令**前面**）：`--config <路径>`、`--verbose`、`--no-console-log`。

退出码：`0` 成功；`1` 运行错误（网络/解析）；`2` 配置或凭据问题。计划任务的历史记录里能看到。

常用例子：

```powershell
python -m cc98digest once --hours 6 --print          # 只看最近 6 小时
python -m cc98digest once --keyword 实习 --print     # 临时换成"实习"
python -m cc98digest once --all-time --print         # 不做时间过滤，全部匹配结果
python -m cc98digest once --max-pages 30 --print     # 临时只翻 30 页（更快，覆盖更浅）
python -m cc98digest --verbose once --print          # 带 DEBUG 日志
python -m cc98digest once --dry-run                  # 不联网，用内置样例跑通报告链路
```

运行中按 `Ctrl+C` 不会丢结果：当前关键词按已抓到的部分收尾，立即写出"部分结果"报告后再退出（退出码 1）。

### 3.1 手动运行（复制这两行即可）

不再提供启动脚本。用项目自带的 Python 调模块入口，**每行先 `cd` 进项目目录**（`-m cc98digest` 和相对路径的 `config.json` / `reports` 都依赖当前目录）：

```powershell
# PowerShell（注意：PowerShell 里 cd 不认 /d，且带空格的路径要用 & 调用运算符）
cd D:\dev\cc98-digest; & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest once --print
```

```bat
:: CMD / 批处理（cd /d 是 cmd 的写法）
cd /d D:\dev\cc98-digest & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest once --print
```

只想验证登录、不抓数据：

```powershell
cd D:\dev\cc98-digest; & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest auth-test
```

常用变体（PowerShell，把子命令换掉即可）：

```powershell
cd D:\dev\cc98-digest; & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest once --keyword AI
cd D:\dev\cc98-digest; & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest once --dry-run
cd D:\dev\cc98-digest; & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest --help
```

如果你环境里的 `python` 就是 3.10+，也可以简写成：

```powershell
cd D:\dev\cc98-digest; python -m cc98digest once --print
```

退出码：`0` 成功、`1` 运行错误、`2` 配置或凭据问题。

**两个常见报错的含义**：

| 报错 | 原因 |
|------|------|
| `找不到接受实际参数"D:\..."的位置形式参数` | 在 PowerShell 里用了 `cd /d`；去掉 `/d`，或改用 `Set-Location` |
| `No module named cc98digest` | 当前目录不是 `D:\dev\cc98-digest`；先把 `cd` 切过去再执行 |

### 3.2 图形界面（本地网页）

```powershell
cd D:\dev\cc98-digest; & "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest gui
```

会自动打开浏览器（默认 <http://127.0.0.1:8848/>）；不想自动开就加 `--no-browser`，端口被占就 `--port 8849`。
服务是**只监听本机回环**的（`--host 127.0.0.1`），Ctrl+C 停止。**不需要安装任何依赖**：后端只用 Python 标准库
（`http.server`），前端是自带的 HTML/CSS/JS 单页，没有 CDN、没有构建步骤，断网也能用。

界面能做什么：

| 区域 | 内容 |
|------|------|
| 顶部（报头） | 运行时间 / 时间窗口 / 演练标记；右上角**朱印**显示当前状态（待 / 抓 / 毕 / 误） |
| 工具栏 | **立即抓取**（真实抓取）、**离线演练**、**刷新数据**、**登录自检**、只看最近 6 小时 |
| 主区 | 每个主题词一节，表格三列：**`#` / 标题（点开帖子）/ 发布时间**，`↻ 已报` 标出上一轮已报告过的帖子 |
| 侧栏 | **账号登录（输入 CC98 账号密码，登录后自动保存）**、命中/扫描/页数/窗口/账号 账本、计划任务状态与下次运行时间、**主题词管理（增删改查）**、历史留档（点开看旧报告） |
| 底部 | 运行日志实时滚动（SSE 推送，按 WARNING/ERROR/INFO 着色），点标题栏可展开收起 |

抓取在后台线程跑，页面不会卡住：点「立即抓取」后日志实时流出，完成后自动刷新报告。
界面与命令行**共用同一份 config.json 和 reports/**，所以两种方式可以随便混用。

**账号登录**（侧栏「账号登录」）：

- 未登录时输入 CC98 用户名和密码，点「登录」——成功后账号密码写入 `config.json`、令牌写入 `.token.json`；
- 之后每次启动自动免登录（令牌到期前用 `refresh_token` 自动续期），无需重复输入；
- 换人 / 换账号：直接在表单里填新账号密码再登录即可覆盖；点「退出登录」则清空本机保存的账号密码和令牌；
- 登录失败不会改动配置里已有的账号密码（先换令牌、成功才写盘）。

**主题词增删改查**（侧栏「主题词（可增删改）」）：

| 操作 | 界面动作 | 效果 |
|------|----------|------|
| 查 | 列表本身就是当前配置 | 显示 config.json 里的 `search.keywords` |
| 增 | 输入框填词 → 回车或点「加」 | 追加到末尾并立即写盘 |
| 改 | 点某行的「✎」→ 改名 → 回车 | 原地替换（位置不变）；Esc 取消 |
| 删 | 点某行的「✕」→ 确认 | 从配置移除 |
| 筛选 | 点词本身 | 只看该词的分组，点「全部显示」恢复 |

约束与行为：

- 空值、重复词、超长（>64 字）、超过 50 个、含量换行符 → 都会拒绝并弹出**中文原因**；
- 至少保留一个主题词，删到最后一个会被拦下；
- 写入是**原子替换**（先写 `.tmp` 再 rename），只改 `search.keywords` 一个字段——
  密码、client_secret、其它配置项一律原样保留（有专门测试守着，包括"凭据来自环境变量时不会被写进文件"）；
- **抓取进行中不允许改词**，避免这次运行的词和结果对不上（返回 400 并提示）；
- 改完配置**不会动当前报告**，报告里仍显示上一轮实际用的词；如果报告里有已被删除的词，
  侧栏会出现一条黄色提示「报告里还有已从配置移除的词：…（重新抓取后消失）」。

后端接口（想自己接别的界面也能用）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/report[?file=cc98-digest-YYYY-MM-DD-HHMM.json]` | 报告 JSON（含分组、扫描统计、告警），可指定历史文件 |
| GET | `/api/status` | 当前运行状态 + 配置摘要 + 计划任务状态 |
| GET | `/api/history` | 历史留档列表 |
| GET | `/api/keywords` | 当前主题词列表 |
| POST | `/api/keywords` | 新增，body `{"value": "词"}` |
| PATCH | `/api/keywords` | 改名，body `{"target": "旧", "newValue": "新"}` |
| DELETE | `/api/keywords?value=词` | 删除 |
| PUT | `/api/keywords` | 整表替换，body `{"keywords": [...]}` |
| GET | `/api/logs?since=N` / `/api/logs/stream` | 日志快照 / SSE 实时流 |
| POST | `/api/run` | 触发抓取，body 可带 `{hours, keywords, dryRun, allTime, pageSize}`；已在跑时返回 409 |
| POST | `/api/auth-test` | 登录与搜索接口自检 |
| POST | `/api/login` | 登录，body `{"username": "...", "password": "..."}`；成功写入 config.json 与 .token.json |
| POST | `/api/logout` | 退出登录，清空本机保存的账号密码与令牌 |
| POST | `/api/shutdown` | 关闭服务 |

主题词相关接口统一返回 `{"ok": true, "keywords": [...]}`；被拒绝时返回 HTTP 400 + `{"ok": false, "error": "中文原因"}`。

每次运行除 Markdown/CSV 外还会写一份 **`reports/latest.json`**，界面读的就是它（机器可读，schema 见 `build_report_payload`）。

### 3.3 开机自启（界面服务常驻）

```powershell
cd D:\dev\cc98-digest
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest autostart --install    # 设置
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest autostart --status     # 查看
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest autostart --remove     # 取消
```

设置后在**当前用户的"启动"文件夹**放一个快捷方式（不需要管理员权限，项目目录里不加任何文件）：

| 项 | 值 |
|---|---|
| 快捷方式 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\cc98-digest 界面服务.lnk` |
| 目标 | `...\pythonw.exe -m cc98digest autostart` |
| 工作目录 | `D:\dev\cc98-digest` |
| 窗口 | 最小化 + `pythonw` = **完全无窗口**，也不会自动弹浏览器 |

行为细节：

- **防重复启动**：入口先探测 `127.0.0.1:8848`，已被占用就写一行日志后静默退出（不会起出第二个服务）；
- 开机自启写自己的日志 `logs\autostart.log`，便于排查"开机到底有没有起来"；
- 之后打开 `http://127.0.0.1:8848/` 即可（建议加进浏览器收藏栏）；
- 不想重启就立刻生效：直接跑一次入口 `& "...\pythonw.exe" -m cc98digest autostart`（`pythonw` 让它不弹窗）。

> 也可以继续"手动启动"：终端里 `python -m cc98digest gui`，用完 Ctrl+C。两种方式共用同一份配置和报告。

---

## 4. 定时运行

`install-task` 注册的计划任务：

- 任务名：`CC98TopicDigest`（默认），每天 **23:00** 运行（`--time 22:30` 可改）。
- 每次检索范围：**当天 23:00 往前 24 小时**（即前一天 23:00:00 → 当天 23:00:00，闭区间）。
- 以**当前用户**身份运行、`RunLevel Limited`，**不需要管理员权限**。
- `StartWhenAvailable`：当天电脑关机/睡眠错过时间，开机后会补跑一次。
- `MultipleInstances IgnoreNew`：上一次还没跑完则跳过本次，不会叠跑。
- **失败自动重试** `RestartCount 3` + `RestartInterval 10 分钟`：实测踩过——23:00 运行时电脑刚开机还没连上网，
  DNS 解析失败直接退出码 2，那一整天的报告就丢了；现在等网络恢复后会自动补上。
- 最长执行时间 30 分钟。

相关文件：

| 文件 | 说明 |
|------|------|
| `run-scheduled.bat` | 任务实际调用的入口（已自动生成，含 UTF-8 设置与日志重定向） |
| `run-once.bat` | 手动前台跑一次：`run-once.bat --print` |
| `logs\scheduler-YYYYMMDD.log` | 计划任务每次运行的控制台输出（最多保留 3 个） |
| `logs\autostart.log` | 开机自启入口的日志（如果启用了 3.3） |
| `logs\cc98-digest.log` | 主日志（2MB 滚动，保留 5 份） |

查看/触发/删除任务：

```powershell
Get-ScheduledTask -TaskName CC98TopicDigest | Format-List TaskName,State
Get-ScheduledTaskInfo -TaskName CC98TopicDigest | Format-List LastRunTime,LastTaskResult,NextRunTime
Start-ScheduledTask -TaskName CC98TopicDigest      # 立刻手动跑一次
python -m cc98digest uninstall-task
```

> 如果 `python` 不在系统 PATH 里（例如只在某个虚拟环境/Conda 里），把 `scheduler.py` 的
> `python_for_task()` 逻辑或 `run-scheduled.bat` 里的 `set "PY=..."` 改成解释器绝对路径即可。
> 当前机器上安装计划任务时自动探测到的是 `D:\dev\python\python\python 3.12.5\pythonw.exe`
> （用 pythonw 避免每天弹黑框；路径含空格已验证可用）。

### 4.1 检索记录保留策略（自动清理）

每次运行**开始前和结束后各清一次**（开始前清是为了"即使这次抓取失败，磁盘也不会一直涨"），规则：

| 对象 | 规则 | 默认 |
|------|------|------|
| `reports/cc98-digest-*.md / .csv / .json` | 早于 `keep_days` 天的删除 | 3 天 |
| 同上（额外保险） | 即使都在保留期内，也只留最新的 `max_report_sets` 组 | 10 组 |
| `reports/latest.*` | **永不删除**（界面和历史留档依赖它） | — |
| `logs/scheduler-*.log` | 只留最新的 `max_scheduler_logs` 个 | 3 个 |
| `logs/cc98-digest.log` | 由 RotatingFileHandler 自己滚动 | 2 MB × 5 |
| `state.json` 里的"已报告"记录 | 早于 3 天的条目删除（24 小时窗口决定更早的再也用不上） | 3 天 |

"一组"= 同一次运行的 md/csv/json。清理结果会写进日志，例如：

```
2026-10-10 14:50:18 INFO  cc98digest.digest: 已清理 3 个过期检索记录（保留 3 天 / 最多 10 组报告 / 最多 3 个任务日志）。
```

想改策略就编辑 `config.json` 的 `output` 段；`keep_days: 0` + `max_report_sets: 0` 表示完全不清理。
实测：把上限从 30 收到 10，`reports/` 从 77 个文件 / 535 KB 降到 33 个 / 354 KB。

> 注意：界面侧栏的"历史留档"列表读的就是这些文件，所以**只保留 10 组**意味着更早的运行记录（无论多久前）
> 都会从列表里消失；需要长期留档就调大 `max_report_sets`。

---

## 5. 输出

```
reports/
  cc98-digest-2026-10-05-1300.md     # 每次运行一份
  cc98-digest-2026-10-05-1300.csv
  latest.md                          # 最近一次（方便直接打开）
  latest.csv
```

Markdown 报告包含（**只有按主题词分组这一种结果视图**，没有单独的汇总总表）：

1. 运行时间 / 时间窗口 / 主题词 / 命中总数；
2. **按主题词分组**：每个词一张紧凑表格，只有 **`#` / 标题（可点击，链接即帖子 URL）/ 发布时间** 三列，
   方便快速扫读；某词没命中时该小节写"无"；同时命中多个词的帖子会出现在每个相关词的小节里；
3. **数据完备性**小节：一句话说明检索方式（每词最多 2 页）+ 每个词的扫描统计（几页 / 扫了多少条 / 窗口内命中几条）。
   *不再单列"翻页触顶"栏*——上限锁死后每轮都会触及，属于既定规则，不作为异常提示；
   只有**抓取失败 / 超出时间预算 / 被 Ctrl+C 中断**才会在这里单独标注；
4. 运行告警（例如某个关键词抓取失败）。

`🔄` 标记（在分组表格的标题列里）表示这条帖子上一轮已经报告过，用于区分新增项；文件末尾会给出图例说明。
版面、作者、回复数等字段只存在于 CSV（`latest.csv`）里，Markdown 报告不再展示。

---

## 6. 工作原理与已知边界（重要）

**接口**：`GET https://api.cc98.org/topic/search?keyword=<词>&from=<偏移>&size=<条数>`
返回主题对象数组，字段：`id / title / boardId / boardName / userName / isAnonymous / time / lastPostTime / replyCount / hitCount / likeCount`。

**鉴权**：CC98 使用 OpenID Connect 的 password grant，令牌端点 `https://openid.cc98.org/connect/token`，
`scope = cc98-api openid offline_access`。`client_id` / `client_secret` 用的是 CC98 第三方客户端
（如 [cc98-mcp](https://github.com/EviterLesRoses2/cc98-mcp)、[CC98-CLI](https://github.com/Lucent-Snow/CC98-CLI)、CC98-Desktop）
通用的公开值，登录的仍然是**你自己的账号**，权限等价于以你身份只读浏览。账号密码保存在 `config.json`
（可在网页界面里登录写入），令牌缓存在 `.token.json`，到期前用 `refresh_token` 自动续期。

**时间处理**：接口返回的时间是 UTC，工具统一转成 `Asia/Shanghai`（UTC+8）后比较与展示。
时间窗口为闭区间 `[运行时刻 - hours, 运行时刻]`：23:00 运行即 **前一天 23:00:00 → 当天 23:00:00**。
上界会**真正拦住**晚于窗口结束的帖子（抓取要花时间，接口可能返回比运行时刻更新的帖，那些不属于本窗口）。

**已知边界（请知悉）**：

1. CC98 搜索接口**不支持按时间过滤或排序**。因此工具是"抓取关键词的搜索结果 → 本地按创建时间筛选"。
   当前检索规则：**每个关键词最多看 2 页（2×20 = 40 条）**，硬上限锁死在 `config.py`
   的 `PAGE_LIMIT_HARD = 2`，配置和 `--max-pages` 都改不大它。
   代价：像 `AI` 这种上千条命中、且搜索按相关性排序的词，**翻 2 页只能覆盖到最相关的几条**。
   这是当前检索规则的既定取舍，报告里不再为此单独报警（只在"数据完备性"里给出扫描统计）。
2. 关键词检索**只搜标题（其次作者名）**，命中后按创建时间判断是否落在 24 小时窗口内。
3. 全局"最新主题"接口 `/topic/new` 按发布时间倒序、每页 20 条 ≈ 15 分钟，可以作为时效性补充，
   但当前规则关闭了它（`search.recent_feed_pages = 0`），只走关键词检索。
4. 命中判定基于**标题**（其次作者名），不抓楼层正文。需要正文语义匹配的话，可在此基础上扩展
   `/topic/{id}/post` 拉取楼层。
5. "前 24 小时"按**创建时间**判定：老帖仅在 24 小时内被回复不会收录。若想改成"最近 24 小时有新回复"，
   把 `window.py` 里比较字段从 `created_at` 换成 `last_post_at` 即可（约 1 行）。
6. 单次运行 = 请求串行、默认间隔 1 秒。别把 `rate_limit_seconds` 调得太小：请求过密时 CC98 会回 403
   （连 `/me` 都 403），工具会自动等约 45 秒重试，仍失败则本轮跳过该关键词并写入告警。
7. **随时可以中止，且中止不丢结果**：
   - 整轮有 `run_time_budget_seconds`（默认 180 秒）预算，到点自动停止未完成的关键词并出报告；
   - 按 `Ctrl+C`：当前词按已抓到的部分收尾，**立刻写出"部分结果"报告**（表头会标 ⚠️，被中断/未扫描的词在
     "数据完备性"里注明），然后退出码 1。报告不会丢，剩余关键词记为"未扫描"。

---

## 7. 网络要求

`api.cc98.org` 属于浙大校园网服务，**通常需要校园网 / RVPN / 公网 IPv6** 才能访问：

- 校内有线/无线网：直连即可；
- 校外：需要 RVPN（浙大 RVPN）或 IPv6 直连；
- 本工具**不内置 WebVPN 中转**。校外用户请先接入 RVPN 再让计划任务运行，或参考
  [cc98-mcp](https://github.com/EviterLesRoses2/cc98-mcp) 的 WebVPN 方案自行扩展
  （`api.py` 的请求层是独立可替换的）。

自检：`python -m cc98digest auth-test` 会明确报出是"连不上令牌端点"还是"账号密码错误"。

---

## 8. 排障

| 现象 | 处理 |
|------|------|
| 退出码 2，提示"未配置 CC98 账号" | 填 `config.json`、设 `CC98_USERNAME`/`CC98_PASSWORD`，或在网页界面里登录 |
| 退出码 2，提示 `invalid_grant` | 账号或密码错误；或该账号需要先在浏览器登录一次完成验证 |
| 退出码 1，提示网络不可达/超时 | 不在校园网；接入 RVPN 或 IPv6 后重试（先跑 `auth-test`） |
| 报 `HTTP 400 invalid_from_or_size` | `page_size` 被配到 >20；工具会自动降到 20，无需手改 |
| 出现 `HTTP 403（疑似请求过密被限流）` | 请求太密被 CC98 限流；工具会自动等约 45 秒重试。改大 `rate_limit_seconds`、减少关键词或调小 `max_pages_per_keyword` 可缓解；被限流期间所有接口（含 `/me`）都会 403，等一两分钟自愈 |
| 报告里命中偏少 | 当前规则每词只看 2 页（硬上限，改不大），热门词（如 `AI`）按相关性排序只能覆盖最相关的部分 |
| 报告里"🔄"很多 | 说明 24 小时窗口与上一轮有重叠（例如补跑），属预期 |
| 报"本次结果为部分结果" | 有词抓取失败 / 超预算 / 被 Ctrl+C 中断；看"数据完备性"里的异常标注 |
| 运行太久想停 | 直接 `Ctrl+C`：当前词按已抓到的部分收尾并立刻出"部分结果"报告，再按一次退出 |
| 计划任务 `LastTaskResult` 非 0 | 看 `logs\scheduler-YYYYMMDD.log` 与 `logs\cc98-digest.log`；退出码含义见上 |
| 计划任务没在 23:00 跑 | 检查是否睡眠/关机（会补跑）、是否被 `IgnoreNew` 跳过、任务是否被禁用 |
| 控制台中文乱码 | 工具已强制 UTF-8；在 cmd 里先 `chcp 65001` |
| **手动抓取时每隔约 1 秒闪一下空白终端** | 已修（v1.2）。原因：前端轮询 `/api/status`，旧版每次都拉一个 `powershell` 子进程去查计划任务；服务由 `pythonw` 启动（无控制台），Windows 就给子进程新建控制台窗口。现在①子进程带 `CREATE_NO_WINDOW`；②计划任务状态改为**后台异步刷新 + 5 分钟缓存**，请求线程只读缓存。`/api/status` 从 ~1459ms 降到 ~1ms |
| 界面打开是"拒绝连接" | 服务没在跑：`python -m cc98digest autostart --status` 看是否设了开机自启；或直接手动跑 `python -m cc98digest gui` |

---

## 9. 目录结构

```
cc98-digest/
  cc98_digest.py            # 兼容入口（等价于 python -m cc98digest）
  cc98digest/
    __main__.py             # python -m cc98digest
    cli.py                  # 子命令与主流程编排
    config.py               # 配置加载/校验/脱敏
    auth.py                 # OAuth2 令牌获取、刷新、缓存
    api.py                  # HTTP 客户端：限速、重试、响应归一化、分页
    window.py               # 时间解析、时间窗口过滤、主题词匹配、去重
    render.py               # Markdown / CSV / JSON 渲染（纯函数，易测）
    state.py                # state.json：幂等标注与清理
    scheduler.py            # Windows 计划任务注册/卸载 + 批处理生成
    gui.py                  # 本地图形界面服务（标准库 HTTP + SSE 日志）
    static/                 # 前端单页：index.html / app.css / app.js / favicon.svg
    logging_setup.py        # UTF-8 日志
  config.example.json       # 模板（入库）
  config.json               # 你的配置（gitignore）
  run-scheduled.bat         # 计划任务入口（自动生成）
  run-once.bat              # 手动跑一次（自动生成）
  tests/                    # 199 个离线单测（Python）
  tests_js/                 # 11 个前端渲染冒烟测试（Node 内建 test runner）
  reports/  logs/           # 输出（gitignore）
```

---

## 10. 测试

```powershell
cd cc98-digest
# Python 侧：199 个用例
& "D:\dev\python\python\python 3.12.5\python.exe" -m unittest discover -s tests -t .
# 前端侧：11 个渲染冒烟测试（Node 内建 runner，零依赖）
node --test tests_js/ui-render.test.cjs
```

Python 用例全部**离线**运行（不联网、不需要真实账号），覆盖：

- 时间解析（ISO/带 Z/带偏移/无时区/嵌套对象/秒与毫秒时间戳/非法值）；
- 时间窗口（左闭右闭、恰好 24 小时、**上界拦住晚于窗口结束的帖子**、--all-time）；
- 主题词匹配（大小写、any/all、`match_style=auto` 的整词边界、中文子串）、多词命中合并与按 id 去重；
- HTTP 层：分页终止条件、**2 页硬上限**、401 强制换令牌重试、429 读 `Retry-After`、403 限流长退避、
  500 重试后失败、网络错误、非 JSON 响应、关键词 URL 编码、`{"data":[...]}` 等响应形态归一化；
- 鉴权：缓存命中短路、过期走 refresh_token、refresh 失败回落密码、`invalid_grant` 中文提示、令牌端点不可达；
- 渲染：空结果、转义（竖线/换行）、超长截断、**报告不再出现"全部命中"总表与"翻页触顶"栏**、告警小节、
  CSV BOM 与重复列、JSON payload 结构；
- 状态：记录/清理/损坏文件容错/原子写入；
- 计划任务：时刻格式校验、批处理生成、PowerShell 命令内容与不泄露密码、失败透传退出码；
- 图形界面：真实起服务打 HTTP（首页/静态资源/各 API）、目录穿越与文件名白名单、409 并发保护、
  `run` 参数透传、日志环形缓冲与 SSE、离线可用（前端不引用任何 CDN）；
- 主流程 dry-run 端到端：报告 + 状态 + 旧报告清理。

前端用例用真实 API 数据驱动 `app.js`，断言渲染结果（标题成链、时间列、`↻ 已报`、0 命中的说明、
异常词打标、XSS 转义、chip 筛选、SSE 写日志、印鉴状态）。

---

## 11. 帮助 / 一键排错

```powershell
cd D:\dev\cc98-digest
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest auth-test        # 先确认能登录
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest --verbose once --hours 48 --print   # 放宽窗口看原始结果
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest once --dry-run --print              # 完全离线验证报告渲染
& "D:\dev\python\python\python 3.12.5\python.exe" -m cc98digest gui              # 图形界面（会用同一份 config 与 reports）
Get-Content logs\cc98-digest.log -Tail 50                                        # 最近日志
```
