"""digest 流水线「写稿 job」的提示词（唯一副本）。

Le Registre 的一键初始化（``POST /api/digest/pipeline``）和手工建 job 都用这份，
避免提示词散落在 jobs.json、蓝图和文档三处各写一遍。

注意：这里只负责**叙述**。报告的表格、标识、参数、数字由 ``ops.digest.render_md``
从 facts 确定性渲染，模型不参与——所以提示词里反复强调「不许重写表格、不许编标识」。
"""

from __future__ import annotations

PROSE_PROMPT = """你是 Vigil 报告的**叙述撰写者**。注入的 JSON 是采集器确定性生成的事实（tasks / intents / objects）。报告的结构、对象、标识、参数、结果全部由脚本渲染，你只写叙述，**不要重写表格**。

文件路径：`<VIGIL_HOME>/reports/<前缀><facts.window.label>.prose.json`
（`<VIGIL_HOME>` 就是你这次运行环境里的 vigil 状态目录；不确定时读环境变量 VIGIL_HOME）
前缀按 `facts.window.granularity` 取（不要自己猜）：

- `daily`  → `Vigil-作业日报-`（窗口 ≤2 天）
- `weekly` → `Vigil-作业周报-`（窗口 ≤10 天）
- `range`  → `Vigil-作业报告-`（更长窗口，label 形如 2026-07-30_2026-10-28）

内容（严格 JSON，三个字符串字段，值都是 Markdown 片段，不含 `##` 标题）：

{
  "summary": "第 1 段「子目标与结果」的正文",
  "risks":   "第 3 段「未闭环与待跟进」的正文",
  "next":    "第 4 段「下周期」的正文"
}

写作要求：

1. summary：按 facts.tasks[].intents[] 组织，**每个子目标一行**，写成「做了什么 → 结果」的人话（例如：在 node233/test-pg 建了 superadmin xinggh，口令进 OpenBao，外部直连验证通过）。同一天同主题的子目标合并成一行。
2. 不要复述用户原话，不要写工具名、命令、临时脚本路径、会话 id —— 那些是证据不是结论。
3. 不要重复表格里已经有的标识（账号名、job 名、URL、uid）；summary 负责讲清「这些对象合起来是干了什么事」。
4. risks：只写 end_reason 不是 cli_close/turn_complete 的任务，以及 facts.gaps 和 identity 为空的对象；每条给「卡在哪 + 下一步」。没有就写「无」。
5. next：1-3 条，必须对应 risks 里的未闭环项；没有就写「无」。
6. **禁止编造** facts 里没有的对象、标识、URL、参数、账号名。facts 里 identity 为空 = 「标识未记录」，照实写，不许猜。
7. 不要发布任何东西，不要调用 publish_bookstack，不要用 curl/terminal 发请求 —— 渲染与发布由后面的 job 用脚本完成。
8. 文件写完用 read_file 读回确认是合法 JSON（能被解析），再结束。

最后回答两行：第一行 prose 文件绝对路径；第二行本周期最值得记住的一件事（≤60 字）。
"""

# 一键初始化时的两条 job 名与默认排期（周五 18:00 写稿 / 18:10 渲染发布）。
FACTS_JOB_NAME = "Weekly work log (digest)"
RENDER_JOB_NAME = "Weekly work log (render)"
DEFAULT_FACTS_SCHEDULE = "0 18 * * 5"
DEFAULT_RENDER_SCHEDULE = "10 18 * * 5"
FACTS_TOOLSETS = ["file", "session_search", "skills"]
