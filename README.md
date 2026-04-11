# 🚀 U校园AI版刷课脚本Plus版
基于大语言模型（LLM）与 Web 自动化技术的 关于U校园AI版的刷课脚本。
通过全新重构的 CustomTkinter 仪表盘与半自动模式，提供前所未有的稳定体验。
## 📸 界面预览 | Screenshots
![image](images/1.png)
## ✨ 为什么选择 Plus 版？ | What's New
本项目基于原作者 Zzj-klwgxdz 的强大基础进行**深度二次开发**。相比于原版纯命令行的全自动脚本，Plus 版全面升级了**可控性、稳定性和交互体验**：
| 特性分类 | 📉 原版痛点 (Console 模式) | 🚀 Plus 版革新 (GUI 增强模式) |
|---|---|---|
| 🎯 **作答模式** | 每次必须从头扫描，可能会做老师不要求做的板块，且无法跳过，无法自己选择。 | 浏览器内自由翻页，自主选择需要做的页面，一键抓取当前页作答 |
| 🖥️ **交互体验** | 命令行黑框| **CustomTkinter 仪表盘。** 跨线程日志同步，进度一目了然，界面流畅永不假死。 |
| 🐛 **算法修复** | 修复了同一问多个空会导致后续答案全部错位填入 | **彻底重构 AI 答案解析正则引擎。** 精准匹配题号，保障答案对齐。 |
## 🎯 主要功能 | Main Features
本项目支持 U校园AI版 平台绝大多数常见题型与特殊任务，接入KIMI API，实现AI答题：
 * 📝 **全题型智能作答**
   * **基础题型**：单选题、多选题、一般填空题、简答题、翻译题。
   * **复杂题型**：选词填空（完美支持单词变形及**断开式长短语**）、下拉选择题。
   * **词汇专项**：英汉互译测试、语境词汇填空。

## 🛡️ 核心黑科技：防作弊绕过机制
U校园平台拥有非常严格的自动化防作弊检测机制。
> [!IMPORTANT]
> **本项目的核心基石是原作者Zzj-klwgxdz精妙的 Token 注入绕过机制：**
> 程序在启动 Selenium 控制浏览器登录后，会通过执行 JavaScript 脚本，**强行向浏览器的 Local Storage 注入合法的真实用户凭证（即 token_full）**，随后刷新页面。
> 这使得 U校园底层鉴权系统误认为这是一个“继承了合法登录状态的真实物理浏览器”，从而完美绕过前置特征检测。
>
token获取方法：
手动在浏览器登陆账号，然后打开开发者窗口在控制台输入localStorage.getItem('__token') 
把获取的token粘贴到config.json中的token_full中（注意格式一致）
此token会不定期更新，如果发现登陆进去是白屏，那么需要更新token
> [!IMPORTANT]
> token 的值必须是字符串类型，你必须在所有内部的双引号前加反斜杠（\）进行转义，否则会破坏 JSON 的语法结构
## 🚀 部署与运行 | Getting Started
### 环境要求
 * 操作系统：Windows 10/11
 * 浏览器：Microsoft Edge（必须安装）
 * Python 环境：Python 3.8 或以上版本
### 方式一：小白快速体验 (Exe 直接运行)
如果你不想折腾代码环境，请直接下载打包好的程序：
 1. 前往 Releases 页面。
 2. 下载最新的 .zip 压缩包并解压。
 3. 修改配置（见下文教程）。
 4. 双击运行 UnipusAI_v2.2_plus.exe 即可。
### 方式二：开发者源码运行
如果你想自己修改代码或学习技术原理：
```bash
# 1. 克隆本仓库
git clone [https://github.com/YSJohnson/UnipusAI_Plus.git](https://github.com/YSJohnson/UnipusAI_Plus.git)
cd UnipusAI_Plus

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行主程序
python UnipusAI_v2.2_plus.py

```
## ⚙️ 配置文件说明 | Configuration
在运行程序前，必须配置同级目录下的 config.json 文件（模板如下）。
```json
{
  "url": "[https://u.unipus.cn/user/student](https://u.unipus.cn/user/student)",
  "username": "你的手机号",
  "password": "你的密码",
  "api_key": "你的大模型API_KEY (推荐使用 Kimi/Moonshot)",
  "token_full": "你的U校园Token (用于绕过防作弊)",
  "target_course": "新视野大学英语（第四版）读写教程1",
  "learning_strategy": "learn_all_compulsory_course",
  "base_url": "[https://api.moonshot.cn/v1](https://api.moonshot.cn/v1)",
  "model": "kimi-k2-turbo-preview",
  "temperature": 0.3,
  "max_tokens": 2000,
  "timeout": 10,
  "whisper_api": null
}

```

> [!TIP]
> **🤖 如何获取 api_key？**
> 本项目默认配置为 **Kimi**。请访问 Kimi 开发者平台 注册账号，生成一个全新的 API Key 并填入(无需修改base_url和model)
> 或者任意支持openai接口的大模型api并修改相应的base_url和model
> 
## 🎮 使用指南 | How to Use
由于采用了**半自动模式**，操作流程与原版有本质区别，请仔细阅读：
 1. 1️⃣ **启动程序**：双击运行 exe 或执行 python 脚本。
 2. 2️⃣ **静默登录**：GUI 面板弹出后，程序会在后台自动接管 Edge 浏览器并完成登录、防作弊 Token 注入等操作。此时控制台主按钮处于“未就绪”锁定状态。
 3. 3️⃣ **人工导航**：当控制台提示“🟢 系统已就绪”且主按钮变亮时，**请在自动弹出的 Edge 浏览器中，手动点击进入你想要刷的课程单元和具体题目页面**。
![image](images/2.png)
 4. 4️⃣ **一键秒杀**：页面和题目加载完毕后，切回我们的 Plus 控制台，点击 **【🚀 抓取当前页面并作答】**。
 5. 5️⃣ **等待与继续**：观察控制台日志（此时请勿乱动浏览器鼠标）。作答完毕且自动提交后，系统会发出提示完成。此时你可以继续在浏览器选择要做的页面，然后重复第 4 步操作。
![image](images/3.png)
![image](images/4.png)

## 🙏 致谢 | Acknowledgments
本项目脱胎于优秀的开源土壤。特别感谢原版 UnipusAI 项目的创造者：
 * **原作者**：Zzj-klwgxdz (B站ID: 看了吴钩系钓舟)
 * **原项目地址**：https://github.com/Zzj-klwgxdz/UnipusAI
感谢原作者提供的强大解析器框架与思路，特别是其针对 U校园 极具创造性的防作弊绕过机制。Plus 版沿用了其优秀的依赖注入和基类设计规范。
## 📜 许可证 | License
本项目采用 MIT License 开源许可证。请在合法合规的前提下自由学习与交流。
> [!WARNING]
> ### 🛑 严正声明与合规提醒 (Disclaimer)
> 本项目及所附带的所有代码、可执行文件**仅限用于 Python Selenium 自动化测试技术交流，以及大语言模型 (LLM) API 接入的学术探讨**。
>  * 严禁将本项目用于任何破坏教学公平、恶意刷课、作弊等违反学校或平台规定的行为。
>  * 因滥用本项目引发的一切后果（包括但不限于账号封禁、学分作废、法律责任等），均由使用者**自行承担**，开发者不承担任何直接或间接责任。
>  * **下载、克隆或运行本程序即代表您已同意上述条款。**
> 
