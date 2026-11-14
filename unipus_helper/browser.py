"""浏览器后端规格：环境检测与 driver 启动共用同一份定义。

为什么要单独成文件：过去「检测哪个浏览器」写死在 ``EnvironmentChecker`` 里，
「启动哪个浏览器」写死在 ``UCampusBot`` 里，两边各记一套路径和类名。只改一边就会
出现「门禁放行 Chrome、driver 却起 Edge」这类难定位的错配。现在两边都只读
``BrowserSpec``，增删浏览器只改这一处。
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

DEFAULT_BROWSER = "edge"


@dataclass(frozen=True)
class BrowserSpec:
    """一种浏览器后端的全部差异点。"""

    key: str
    display_name: str
    # --disk-cache-dir 下的子目录名，两种浏览器不能共用
    profile_dir_name: str
    windows_exe: str = ""
    windows_paths: Tuple[str, ...] = ()
    macos_paths: Tuple[str, ...] = ()
    which_names: Tuple[str, ...] = ()
    # 仅 Windows 生效；None 表示这个浏览器只支持手动安装
    installer_url: Optional[str] = None
    manual_install_hint: Tuple[str, ...] = ()
    macos_package: str = ""

    @property
    def supports_auto_install(self) -> bool:
        return bool(self.installer_url) and platform.system() == "Windows"

    def detect_install_path(self) -> Optional[str]:
        """按「Windows 注册表 → 环境变量拼出的固定位置 → PATH 命令名」查找。

        注册表 App Paths 排第一：它记录的是安装器写进去的真实路径，
        版本号变化、装在非默认目录都不影响，也不会因为系统盘符不同而失效。
        """
        registered = self._windows_app_path()
        if registered:
            return registered

        for path in self._platform_paths():
            if path and os.path.exists(path):
                return path

        for command in self.which_names:
            found = shutil.which(command)
            if found:
                return found
        return None

    def _windows_app_path(self) -> Optional[str]:
        """从注册表 App Paths 读取安装位置。

        浏览器装在非默认目录、或恰好赶上自动更新把旧 exe 换掉时，
        固定路径表会误判成「未安装」；注册表是 Windows 上的权威来源。
        """
        if platform.system() != "Windows" or not self.windows_exe:
            return None

        import winreg

        key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{self.windows_exe}"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key_path) as key:
                    path = winreg.QueryValue(key, None)
            except OSError:
                continue
            if path and os.path.exists(path):
                return path
        return None

    def _platform_paths(self) -> Tuple[str, ...]:
        system = platform.system()
        if system != "Windows":
            if system == "Darwin":
                return tuple(os.path.expanduser(path) for path in self.macos_paths)
            return ()

        # Windows 的候选路径全部用环境变量拼，不写死盘符与目录名：
        # 系统盘不是 C:、装了 32/64 位不同版本、用户级安装，都能覆盖。
        paths = []
        for raw in self.windows_paths:
            path = os.path.expandvars(raw)
            # 变量缺失时 expandvars 会原样留下 %NAME%，这种路径肯定不存在；
            # PROGRAMW6432 与 PROGRAMFILES 在 64 位系统上可能指向同一目录，去重。
            if "%" in path or path in paths:
                continue
            paths.append(path)
        return tuple(paths)


EDGE = BrowserSpec(
    key="edge",
    display_name="Microsoft Edge",
    profile_dir_name="edge_cache",
    windows_exe="msedge.exe",
    windows_paths=(
        r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe',
        r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe',
        r'%PROGRAMW6432%\Microsoft\Edge\Application\msedge.exe',
        r'%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe',
    ),
    macos_paths=(
        '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        '~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    ),
    which_names=('msedge', 'microsoft-edge', 'microsoft-edge-stable'),
    installer_url="https://go.microsoft.com/fwlink/?linkid=2108834&Channel=Stable&language=zh-cn",
    manual_install_hint=("https://www.microsoft.com/edge",),
    macos_package="microsoft-edge",
)

# Chrome 只做检测与提示，不提供自动安装：官方安装包源在部分网络下不可达，
# 静默失败比直接让用户手动装更难排查。
CHROME = BrowserSpec(
    key="chrome",
    display_name="Google Chrome",
    profile_dir_name="chrome_cache",
    windows_exe="chrome.exe",
    windows_paths=(
        r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe',
        r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe',
        r'%PROGRAMW6432%\Google\Chrome\Application\chrome.exe',
        r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
    ),
    macos_paths=(
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ),
    which_names=('google-chrome', 'google-chrome-stable', 'chrome'),
    installer_url=None,
    manual_install_hint=("https://www.google.com/chrome/",),
    macos_package="google-chrome",
)

BROWSERS: Dict[str, BrowserSpec] = {spec.key: spec for spec in (EDGE, CHROME)}

# 配置里可能出现的写法，统一收敛到 key
_ALIASES = {
    "msedge": "edge",
    "microsoft-edge": "edge",
    "microsoft edge": "edge",
    "google-chrome": "chrome",
    "google chrome": "chrome",
}


def normalize_browser(value: Optional[str]) -> str:
    """把配置值规整成 BROWSERS 的 key。

    未配置（None / 空串）时静默用默认浏览器；非空但无法识别时给出提示，
    避免手误拼错（如 "chorme"）后静静退回 Edge，用户还以为配的是 Chrome。
    """
    text = (value or "").strip().lower()
    if text in BROWSERS:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    if text:
        print(f" 未知的 browser 配置值 '{value}'，已回退到 {DEFAULT_BROWSER}")
    return DEFAULT_BROWSER


def get_spec(value: Optional[str]) -> BrowserSpec:
    return BROWSERS[normalize_browser(value)]


def driver_api(spec: BrowserSpec):
    """返回 (Options 类, WebDriver 类, Service 类)。

    必须按浏览器显式取类：selenium 的通用 ``Service`` 基类是抽象类，
    同一套类没法同时驱动 Edge 和 Chrome。未知 key 直接抛错，避免新增
    后端时忘了登记就静默落到 Edge。
    """
    from selenium import webdriver

    if spec.key == "chrome":
        from selenium.webdriver.chrome.service import Service as ChromeService
        return webdriver.ChromeOptions, webdriver.Chrome, ChromeService

    if spec.key == "edge":
        from selenium.webdriver.edge.service import Service as EdgeService
        return webdriver.EdgeOptions, webdriver.Edge, EdgeService

    raise ValueError(f"未在 driver_api 中登记浏览器后端: {spec.key}")
