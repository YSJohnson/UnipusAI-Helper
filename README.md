# UnipusAI-Helper

<p align="center">
  <img src="images/1.png" alt="UnipusAI-Helper" width="900" />
</p>

<p align="center">
  基于 Selenium、Fluent UI 与 OpenAI 兼容接口的 U 校园 AI 版刷课工具
</p>

<p align="center">  
  <img alt="GUI" src="https://img.shields.io/badge/gui-Fluent_UI-2FA572">
  <img alt="Browser" src="https://img.shields.io/badge/browser-Selenium-43B02A">
  <img alt="AI" src="https://img.shields.io/badge/api-OpenAI%20Compatible-111111">
  <img src="https://img.shields.io/badge/license-AGPLv3-blue.svg" alt="License">
</p>



## 简介

`UnipusAI-Helper` 是从早期 `UnipusAI_Plus` 重构而来的桌面版工具，当前版本为 `3.7.0`，主程序为 `UnipusAI_Helper.py`，配置编辑器为 `config_editor.py`。

---

## 特性

- GUI 控制台：显示任务清单、完成状态、运行状态、实时日志和调试开关；账号默认脱敏显示。
- 任务状态感知：扫描时读取每个任务是「已完成 / 未开始 / 进行中」，默认只勾选**未完成必修**。
- 断点续跑：每完成一个任务自动取消勾选，停止后剩下的勾选就是「还没做」的任务，再点开始即从卡住的那题继续。
- 双模式处理：支持「扫描任务列表」批量处理、只扫「当前单元必修」，也支持「快速处理当前页」。
- 单元编号按目录真实标题显示，`Course preview` 这类预习单元单列为「预备单元」，不会把后面的 Unit 挤乱。
- 多题型处理：支持单选、多选、填空、写作、选词填空、拖拽排序、下拉选择、词汇测试、听力填空、听力选择、视频选择、视频任务、视频弹窗题、词汇闪卡、Self-check 词汇勾选、My voice 文字作答、**人物 A–F 匹配**、**翻译段落**、**首字母搭配填空**、**Wrapping up 自评清单**、**讨论区发帖**。
- 答案格式校验：选项题会把模型回填的选项原文反查成字母；本页有题没填好时自动二次修正。
- 搭配题增强：自动读取同题组的 `Learning` 页当答案上下文；空位多时按 8 空分批提问；题干印在输入框外的首字母会自动去掉再写入。
- 做完自动交卷：填完直接提交，日志提示「已自动提交」；找不到按钮会明确提示手动提交。
- 音视频辅助：支持本地 Whisper 转写。
- 环境检查：启动时检查 Edge、FFmpeg、网络和运行环境。
- 配置编辑器：提供独立 GUI 编辑器，减少手动修改 JSON 出错的概率。

---

## 使用方法

```powershell
git clone https://github.com/YSJohnson/UnipusAI-Helper.git
cd UnipusAI-Helper
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe config_editor.py
.venv\Scripts\python.exe UnipusAI_Helper.py
```

如果系统没有 `py` 命令，可将创建虚拟环境的命令改为 `python -m venv .venv`。先运行配置编辑器生成 `config.json`，保存后再启动主程序。

如需跳过环境检查：

```powershell
.venv\Scripts\python.exe UnipusAI_Helper.py --skip-check
```

---

## 配置说明

项目使用 `config.json` 作为本地配置文件，建议使用配置编辑器而不是手动修改。

```json
{
  "username": "",
  "password": "",
  "url": "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome",
  "api_key": "",
  "base_url": "",
  "model": "",
  "max_tokens": 32768,
  "temperature": 0.3,
  "token_full": "",
  "debug_mode": false
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `username` | 是 | U 校园 AI 版账号，用于自动登录 |
| `password` | 是 | U 校园 AI 版密码，用于自动登录 |
| `url` | 否 | 登录入口，留空或保持默认值即可 |
| `api_key` | 是 | 大模型接口密钥 |
| `base_url` | 是 | OpenAI 兼容接口地址，例如 `https://api.siliconflow.cn/v1` |
| `model` | 是 | 接口支持的模型名称 |
| `max_tokens` | 否 | 最大 token 数，默认 `32768`。推理型模型会把预算烧在思考上，代码层下限也是 32768；模型不接受时会自动减半重试 |
| `temperature` | 否 | 生成温度，保持默认数值即可 |
| `token_full` | 否 | 认证兜底；支持旧版完整 `__token` JSON 或 U校园 5.0 的 JWT，正常登录时留空 |
| `debug_mode` | 否 | 是否开启调试日志，默认 `false` |

