# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

閾界档案馆（yjsws）第三方档案 JSON 转换器。把外部来源的异常/悬案素材（txt / html / md / json）经 AI 结构化润色后，生成 PostgreSQL `tb_archive` 表的 upsert SQL。项目为一次性运行的脚本集合，无测试、无 lint 配置、无包管理清单以外的构建步骤。

## 环境与运行

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # psycopg2-binary, openai, python-dotenv（archive_content_2_md.py 另需 markdown）
```

`.env`（repo 根目录，未入库）需要：`DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`、`DEEPSEEK_API_KEY`。

**所有脚本都必须从 repo 根目录运行**：`dotenv_values(".env")` 以及 `data/`、`FILES_TO_SQL/` 都是相对 cwd 的路径。同时 `run.py` 用 `from polish import ...` 这类同级裸导入，依赖 Python 把「脚本所在目录」加入 sys.path——所以只能用 `python archive_content_markdown_update/run.py` 的形式调用，不能 `python -m`。

```powershell
python archive_content_markdown_update/run.py                    # 主流水线（批量）
python archive_content_markdown_update/polish.py data/1.json     # 只跑 AI 润色 → data/1_polished.json
python archive_content_markdown_update/import_archive.py data/1_polished.json  # 只生成 data/1.sql
python archive_content_markdown_update/export.py <id>            # 从库里导出一条 → data/archive_<case_id>.json
python utils/txt_2_json.py <file.txt>                            # txt → data/<stem>.json
python utils/archive_content_2_md.py <file.json>                 # 取 content 字段 → ./2md/<stem>.md
```

产出的 SQL 执行方式：`psql -d <dbname> -f data/output_<时间戳>.sql`（每批一个文件，逐个执行）。

## 主流水线（archive_content_markdown_update/）

`run.py` 是**常驻进程**：外层 `while True` 每轮重新扫描 `FILES_TO_SQL/`，取第一个文件处理；目录里没有待处理文件就调 `wait_for_input_files()` 挂起（每 `FILE_POLL_TICK`=10 秒扫一次），投进新文件后自动继续，只能 Ctrl+C 退出。

**`handled` 集合是这个循环的安全阀**：处理过但没被删掉的文件（解析失败、AI 拒绝、入库失败）会登记进去并从后续扫描中排除。没有它，坏文件会被无限重试、一直烧 API。注意它只在内存里，重启进程后这些文件会再试一次。

每个文件走三步，全部写进 `data/`，INSERT 每攒够 `BATCH_SIZE`（默认 10）条就落一次盘：

1. **parse**（run.py `parse_file_to_dicts`）→ 档案 dict **列表**，逐条落盘 `data/<stem>.json`。html 用内置 `HTMLParser` 抽纯文本；**txt/md 会把文件名补进 content 首行**（字幕类 txt 正文里通常没有事件名，文件名是唯一的标题线索；首行已是文件名时不重复添加）；json 支持单对象和**数组**（数组的每个元素是一份独立档案，产物命名 `<文件名>_<两位序号>`，单对象/txt/html 仍用文件名本身）。记录必须有 `content`，且正文不短于 `MIN_CONTENT_LEN`（默认 200 字，太短的在这一步就丢掉、不花 AI 调用），缺 `title` 时用 stem 兜底；数组里的坏记录只跳过自己。知乎抓取格式（`content_text`/`content_url`）由 `zhihu_to_archive` 自动归一化，抓取元数据（`voteup_count`、`creator_hash` 等）一律丢弃，因为 `tb_archive` 没有对应列。**例外是 `desc` 和 `content_id`**：前者要喂给 AI 做主题前置判断，后者要写进 `jobs_done.txt` 做追溯，所以都保留在档案 dict 里，靠 `import_archive.SKIP_FIELDS` 拦着不写库（`content_id` 另外还在 `polish.SKIP_FIELDS` 里，不让 AI 看见）。
2. **polish**（polish.py）→ 调 DeepSeek（OpenAI SDK + `base_url=https://api.deepseek.com`，流式），落盘 `data/<stem>_polished.json`，随即往根目录 `jobs_done.txt` 追加一行 `content_id`（没有 content_id 的来源退化成写 stem）。只有润色成功的才记账，被跳过/拒绝的不写，所以这份流水可以直接拿来做重跑去重。
3. **配图**（image_search.py）→ 拿润色后的 title 去百度图片搜一张，`insert_cover_image` 插到 content 第一个正文段落之后（跳过开头的标题/引用/列表块）。必须在落盘和拼 SQL 之前做，否则 json 和入库内容都会漏图。开关是 `run.ENABLE_IMAGE_SEARCH`。
4. **generate_sql**（import_archive.py）→ 拼 `INSERT INTO tb_archive ... ON CONFLICT (title) DO UPDATE`，攒进当前批次。由 `persist_record` **逐条**处理：先把 SQL 追加进本次运行的存档 `data/output_YYYYmmdd_HHMMSS.sql`（每次运行一个文件），再立刻入库。没有批量攒条的概念——生成一条就落库一条。

