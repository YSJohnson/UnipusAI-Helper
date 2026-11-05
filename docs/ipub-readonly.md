# U 校园 5.0 iframe 课件：只读工具链设计与实测记录

本文记录 `ipub_adapter.py` / `ipub_questions.py` / `ipub_plan.py` / `ipub_dryrun.py`
这套**只读**工具的设计依据、实测结论和安全边界。它面向维护者与新教材适配排查，
不参与主程序的答题与提交流程。

## 1. 与上游主程序的关系

上游 `main` 已经实现了：

- U 校园 5.0 课程目录的 CSS Modules 前缀匹配选择器
  （`[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]`、
  `[class*="taskTypeName"], [class*="nodeTypeName"]`）与可见节点过滤
  （`getClientRects().length` / `is_displayed()`）；
- `iframe#ipublish-pc-book-easy-iframe` 的切换、题目作答与提交执行器。

因此本工具链**不修改** `UnipusAI_Helper.py` / `fluent_ui.py`，也不重复实现执行器。
它提供的是互补能力：在不改变页面状态的前提下，把「这一页到底有什么、题干从哪来、
每道题需要几个答案槽」讲清楚，并可离线回归。

## 2. 阶段一：iframe 探测与 DOM 抽取

任务页形如 `ucontent.unipus.cn/_ipublishpc_default/pc.html#/course-v3:.../courseware/...`，
真实内容在 `iframe#ipublish-pc-book-easy-iframe`（`ipub.unipus.cn/learner/textbook/book/...`）。

- `IpubDriver` Protocol 抽象出 `execute()` / `set_frame()`，提供 `SeleniumDriver`
  和 `WebDriverHttpClient` 两种实现；后者可直接复用 GUI 已打开的 msedgedriver 会话。
- `IpubFrame` 上下文管理器负责切入 / 切回，保证异常时也恢复顶层上下文。
- 抽取 JS 只读 `[data-ipublish-question-group-type]` 分组，共发现 9 种：
  `discussion_group`、`single_choice_group`、`fill_blanks_group`、
  `true_false_group`、`fill_blanks_choice_group`、`short_answer_group`、
  `oral_sentence_scoop_group`、`oral_personal_state_group`、`multi_media_upload_group`。
- `normalize_groups()` 归一化并如实统计「题干缺失」，不用选项去猜题。

实测（`tests/fixtures/ipub_dryrun_result.json`）：16 个分组、9 种题型、
38 道顶层题，阶段一 DOM 里 38 道题干全部缺失 —— 这个缺口必须显式暴露，不能掩盖。

## 3. 阶段二：题干来源结论

**题干不在渲染后的 DOM，也不需要从教材 CDN 或 `ucontent` API 解密获取。**

ipub 阅读器是 React 应用，每个 `[data-question-id]` 元素所属组件链的
`memoizedProps.questionConfig.json`（或 `questionStore.questionConfig.json`）里保存着
完整分组定义：

| 字段 | 用途 |
| --- | --- |
| `groupId` | 与 DOM 分组元素的 `id` 完全一致，可直接对齐 |
| `list[].quesTextString` / `children[].quesTextString` | 纯文本题干 |
| `list[].quesText` | Lexical JSON，用于统计 `insert-dig-empty` 填空占位 |
| `list[].options` / `children[].options` | 选项 / 词库，`optionValue` 可能是 Lexical JSON，需要展平 |
| `answers` | **故意不读、不导出**，只保留布尔标记 `has_answers` |

`ipub_questions.py` 只读遍历 fiber 的 `return` 链（最多 25 层）找到配置，
按 `groupId` 对齐，未知 id 时按分组顺序回退，题目数量不一致时安全跳过。

排查过并排除的路径：`studentContent.json`（CDN 明文 Lexical 树）、
`answer/list`、`getChapterTemplate`、Rust 版 `ucontent v3` AES-128-ECB 解密链路。
页内 `fetch` / 同步 XHR 需要浏览器身份态且不稳定，Python 直接 GET 不可行；
React 内存方案不需要任何额外网络请求或令牌。

