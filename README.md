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

原 `UnipusAI_Plus` 项目已彻底重构为 `UnipusAI-Helper`。

## 简介

`UnipusAI-Helper` 是从早期 `UnipusAI_Plus` 重构而来的桌面版工具，当前版本为 `3.6.0`，主程序为 `UnipusAI_Helper.py`，配置编辑器为 `config_editor.py`，界面为 Fluent 风格。项目重点在于更稳定的 GUI 体验、多任务的批量处理、单页面的手动处理。

---

## 特性

- GUI 控制台：显示任务清单、运行状态、实时日志和调试开关。
- 双模式处理：支持“扫描任务列表”批量处理，也支持“快速处理当前页”。
- 多题型处理：支持单选、多选、填空、写作、选词填空、拖拽排序、下拉选择、词汇测试、听力填空、听力选择、视频选择、视频任务、视频弹窗题、词汇闪卡、Self-check 词汇勾选、My voice 文字作答。
- 音视频辅助：支持本地 Whisper 转写。
- 环境检查：启动时检查 Edge、FFmpeg、网络和运行环境。
- 配置编辑器：提供独立 GUI 编辑器，减少手动修改 JSON 出错的概率。

---

## 项目结构

```text
UnipusAI-Helper/
├── UnipusAI_Helper.py
├── fluent_ui.py
├── config_editor.py
├── EnvironmentChecker.py
├── AudioRecognizer.py
├── config.json
├── requirements.txt
└── images/
```

---

## 运行环境

- Windows 10/11
- Python 3.10 及以上（3.12 与 3.13 已验证）
- Microsoft Edge
- FFmpeg
- OpenAI 兼容大模型接口

> [!IMPORTANT]
> 从v3.5之后不再提供Windows exe打包。项目依赖本地 Whisper 和 PyTorch，打包后体积较大，单文件程序还存在启动缓慢及 DLL 兼容风险。请克隆仓库并通过 Python 源码运行；首次启动本地语音识别时还可能下载 Whisper `base` 模型，请预留网络流量和磁盘空间。

---

## 使用方法

### 源码运行

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
  "max_tokens": 8192,
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
| `max_tokens` | 否 | 最大 token 数，保持配置编辑器生成的默认数值即可 |
| `temperature` | 否 | 生成温度，保持默认数值即可 |
| `token_full` | 否 | 认证兜底；支持旧版完整 `__token` JSON 或 U校园 5.0 的 JWT，正常登录时留空 |
| `debug_mode` | 否 | 是否开启调试日志，默认 `false` |



## 获取 `token_full`（可选）
从3.6.0版本之后不再强制需要手动获取token_full 程序会自己处理 此处仅保留为降级备选方案

正常情况下程序会保留本次 SSO 登录生成的认证信息，`token_full` 留空即可。仅当正常登录后仍提示缺少认证信息时，再手动配置以下任一种值：

`token_full` 是沿用旧版本的字段名，保存的是认证凭据，并不是独立的反作弊开关。程序仍需完成账号密码、验证码和服务端 SSO 校验；登录成功后优先使用平台签发的 `jwt` cookie。

- 旧版课程页的完整 token：登录后执行 `localStorage.getItem('__token')`。
- U校园 5.0 门户的 JWT：登录 `https://ucloud.unipus.cn/` 后执行 `JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT')).Authorization`。

将控制台返回的结果直接粘贴到配置编辑器。程序会区分两种格式，不会把裸 JWT 写入只接受完整 JSON 对象的旧版 `__token`。

> [!IMPORTANT]
> 手动编辑 `config.json` 时还需保证内容是合法 JSON；建议直接使用配置编辑器可自动处理字符串转义。

---

## 使用指南

1. 配置好基础信息，启动主程序并等待环境检查完成。
2. 程序会自动打开浏览器并执行登录流程。
3. 如果遇到验证码或人机验证，需要在浏览器中手动完成。
4. 系统就绪后，控制台显示系统就绪即为登录成功。

![Task Processing](images/4.png)

5. 先在浏览器点击 `我的课程` -> `选择你要刷的课程`，打开到展示教程目录的页面。

![Task Processing](images/5.png)

6. 可点击“扫描任务列表”，脚本会自动扫描所有单元所有任务并展示出来，你可以自行选择需要刷的任务，或一键选择所有必修任务。

![Task Processing](images/6.png)

7. 点击 `开始处理选中任务` 即可批量全自动处理选中任务。

8. 或者手动打开需要做的某一个页面，进入到题目页面，点击“快速处理当前页”，脚本则只做当前页面。

![Task Processing](images/7.png)

9. 做完后脚本会在延迟几秒后自动提交。  
这是为了模拟真人的思考时间，避免用时过短引起怀疑。  
控制台会提示当前页面处理完毕并自动提交作业。

![Task Processing](images/2.png)

![Task Processing](images/3.png)

---

## 题型适配投稿

我本身能接触到的教材有限 能适配的教材题型也有限 所以不能保证所有题型都适配
如果你遇到暂不支持的新题型 可以在 issue 中投稿页面结构 我会在有可复现材料时优先适配 也将会帮助项目变得更加完善。

在提交 issue 时 请尽量按照以下格式并提供以下内容 这会有利于我的适配：
1. 题型截图：直接截图整个页面即可。
2. 关键元素 HTML：在浏览器按下 F12 打开开发人员工具 在控制台粘贴以下内容 会自动获取当前页面的题型内容：