**入库约定**（`ENABLE_DB_IMPORT` 开关，连接信息取 `.env` 的 `DB_*`）：一条 = 一个事务；**先写存档再入库**，所以入库失败时 SQL 还在存档文件里，可以手工 `psql -f` 补执行，失败只打印 `❌` 并计数，不中断后续记录。库侧已确认：`unique_title` 唯一索引存在（`ON CONFLICT (title)` 依赖它），`characters`/`timelines`/`evidence`/`ref_links`/`tags` 都是 jsonb，`location` 是 geometry。

**禁跑时段闸门**：每条记录开跑前调 `wait_out_pause_window()`，北京时间**落在** `PAUSE_WINDOWS`（默认 9:00-12:00、14:00-18:00，左闭右开）内就挂起，到该时段结束自动继续；其余时间（含深夜）正常跑。注意方向——这两个区间是**禁跑**而非工作时段。每 60 秒轮询、每 10 分钟打印剩余时间。检查点放在「每条开跑前」而不是「每条跑完后」——两者在记录之间的效果等价，但前者顺带保证在禁跑时段启动时不会先偷跑一条。时区用固定 `UTC+8` 而不是本机时区，换机器不会跑偏。开关 `ENABLE_PAUSE_WINDOW`。

单个文件（或数组里的单条记录）失败只跳过它自己，流水线继续。由于冲突键是 `title`，run.py 会记录已生成的 title 并在同批出现重名时告警——数组输入下重名概率明显变高。

`preview.py` 是同一条链路的免 SQL 版本：`python archive_content_markdown_update/preview.py sources/test.json [序号]`，输出 `data/*_polished.json` 加 `preview_md/*.md`，用于人工检查 AI 润色质量。

### polish.py 的关键约定