实测：16/16 分组按 id 对齐，38/38 题干解析成功，0 缺失。

## 4. 阶段三：答案槽计划（只读 planner）

`ipub_plan.py` 是纯变换：输入阶段一 / 二的结果字典，输出可序列化的 `AnswerPlan`。

- 单选 / 判断：1 个 `choice` / `true_false` 槽，`accepted` 为去重后的选项标签；
- 填空：按 `blank_count`（上限 40，回退 DOM 空节点数）生成 `blank` 槽；
- 选词填空：每个 `bank` 槽带词库；简答：1 个 `text` 槽；
- 讨论、口语、媒体上传：明确 `supported=False` 并写明原因；
- 未知题型不猜测；题干缺失时 `prompt` 为空，绝不从选项反推。

`build_prompt()` 只组装题干、题型、允许值和输出格式，不含正确答案或凭据。
`apply_suggestions()` 是唯一可能访问网络的函数，且必须由调用方显式注入 client
（CLI 里即 `--llm` / `--llm-limit N`）；返回值只写入内存中的 `slot.suggestion`。
`--llm-limit 0` 使用一个必然抛错的 `NoNetworkClient`，保证零网络、零配置依赖。

离线回放实测：16 分组、38 题、32 题有槽位布局、53 个答案槽、1 题题干缺失；
换 key 后 `--llm-limit 3` 端到端烟测：3 次真实请求返回建议，其余 29 题明确标记
`skipped: --llm-limit 3 reached`，输出 JSON 不含 `answers` / `api_key` / `Bearer` / 密钥片段。

## 5. 与 GUI 答题执行器的兼容性报告

上游 `main` 的 `UnipusAI_Helper.py` 里有 `PublishQuestionParser.GROUP_KINDS`，
把 `data-ipublish-question-group-type` 映射到 `choice` / `true_false` / `fill` /
`dropdown` / `text` / `link_match` / `record` / `skip` / `upload`。
其中前七类 `parse_group()` 有实现，会自动作答；`skip` 主动跳过（阅读材料、小组讨论），
`upload` 已登记但没有解析分支，需要手动处理。

`ipub_support.py` 用 `ast` 读这张表（**不 import 主程序、不启动 GUI、不开浏览器**），
再和阶段一/二的 dry-run 结果合并，生成逐组报告：

```powershell
.\.venv\Scripts\python.exe ipub_dryrun.py --from-json tests/fixtures/ipub_dryrun_result.json --gui-support
```

输出会标出每组的 `GUI 自动作答 / GUI 主动跳过 / 需手动处理 / GUI 未登记`，
可用 `--support-out report.json` 保存 JSON。这样升级或切换 GUI 版本后，
不登录也能立刻看出新版本是否覆盖当前教材的题型。

在本教材的 16 组真实抽取结果上，上游 0ceb50b7（v3.9.3）的报告是
**14 组自动作答、1 组主动跳过（discussion_group）、1 组需手动（multi_media_upload_group）**，
没有未登记题型。

## 6. 安全边界

所有阶段一律不执行：填答、点击、切页提交、发帖、上传、读取正确答案。
`tests/test_ipub_*.py` 中有源码级断言，禁止 planner 出现 `.click(`、`.submit(`、
`send_keys`、`switch_to`、`fetch(`、`innerHTML =`、`it.answers` 等写入或答案读取。
`ipub_dryrun.py` 默认既不调用大模型，也不写页面；`--plan-out` 只写本地 JSON。

本工具链只用于页面结构研究、回归测试与配置验证。请遵守学校、课程平台与相关法律
法规，不要用于违反平台规则或影响教学公平的用途。

## 7. 回归

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

全部 71 个用例离线通过（阶段一 10、阶段二 20、阶段三 32、兼容性报告 9），
不需要浏览器、不需要网络、不需要 `config.json`。