---

## 使用指南

1. 配置好基础信息，启动主程序并等待环境检查完成。
2. 程序会自动打开浏览器并执行登录流程。
3. 如果遇到验证码或人机验证，需要在浏览器中手动完成。
4. 系统就绪后，控制台显示系统就绪即为登录成功。

![Task Processing](images/2.png)

5. 先在浏览器点击 `我的课程` -> `选择你要刷的课程`，打开到展示教程目录的页面。

![Task Processing](images/5.png)

6. 可点击「扫描任务列表」，脚本会扫描所有单元所有任务并展示出来；扫描完默认勾选的是**未完成必修**，
   也可以点「必修」「仅未完成必修」「全选」「清空」自行调整。
   只想做当前单元时，用「扫描当前单元必修」即可，不会切换其它单元。

![Task Processing](images/1.png)

7. 点击「处理选中任务」即可批量全自动处理选中任务。每做完一个任务会自动取消勾选并把状态改成「已完成」；
   中途卡住点「停止」后，剩下的勾选就是还没做的任务，再点一次「处理选中任务」会从第一道未完成的继续。

8. 或者手动打开需要做的某一个页面，进入到题目页面，点击「快速处理当前页」，脚本则只做当前页面。

![Task Processing](images/7.png)

9. 做完后脚本会在延迟几秒后自动提交。  
控制台会提示「已自动提交」；若页面按钮结构特殊没点中，会提示「请手动提交」。

![Task Processing](images/3.png)

---

## 更新日志

<details>
<summary><b>点击展开完整更新日志</b></summary>

<details>
<summary><b>v3.7.0（2026-10）</b>：任务状态感知 / 断点续跑 / 匹配题与新题型适配 / 自动交卷</summary>

