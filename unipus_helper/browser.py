
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

DEFAULT_BROWSER = "edge"


def quit_driver(driver):
    driver.quit()
    profile = getattr(driver, "_unipus_profile_dir", None)
    if profile:
        shutil.rmtree(profile, ignore_errors=True)


@dataclass(frozen=True)
class BrowserSpec:

    key: str
    display_name: str
    profile_dir_name: str
    windows_exe: str = ""
    windows_paths: Tuple[str, ...] = ()
    macos_paths: Tuple[str, ...] = ()
    which_names: Tuple[str, ...] = ()
    installer_url: Optional[str] = None
    manual_install_hint: Tuple[str, ...] = ()
    macos_package: str = ""

    @property
    def supports_auto_install(self) -> bool:
        return bool(self.installer_url) and platform.system() == "Windows"

    def detect_install_path(self) -> Optional[str]:
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

        paths = []
        for raw in self.windows_paths:
            path = os.path.expandvars(raw)
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

_ALIASES = {
    "msedge": "edge",
    "microsoft-edge": "edge",
    "microsoft edge": "edge",
    "google-chrome": "chrome",
    "google chrome": "chrome",
}


def normalize_browser(value: Optional[str]) -> str:
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
    from selenium import webdriver

    if spec.key == "chrome":
        from selenium.webdriver.chrome.service import Service as ChromeService
        return webdriver.ChromeOptions, webdriver.Chrome, ChromeService

    if spec.key == "edge":
        from selenium.webdriver.edge.service import Service as EdgeService
        return webdriver.EdgeOptions, webdriver.Edge, EdgeService

    raise ValueError(f"未在 driver_api 中登记浏览器后端: {spec.key}")
