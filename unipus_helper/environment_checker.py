import os
import platform
import tempfile
import shutil
import subprocess
import zipfile
import urllib.request
from typing import Optional, List

from .browser import BrowserSpec, DEFAULT_BROWSER, get_spec


class EnvironmentChecker:

    FFMPEG_DOWNLOAD_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

    def __init__(self, browser: str = DEFAULT_BROWSER):
        self.issues: List[str] = []
        self.warnings: List[str] = []
        self.ffmpeg_path: Optional[str] = None
        self.ffprobe_path: Optional[str] = None
        self.browser: BrowserSpec = get_spec(browser)

    def check_all(self) -> bool:
        print(" 正在检查运行环境...\n")

        self._check_system()

        self._check_browser()

        self._check_ffmpeg()

        if self.issues:
            print("\n" + "=" * 60)
            print(" 发现以下问题需要修复：")
            print("=" * 60)
            for issue in self.issues:
                print(f"   {issue}")

            if self.warnings:
                print("\n  警告（可忽略）：")
                for warning in self.warnings:
                    print(f"   {warning}")

            return False

        if self.warnings:
            print("\n  警告（可忽略）：")
            for warning in self.warnings:
                print(f"   {warning}")

        print("\n 环境检查通过！\n")
        return True

    def _check_system(self):
        print(f"   操作系统: {platform.system()} {platform.release()}")
        print(f"   架构: {platform.machine()}")
        print(f"   Python: {platform.python_version()}")

        if platform.system() not in ("Windows", "Darwin"):
            self.warnings.append(
                f"非 Windows / macOS 系统，可能无法正常使用 {self.browser.display_name}")

    def _check_browser(self):
        path = self.browser.detect_install_path()
        if path:
            print(f"    {self.browser.display_name}: {path}")
            return

        self.issues.append(f" 未检测到 {self.browser.display_name} 浏览器")

        if self.browser.supports_auto_install:
            return
        self.issues.append("   → 请手动安装：" + " / ".join(self.browser.manual_install_hint))


    def _check_ffmpeg(self):
        print("   检查 FFmpeg...")

        ffmpeg_in_path = shutil.which("ffmpeg")
        if ffmpeg_in_path:
            self.ffmpeg_path = ffmpeg_in_path
            self.ffprobe_path = self._resolve_ffprobe_path(ffmpeg_in_path, prefer_path_lookup=True)
            version = self._get_ffmpeg_version(ffmpeg_in_path)
            print(f"       FFmpeg 已添加到 PATH: {version}")
            print(f"         FFmpeg 路径: {ffmpeg_in_path}")
            if self.ffprobe_path:
                print(f"         FFprobe 路径: {self.ffprobe_path}")
            else:
                print("         FFprobe 路径: 未检测到")
            return

        common_paths = [
            r'C:\ffmpeg\bin\ffmpeg.exe',
            r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
            r'C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe',
            os.path.expandvars(r'%LOCALAPPDATA%\ffmpeg\bin\ffmpeg.exe'),
            os.path.expandvars(r'%USERPROFILE%\ffmpeg\bin\ffmpeg.exe'),
            os.path.expandvars(r'%SystemDrive%\ffmpeg\bin\ffmpeg.exe'),
        ]

        for path in common_paths:
            if os.path.exists(path):
                self.ffmpeg_path = path
                self.ffprobe_path = self._resolve_ffprobe_path(path)
                version = self._get_ffmpeg_version(path)
                print(f"        FFmpeg 已安装但未添加到 PATH: {version}")
                print(f"         FFmpeg 路径: {path}")
                if self.ffprobe_path:
                    print(f"         FFprobe 路径: {self.ffprobe_path}")
                else:
                    print("         FFprobe 路径: 未检测到")
                self.issues.append("  FFmpeg 已安装但未添加到系统环境变量 PATH")
                return

        self.issues.append(" 未检测到 FFmpeg（语音识别需要）")

    def _get_ffmpeg_version(self, ffmpeg_path: str) -> str:
        try:
            result = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            first_line = result.stdout.split('\n')[0]
            version = first_line.split()[2] if len(first_line.split()) > 2 else "未知版本"
            return version
        except:
            return "未知版本"

    def _resolve_ffprobe_path(self, ffmpeg_path: str, prefer_path_lookup: bool = False) -> Optional[str]:
        if prefer_path_lookup:
            ffprobe_in_path = shutil.which("ffprobe")
            if ffprobe_in_path:
                return ffprobe_in_path

        ffmpeg_dir = os.path.dirname(ffmpeg_path)
        if ffmpeg_dir:
            probe_name = "ffprobe.exe" if platform.system() == "Windows" else "ffprobe"
            candidate = os.path.join(ffmpeg_dir, probe_name)
            if os.path.exists(candidate):
                return candidate

        return None


    def show_fix_guide(self):
        install_option = (f"自动下载并安装 {self.browser.display_name} 浏览器"
                          if self.browser.supports_auto_install
                          else f"查看 {self.browser.display_name} 手动安装说明")

        print("\n" + "=" * 60)
        print(" 自动修复选项")
        print("=" * 60)
        print("\n请选择操作：")
        print(f"   [1] {install_option}")
        print("   [2] 自动下载并安装 FFmpeg（推荐）")
        print("   [3] 将 FFmpeg 添加到系统 PATH（如已安装）")
        print("   [4] 手动指定 FFmpeg 路径")
        print("   [5] 显示详细帮助后退出")
        print("   [Q] 退出程序")
        if platform.system() != "Windows":
            print("\n   macOS / Linux 提示：下面 [1][2][3] 是 Windows 专用流程，")
            print("   建议直接用包管理器安装，装完重跑本程序即可：")
            if self.browser.macos_package:
                print(f"     macOS   brew install --cask {self.browser.macos_package}")
            print("     macOS   brew install ffmpeg")
            print("     Ubuntu  sudo apt install ffmpeg   # 浏览器请参考官网 .deb")

        choice = input("\n请输入选项 (1/2/3/4/5/Q): ").strip().upper()
        return choice


    def auto_install_ffmpeg(self) -> bool:
        if platform.system() != "Windows":
            print("\n  当前系统不是 Windows，自动安装流程只适用于 Windows，改用包管理器：")
            print("     macOS:  brew install ffmpeg")
            print("     Ubuntu: sudo apt install ffmpeg")
            return False
        print("\n 正在下载 FFmpeg...")
        print("   来源: https://github.com/BtbN/FFmpeg-Builds")

        try:
            zip_path = os.path.join(tempfile.gettempdir(), "ffmpeg.zip")

            def download_progress(block_num, block_size, total_size):
                downloaded = block_num * block_size
                percent = min(100, downloaded * 100 / total_size)
                print(f"\r   进度: {percent:.1f}% ({downloaded // 1024 // 1024}MB / {total_size // 1024 // 1024}MB)",
                      end="")

            print("   下载中（约 130MB，请耐心等待）...")
            urllib.request.urlretrieve(
                self.FFMPEG_DOWNLOAD_URL,
                zip_path,
                reporthook=download_progress
            )
            print()

            print("    解压文件...")
            extract_dir = os.path.expandvars(r'%LOCALAPPDATA%\ffmpeg')

            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)

            os.makedirs(extract_dir, exist_ok=True)

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)

            bin_dir = None
            for root, dirs, files in os.walk(extract_dir):
                if 'ffmpeg.exe' in files and 'bin' in root:
                    bin_dir = root
                    break

            if not bin_dir:
                subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
                if subdirs:
                    potential_bin = os.path.join(extract_dir, subdirs[0], 'bin')
                    if os.path.exists(os.path.join(potential_bin, 'ffmpeg.exe')):
                        bin_dir = potential_bin

            if not bin_dir:
                print("    解压后未找到 ffmpeg.exe")
                return False

            print("    添加到系统环境变量...")
            self._add_to_system_path(bin_dir)

            print("    验证安装...")
            os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')

            ffmpeg_exe = os.path.join(bin_dir, 'ffmpeg.exe')
            ffprobe_exe = os.path.join(bin_dir, 'ffprobe.exe')
            version = self._get_ffmpeg_version(ffmpeg_exe)
            print(f"    FFmpeg 安装成功: {version}")
            print(f"      FFmpeg 路径: {ffmpeg_exe}")
            if os.path.exists(ffprobe_exe):
                print(f"      FFprobe 路径: {ffprobe_exe}")
            else:
                print("      FFprobe 路径: 未检测到")

            try:
                os.remove(zip_path)
            except:
                pass

            print("\n  请重新运行本程序以加载新的环境变量")
            input("按回车键退出...")
            return True

        except Exception as e:
            print(f"\n    安装失败: {e}")
            print("    请手动下载: https://ffmpeg.org/download.html")
            return False

    def add_ffmpeg_to_path(self) -> bool:
        if not self.ffmpeg_path:
            print("    未检测到已安装的 FFmpeg")
            return False

        bin_dir = os.path.dirname(self.ffmpeg_path)

        print(f"\n 正在添加到系统 PATH...")
        print(f"   路径: {bin_dir}")

        try:
            self._add_to_system_path(bin_dir)
            print("    添加成功")
            print(f"    FFmpeg 路径: {self.ffmpeg_path}")
            ffprobe_path = self._resolve_ffprobe_path(self.ffmpeg_path)
            if ffprobe_path:
                print(f"    FFprobe 路径: {ffprobe_path}")
            else:
                print("    FFprobe 路径: 未检测到")
            print("\n  请重新运行本程序以加载新的环境变量")
            input("按回车键退出...")
            return True
        except Exception as e:
            print(f"    添加失败: {e}")
            return False

    def _add_to_system_path(self, bin_dir: str):
        if platform.system() != "Windows":
            print("    非 Windows 平台请把下面这行加进 ~/.zshrc 或 ~/.bash_profile：")
            print(f'      export PATH="{bin_dir}:$PATH"')
            return
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE
        )

        try:
            current_path, _ = winreg.QueryValueEx(key, "Path")
        except:
            current_path = ""

        paths = [p.strip() for p in current_path.split(os.pathsep) if p.strip()]
        if bin_dir in paths:
            print("   ℹ  该路径已在 PATH 中")
            return

        new_path = bin_dir + os.pathsep + current_path if current_path else bin_dir
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)

        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result)
        )

        print("    系统环境变量已更新")


    def auto_install_browser(self) -> bool:
        if not self.browser.installer_url:
            print(f"\n  {self.browser.display_name} 需要手动安装：")
            for hint in self.browser.manual_install_hint:
                print(f"     {hint}")
            print("   安装完成后重新运行本程序即可。")
            return False

        if platform.system() != "Windows":
            print("\n  当前系统不是 Windows，这里下载的是 Windows 安装包，改用包管理器：")
            if self.browser.macos_package:
                print(f"     macOS: brew install --cask {self.browser.macos_package}")
            return False

        print(f"\n 正在下载 {self.browser.display_name} 安装程序...")
        try:
            installer_name = f"{self.browser.key.title()}Setup.exe"
            installer_path = os.path.join(tempfile.gettempdir(), installer_name)
            urllib.request.urlretrieve(self.browser.installer_url, installer_path)
            print(f"    下载完成")
            print("    启动安装程序...")
            subprocess.Popen([installer_path], shell=True)
            print(f"\n  请完成 {self.browser.display_name} 安装后，重新运行本程序")
            input("按回车键退出...")
            return False
        except Exception as e:
            print(f"    下载失败: {e}")
            return False

    def manual_specify_path(self) -> Optional[str]:
        print("\n" + "-" * 60)
        print("手动指定路径")
        print("-" * 60)

        ffmpeg_path = input("ffmpeg.exe 路径（直接回车跳过）: ").strip()
        if ffmpeg_path and not os.path.exists(ffmpeg_path):
            print("    路径不存在")
            ffmpeg_path = None

        return ffmpeg_path