感谢 [iceqi717](https://github.com/iceqi717) 提供账号

**使用体验**

- 扫描时读取每个任务的「已完成 / 未开始 / 进行中」，任务行右侧显示状态标签；
- 新增「仅未完成必修」筛选，**扫描完默认只勾选未完成必修**；
- 每完成一个任务自动取消勾选并把状态改成「已完成」，停止后剩下的勾选就是还没做的，
  再点开始即从卡住的那题继续（断点续跑）；
- 新增「扫描当前单元必修」，只扫当前单元、不切换其它 Unit；
- 单元编号改用目录真实标题，`Course preview` 这类预习单元单列为「预备单元」，不再把后面的 Unit 挤乱；
- 界面字号整体放大、日志区额外放大到约 130%，分组标题加粗提亮；账号默认脱敏显示；
- 日志去掉 emoji，改用 `【请求】/【返回】/【等待】/【跳过】/【完成】` 等标签；AI 请求前后打印字数与耗时。

**答题能力**

- 新增**人物 A–F 匹配题**：按 `data-scoop-index` 切分陈述，模型只回字母，逐空写入；
- 选项题会把模型回填的选项原文反查成字母；本页没填好时自动二次修正；
- 空位多的填空题按 8 空分批提问，整批返回空就 8→4→2 对半拆，最后 1–2 空换短问法重试；
- 搭配题自动读取同题组的 `Learning` 页当答案上下文，并用「同前缀候选词」抑制误选；
- 题干印在输入框外的首字母会自动去掉再写入（`un`+`unhappy` → 写 `happy`）；
- 填完**自动提交**，日志提示「已自动提交」；点不中按钮会明确提示手动提交；
- `max_tokens` 默认 32768，代码层下限 32768，模型不接受时自动减半重试。

**旧版播放器（course-v2）新增题型适配**

| 题型 | 识别特征 | 处理方式 |
| --- | --- | --- |
| 人物 A–F 匹配 | 方向含 `match the people` / `Filling the blanks with A-F` | 按空位切分陈述，模型只回字母，逐空写入 |
| 翻译段落 | 方向含 `Translate … into Chinese` | 把原文整段喂给模型，只输出中文译文 |
| 首字母搭配填空 | 空位前有 1–4 个绿色首字母（`un____` / `la____`） | 模型给完整单词，程序自动去掉题干已印出的前缀再写入 |
| 看视频写 summary | textarea + `summarize` 方向 | 转写视频后写 3–4 句英文总结 |
| Wrapping up 自评清单 | `Got it` 表格 + `anticon-border` 图标 | 逐行点「Got it」 |
| 讨论区发帖 | `textarea[placeholder="我来评论"]` + 发布按钮 | AI 写 60–90 词观点并发布，校验评论数 |

搭配填空题还会：读同题组 `Learning` 页当上下文（内容过短自动回退去读同单元 `Reading the text`）；
空位 >8 时分批；从题面 + 上下文给每个空列**同前缀候选词**；填完自动提交。

</details>

<details>
<summary><b>v3.6.0（2026-10）</b>：适配新版 ipublish 播放器教材（course-v3）</summary>

`新农科大学英语` 这类 course-v3 教材完成初步适配，感谢 [Miku196](https://github.com/Miku196) 提供的账号。
使用方式与旧版一致。

| 题型 | 播放器标记 | 处理方式 |
| --- | --- | --- |
| 单选 / 多选 | `single_choice_group` / `multiple_choice_group` | 按选项字母点击 |
| 判断 | `true_false_group` | 点击 T / F |
| 填空 | `fill_blanks_group` | 按空号写入 `input`（支持一个组里多道小题、多个空） |
| 选词填空 | `fill_blanks_choice_group` | 从候选词中选择后写入 |
| 下拉填空 | `fill_blanks_dropdown_group` | 点开空位抓取候选列表后交给模型选择，按文本精确点选 |
| 简答 / 翻译 / 写作 | `short_answer_group` / `translation_group` / `writing_group` | 写入文本作答框 |

会跳过并提示的题型：口语录音（`oral_personal_state_group`）、附件上传
（`multi_media_upload_group`）、连线匹配（`text_match_group`）、小组讨论（`discussion_group`）。
纯阅读材料（`rich_text_read_group`）不答题，但会作为上下文提供给模型。

</details>

<details>
<summary><b>v3.5.x 及更早</b>：多选修复、任务定位、依赖与界面</summary>

- 2026-10-07 修正多选判定与选项点击（#20）
- 2026-10-07 兼容新版课程目录 node 任务结构（#16）
- 2026-09-24 适配 U校园 AI 版 5.0，移除 `numpy<2.0.0` 限制修复 Python 3.13+ 安装失败
- 2026-09-18 修复同名任务定位与停止响应
- 2026-09-09 移除遗留代码与冗余依赖，精简代码结构
- 2026-09-04 `AudioTranscriber` 支持 `use_local`，修复 TypeError
- 2026-07-05 v3.5.0 题型更新
- 2026-06-07 更换 Fluent UI 界面

</details>

</details>

---

## 题型适配投稿

我本身能接触到的教材有限 能适配的教材题型也有限 所以不能保证所有题型都适配 目前已知新视野大学英语1 2 3册是基本全部适配 新农科大学英语完成初步适配 其他教材可能会出现不适配的情况。

如果你遇到暂不支持的新题型/新教材 可以在 issue 中投稿页面结构 或者是联系我的邮箱私信发送账号密码（我会在最小范围内使用 这也是最快最高效的方法） 我会在有可复现材料时优先适配 也将会帮助项目变得更加完善。

材料越完整，越容易适配；只有一句“这个题型不支持”的 issue 无法判断页面结构 将被作为无效 issue 关闭。

---

## 致谢

感谢优秀的开源土壤。
感谢 [UnipusAI](https://github.com/Zzj-klwgxdz/UnipusAI) 项目作者 [Zzj-klwgxdz](https://github.com/Zzj-klwgxdz)。这是纯命令行 + 原生 HTTP 实现的 Rust 版 U 校园 AI 版刷课脚本。  
感谢原作者提供的强大解析器框架与思路，特别是其针对 U 校园 AI 版登录认证状态的处理机制。

---

## Star⭐ 承蒙厚爱

[![Star History Chart](https://api.star-history.com/svg?repos=YSJohnson/UnipusAI-Helper&type=Date)](https://www.star-history.com/#YSJohnson/UnipusAI-Helper&Date)

---

## License

本项目采用 **GNU Affero General Public License v3.0 (AGPLv3)**。  
你可以自由使用、修改和分发本项目，但任何修改版或衍生作品必须以相同的 AGPLv3 协议开源。即使是作为网络服务运行（SaaS），也必须向用户公开完整的源代码。

> [!WARNING]
> 本项目仅用于 Python 自动化、Web 页面交互、语音识别与大模型接口接入的学习研究。请遵守学校、课程平台和相关法律法规，不要将其用于违反平台规则或影响教学公平的用途。