```javascript
(() => {
  const selectors = [
    '.layout-direction-container',
    '.abs-direction',
    '.layout-material-container',
    '.audio-material-wrapper',
    '.question-audio',
    'audio',
    '.layoutBody-container',
    '.question-common-abs-reply',
    '.question-common-abs-choice',
    '.question-wrap',
    '.question-basic'
  ];

  const data = {
    url: location.href,
    title: document.title,
    items: selectors.map(sel => ({
      selector: sel,
      count: document.querySelectorAll(sel).length,
      nodes: [...document.querySelectorAll(sel)].map((el, i) => ({
        index: i,
        text: (el.innerText || '').slice(0, 2000),
        html: el.outerHTML
      }))
    }))
  };

  copy(JSON.stringify(data, null, 2));
  console.log('已复制页面结构，可以直接粘贴给我');
})();
```
3. 将获取到的页面结构保存到 txt 文本文档中 并和图片一起作为 issue 附件上传

请注意隐私：

- 不要公开账号、密码、token、Cookie、API Key、学校个人信息。
- 如果截图里有姓名、学号等隐私信息，请注意打码。
- 音频、视频资源链接如果包含个人鉴权参数，也请先脱敏。

材料越完整，越容易适配；只有一句“这个题型不支持”的 issue 无法判断页面结构 将被作为无效 issue 关闭。

---

## 更新日志

### 2026-09-24

- 版本号更新至 `3.6.0`，适配 U校园 AI版 5.0。
- 修复 U校园 5.0 SSO 登录流程：等待可交互的账号和密码输入框，并点击可视的协议勾选控件，避免 `element not interactable`。
- 修复登录后停留在 `home?ticket=...` 并持续加载的问题：识别平台签发的 `jwt` cookie，随后进入不带一次性 ticket 的 `/home`。
- 调整认证状态同步：优先保留本次登录会话，兼容旧版完整 `__token` 和 5.0 JWT，避免空值或过期配置覆盖有效认证。
- 将 `token_full` 调整为可选认证兜底；正常 SSO 登录不再要求手动复制 token。
- 登录地址未显示账号表单时自动回退默认 SSO 入口。
- 修复普通运行信息被错误记录为 `ERROR` 的日志级别问题，并补充登录认证回归检查。

### 2026-07-05

- 版本号更新至 `3.5.0`，窗口标题和界面版本标识改为读取统一版本常量。
- 新增听力选择题识别：支持带音频材料的单选/判断题，先转写音频再根据音频内容作答。
- 新增视频选择题识别：支持“观看视频后判断 True/False 或选择答案”的题型，先播放并转写主视频，再根据视频内容作答。
- 新增视频填空题识别：支持观看视频片段后填写段落空格，复用视频转写并按空格上下文作答。
- 新增拖拽排序题识别：支持音频材料后的 A/B/C/D 信息块排序题，按材料出现顺序生成字母序列并自动重排。
- 新增 Self-check 词汇勾选识别：支持词汇自检表，自动勾选 Got it 列，避免误点 Review 列。
- 新增 My voice 文字作答识别：支持录音/上传作品页，自动生成 500 字符以内英文介绍，填写文字框并生成 PDF 附件上传以满足提交校验。
- 移除 `whisper_api` 配置项：音视频转写固定使用本地 Whisper，不再在配置文件和配置编辑器中保留 Whisper API 输入。
- 优化多题流程：支持同一任务点内连续点击“下一题”处理多道题，最后一题再提交。
- 优化文本框填写：填写后会回读校验，常规输入未生效时自动用 JS 同步输入框状态，避免日志成功但页面仍为空。
- 优化多文本框简答：同一个题目容器内包含多个简答输入框时，会按编号逐个填写所有文本框。
- 优化按钮识别：兼容 `<a class="btn">下一题</a>`、`<a class="btn">上一题</a>` 和 `<a class="btn">提 交</a>`，避免把翻页按钮误当提交按钮。
- 优化答案提取：兼容 AI 返回 `简答题: 1... 简答题: 2...` 的格式，避免题型标签混入上一题答案。
- 优化填空答案提取：兼容 AI 返回 `空1:`、`Blank 1:` 等格式，填写时自动去掉空号前缀。
- 优化听力填空：提取每个空所在句子的左右上下文，并在填写前修剪明显重复或不合语法的短语，减少答案串位。
- 优化音视频预处理：视频题页面会跳过 Words & tips 等短音频，避免把词汇发音误当成题目音频。

---

## 常见问题 Q&A

### 找不到 FFmpeg

- 听力和视频转写依赖 FFmpeg，需要安装并加入系统 `PATH`。

### 登录后白屏或状态异常

- 程序会等待 SSO 完成换票，并自动移除已消费的一次性 `ticket` 后再进入 `/home`。
- 若仍提示认证失败，先将 `token_full` 留空重试；仅在需要时按上文重新获取认证值。

### API 调用失败

- 优先检查：
- `api_key`
- `base_url`
- `model`



其他问题欢迎在 Issue 提出。

---

## 致谢

感谢优秀的开源土壤。
感谢 [UnipusAI](https://github.com/Zzj-klwgxdz/UnipusAI) 项目作者：Zzj-klwgxdz。这是纯命令行 + 原生 HTTP 实现的Rust版U校园 AI 版刷课脚本。  
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
