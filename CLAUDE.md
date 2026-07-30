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

`run.py` 遍历 `FILES_TO_SQL/` 下的 `.html/.json/.txt/.md`，每个文件走三步，全部写进 `data/`，INSERT 每攒够 `BATCH_SIZE`（默认 10）条就落一次盘：

1. **parse**（run.py `parse_file_to_dicts`）→ 档案 dict **列表**，逐条落盘 `data/<stem>.json`。html 用内置 `HTMLParser` 抽纯文本；**txt/md 会把文件名补进 content 首行**（字幕类 txt 正文里通常没有事件名，文件名是唯一的标题线索；首行已是文件名时不重复添加）；json 支持单对象和**数组**（数组的每个元素是一份独立档案，产物命名 `<文件名>_<两位序号>`，单对象/txt/html 仍用文件名本身）。记录必须有 `content`，且正文不短于 `MIN_CONTENT_LEN`（默认 200 字，太短的在这一步就丢掉、不花 AI 调用），缺 `title` 时用 stem 兜底；数组里的坏记录只跳过自己。知乎抓取格式（`content_text`/`content_url`）由 `zhihu_to_archive` 自动归一化，抓取元数据（`voteup_count`、`creator_hash` 等）一律丢弃，因为 `tb_archive` 没有对应列。**例外是 `desc` 和 `content_id`**：前者要喂给 AI 做主题前置判断，后者要写进 `jobs_done.txt` 做追溯，所以都保留在档案 dict 里，靠 `import_archive.SKIP_FIELDS` 拦着不写库（`content_id` 另外还在 `polish.SKIP_FIELDS` 里，不让 AI 看见）。
2. **polish**（polish.py）→ 调 DeepSeek（OpenAI SDK + `base_url=https://api.deepseek.com`，流式），落盘 `data/<stem>_polished.json`，随即往根目录 `jobs_done.txt` 追加一行 `content_id`（没有 content_id 的来源退化成写 stem）。只有润色成功的才记账，被跳过/拒绝的不写，所以这份流水可以直接拿来做重跑去重。
3. **generate_sql**（import_archive.py）→ 拼 `INSERT INTO tb_archive ... ON CONFLICT (title) DO UPDATE`，攒进当前批次。每满 10 条由 `flush_sql` 写出 `data/output_YYYYmmdd_HHMMSS.sql`（同秒内多次落盘会补 `_2`、`_3` 后缀防覆盖），跑完把不足一批的余量也写出去。分批的意义是长任务中途崩溃/中断时已完成的部分不会丢。

单个文件（或数组里的单条记录）失败只跳过它自己，流水线继续。由于冲突键是 `title`，run.py 会记录已生成的 title 并在同批出现重名时告警——数组输入下重名概率明显变高。

`preview.py` 是同一条链路的免 SQL 版本：`python archive_content_markdown_update/preview.py sources/test.json [序号]`，输出 `data/*_polished.json` 加 `preview_md/*.md`，用于人工检查 AI 润色质量。

### polish.py 的关键约定

- `SYSTEM_PROMPT` 是整个项目的业务规范所在：它定义了 `tb_archive` 的语义字段（`lang` 0=中文/1=英文、`location`/`location_desc`、`characters`/`timelines`/`evidence` 三张图结构的 node/edge schema、`ref_links`、`status`、`occurred_at`、以及 12 个预置 tags，允许在不合适时自造）；开头还有一条前置过滤：`desc`/`content` 与灵异或真实案件主题无关时，约定只返回 `{"skip": true, "reason": "..."}`，由 `polish_data` 识别后抛 `AIRejected`，run.py 打印 `🚫` 并跳过——这样跑题内容不会退化成非 JSON 文本、掉进交互修复循环。改字段语义时改这里，而不是散落在代码里。
- 润色前把原始非跳过字段快照进 `data["_originals"]`；AI 返回的字段直接覆盖回原 dict。
- `SKIP_FIELDS`（不喂给 AI）与 import_archive.py 的 `SKIP_FIELDS`（不写库）是两套不同的集合，改动时注意区分。
- AI 输出经 `clean_json_str` 剥 markdown 代码块和首尾杂字符；解析失败会进入**交互式循环**（`[e]` 打开 `$EDITOR`（默认 `code`）手改 / `[r]` 重新生成 / `[x]` 放弃），最多 10 次。因此该流水线不能无人值守跑在 CI 里。
- **生成中按 `s` 或 Esc 可中断当前这条**：`stream_ai` 每收一个 chunk 探一次键盘（Windows 走 `msvcrt.kbhit`，POSIX 回退到 `select`，需按键后回车），命中就 `stream.close()` 并抛 `SkipGeneration`。它是 `RuntimeError` 子类，所以直接落进 run.py 已有的「⏭️ 跳过本条、继续下一条」分支。`sys.stdin` 不是 tty 时（管道、CI）按键检测自动关闭。

### import_archive.py 的 SQL 约定

- 冲突键是 `title`；`ON CONFLICT DO UPDATE` 会更新除 `title` 外所有列并置 `updated_at = NOW()`。
- `characters/timelines/evidence/ref_links` 序列化成 JSON 字符串写入。
- `location` 特殊处理：不走普通转义，包成 `ST_GeomFromText('POINT(lng lat)', 4326)`（PostGIS，WGS84）。
- 转义靠手写的 `escape_sql_string`（单引号翻倍），没有参数化查询——新增字段时确保值经过它。

## 其他目录

- `FILES_TO_SQL/` — 主流水线的输入投放目录（不存在时 run.py 会创建并退出）。
- `data/` — 所有中间产物与分批产出的 `output_<时间戳>.sql`。
- `sources/` — 原始抓取数据（如知乎导出），尚未接入流水线。
- `etc/truecrime_json_convert/` — 一次性的历史转换脚本（英文 true crime 数据集 → tb_archive SQL，含一个走本地 Ollama 翻译成中文的变体）。与主流水线无代码共享，作参考用，不要在此基础上做新功能。
- `utils/` — 独立小工具，彼此无依赖。