- `SYSTEM_PROMPT` 是整个项目的业务规范所在：它定义了 `tb_archive` 的语义字段（`lang` 0=中文/1=英文、`location`/`location_desc`、`characters`/`timelines`/`evidence` 三张图结构的 node/edge schema、`ref_links`、`status`、`occurred_at`、以及 12 个预置 tags，允许在不合适时自造）；开头还有一条前置过滤：`desc`/`content` 与灵异或真实案件主题无关时，约定只返回 `{"skip": true, "reason": "..."}`，由 `polish_data` 识别后抛 `AIRejected`，run.py 打印 `🚫` 并跳过——这样跑题内容不会退化成非 JSON 文本、掉进交互修复循环。改字段语义时改这里，而不是散落在代码里。
- 润色前把原始非跳过字段快照进 `data["_originals"]`；AI 返回的字段直接覆盖回原 dict。
- `SKIP_FIELDS`（不喂给 AI）与 import_archive.py 的 `SKIP_FIELDS`（不写库）是两套不同的集合，改动时注意区分。
- AI 输出经 `clean_json_str` 剥 markdown 代码块和首尾杂字符。解析失败先走 `try_auto_repair` 自动修复链（`AUTO_REPAIRS`，按序取第一个成功的）：① `escape_newlines_in_strings` 把字符串字面量内的裸换行/CR/TAB 转义成 `\n`/`\t`——这是 AI 最常见的破格方式（markdown 正文直接带真换行），转义能保住段落与标题结构；② `strip_newlines` 用 `re.sub(r"\r?\n", "")` 清空所有换行兜底，注意它会**压平 markdown**。修复成功只打印 `🔧` 不打扰人。
- 自动修复全失败时抛 `ParseFailed`（带原始输出），run.py 用 `dump_failure` 把原文原样存进 `failure/<stem>.json`、失败原因存进同名 `.reason.txt`，然后继续下一条——**不阻塞问人**。把 `polish.INTERACTIVE_REPAIR` 设成 `True` 才会启用旧的交互式循环（`[e]` 打开 `$EDITOR`（默认 `code`）手改 / `[r]` 重新生成 / `[x]` 放弃）。
- **余额不足会挂起等充值**：`polish_with_retry` 用 `is_balance_error` 认出 DeepSeek 的 402 `Insufficient Balance`（按 `status_code` 或消息文本判断），打印 `💳` 后 `input()` 等回车，回车即**重试同一条**而不是跳过——否则那条记录会白丢。其他异常原样抛出走既有分支。非交互环境下 `input()` 抛 `EOFError` 并向上传播，避免无人值守时死循环刷屏。
- **生成中按 `s` 或 Esc 可中断当前这条**：`stream_ai` 每收一个 chunk 探一次键盘（Windows 走 `msvcrt.kbhit`，POSIX 回退到 `select`，需按键后回车），命中就 `stream.close()` 并抛 `SkipGeneration`。它是 `RuntimeError` 子类，所以直接落进 run.py 已有的「⏭️ 跳过本条、继续下一条」分支。`sys.stdin` 不是 tty 时（管道、CI）按键检测自动关闭。

### image_search.py 的两个坑

用的是 `image.baidu.com/search/acjson` 免密钥接口（`.env` 里没有任何百度凭证）。两点否则必然踩：

1. **必须先访问 `https://image.baidu.com/` 拿 BAIDUID cookie**，否则 `data` 会随机返回空数组——同样的请求第一次有结果、第三次就空了。`_get_opener()` 缓存了带 cookie 的 opener，进程内只换一次。
2. **参数必须给全**（`ct`/`fp`/`istype`/`gsm` 等一堆看似无用的），只传 `word`/`rn` 精简参数同样返回空。

**跨域是硬要求**（前端要能跨站渲染），靠两道关卡保证：`is_cdn_url()` 只放行 `https://img*.baidu.com`——`objURL` 指向的原始站点普遍既防盗链又不给 CORS 头，一律丢弃；`supports_cors()` 再对候选图发一次 HEAD（不下载图片本体），要求 200 + `Content-Type: image/*` + 带 `Access-Control-Allow-Origin`。`find_cover()` 逐张校验最多 `CORS_CANDIDATES`（8）张，返回第一张通过的，全军覆没就返回 `None`；所有异常都被吞掉——配图失败不该中断建档。

插入格式固定是 markdown 图片语法 `![档案标题](url)`。

单独调试：`python archive_content_markdown_update/image_search.py <关键词> [数量]`。

### bilibili_search.py / fix_bilibili_refs.py（B 站搜索链接 → 播放链接）

一次性维护任务：早期入库的档案 `ref_links` 里存的是 `search.bilibili.com/all?keyword=...`
**搜索页**链接，要换成真正的**视频播放页**，并在 content 首行插入 `@[video](链接)`。

```powershell
python archive_content_markdown_update/fix_bilibili_refs.py --dry-run --limit 30  # 先看命中率
python archive_content_markdown_update/fix_bilibili_refs.py                        # 正式跑
python archive_content_markdown_update/fix_bilibili_refs.py --fill-missing         # 只补正文首行
```

`--fill-missing` 针对「ref_links 早就是播放页（比如人工改过）、但正文没有 @[video] 首行」
的记录，不搜索、直接拿 ref_links 里的链接补首行。

**别用 B 站搜索接口做匹配**。关键词形如 `【元宝撸奇案】—<视频标题>`，标题又长又口语化，
实测直接搜命中率只有五成：接口能返回 20 条，但目标视频常常一条都排不进去。改成
**抓 UP 主全量投稿再本地比对**后命中率到 ~90%：整库 1562 条只由 7 个 UP 贡献，
`Catalog` 把每个 UP 的全部投稿标题缓存进 `data/bilibili_catalog.json`，之后 difflib
本地比对，既准又不用打接口。

**抓目录必须借浏览器的登录态**。`x/space/wbi/arc/search` 匿名调用翻两页就 `-352 风控校验失败`
/ `412`，加 sleep 也没用；而 Chrome 里带登录 cookie 跑 58 页零失败。cookie 拿不出来
（SESSDATA 是 HttpOnly，扩展也拦截 cookie 读取），所以流程是：Python 生成 wbi 签名 URL →
浏览器 fetch 抓完 → 存成 JSON 下载到本地 → 放进 `data/bilibili_catalog.json`。目录抓一次
就能一直用，日常跑 `fix_bilibili_refs.py` 不需要浏览器。（同理注意：页面里用 `<a download>`
触发下载要**不挂进 DOM** 地 dispatch click，否则 B 站 SPA 的全局点击处理会把它当路由跳转，
整页导航走掉、抓好的数据全丢。）

**宁可不改也不能配错**：`MATCH_THRESHOLD`（0.72）以下、以及退化关键词（`keyword=Wayne调查`
这种只剩 UP 名的、归一化后短于 `SHORT_TITLE_LEN` 又不是几乎完全一致的）一律跳过，记进
`data/bilibili_unmatched.tsv` 留人工处理。实测最低分那几条也都是对的——分数低只是因为
B 站标题带 `| Wayne调查`、`—【2022-05-31】-中文` 之类的后缀。

**找 UP 的 mid 也要登录态**：`search_type=bili_user` 匿名调用返回 `code=0` 但没有
`result` 字段，`resolve_mid()` 于是退化成「搜视频、挑 author 完全一致的那条」——这招
对投稿还在搜索结果里的 UP 有效，但 `万象奇谈` 这种搜不到的就得在浏览器里（带登录）
调一次 bili_user 才拿得到 mid（38951122）。

`data/bilibili_fix_done.txt` 记已处理 id，重跑自动跳过（断点续跑）；`insert_video_line`
会先摘掉正文里**任何位置**已有的 `@[video]` 行再插首行——早期人工加的那条可能在**文末**，
只判断首行会插成两条——所以整个脚本是幂等的。

### import_archive.py 的 SQL 约定

- 冲突键是 `title`；`ON CONFLICT DO UPDATE` 会更新除 `title` 外所有列并置 `updated_at = NOW()`。
- `characters/timelines/evidence/ref_links` 序列化成 JSON 字符串写入。
- `location` 特殊处理：不走普通转义，包成 `ST_GeomFromText('POINT(lng lat)', 4326)`（PostGIS，WGS84）。
- 转义靠手写的 `escape_sql_string`（单引号翻倍），没有参数化查询——新增字段时确保值经过它。

## 其他目录

- `FILES_TO_SQL/` — 主流水线的输入投放目录（不存在时 run.py 会创建并退出）。**处理完会删源文件**：`DELETE_AFTER_IMPORT` 开启且 `ENABLE_DB_IMPORT` 也开启时，一个文件里的记录**全部**入库成功才 `os.remove`；有任何一条解析失败/被拒/入库失败就保留整个文件并打印 `📌 保留源文件（N/M 条入库成功）`。所以这个目录应放副本，原始数据留在 `sources/`。
- `data/` — 所有中间产物与分批产出的 `output_<时间戳>.sql`。
- `sources/` — 原始抓取数据（如知乎导出），尚未接入流水线。
- `etc/truecrime_json_convert/` — 一次性的历史转换脚本（英文 true crime 数据集 → tb_archive SQL，含一个走本地 Ollama 翻译成中文的变体）。与主流水线无代码共享，作参考用，不要在此基础上做新功能。
- `utils/` — 独立小工具，彼此无依赖。
