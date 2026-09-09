import whisper
import tempfile
import os
import traceback
import urllib.error
import urllib.request
from typing import Dict, Optional


class AudioTranscriber:
    """
    使用本地 Whisper 进行语音识别
    """

    def __init__(self):
        self.local_model = None
        self._transcript_cache: Dict[str, str] = {}
        self._init_local_model()

    @staticmethod
    def _print_exception(prefix: str, exc: Exception) -> None:
        """完整输出异常信息与堆栈，方便定位问题"""
        print(f"{prefix}: {exc}")
        tb = traceback.format_exc().rstrip()
        if tb and tb != "NoneType: None":
            print(tb)

    def _init_local_model(self):
        """初始化本地 Whisper 模型"""
        try:
            print("       加载 Whisper 本地模型 (base)...")
            # 可选: tiny, base, small, medium, large
            # base 是速度与准确率的平衡选择
            self.local_model = whisper.load_model("base")
            print("       本地模型加载完成")
        except ImportError:
            print("       未安装 whisper，请运行: pip install openai-whisper")
            raise
        except Exception as e:
            self._print_exception("       加载本地模型失败", e)
            raise

    def transcribe(self, audio_url: str, language: str = "en") -> str:
        """
        下载音频并转录为文字

        Args:
            audio_url: 音频文件URL
            language: 语言代码，默认英语 en，中文 zh

        Returns:
            识别出的文字
        """
        # 检查缓存
        if audio_url in self._transcript_cache:
            print(f"       使用缓存的识别结果")
            return self._transcript_cache[audio_url]

        audio_path = None

        try:
            print(f"      ⬇  下载音频...")
            with urllib.request.urlopen(audio_url, timeout=30) as response, \
                    tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(response.read())
                audio_path = f.name

            print(f"        开始识别...")
            text = self._transcribe_local(audio_path, language)

            if text:
                self._transcript_cache[audio_url] = text
                print(f"       识别成功 ({len(text)} 字符)")
                print(f"       音频识别结果{text}")

            return text or ""

        except urllib.error.URLError as e:
            self._print_exception("       下载音频失败", e)
            return ""
        except Exception as e:
            self._print_exception("       识别失败", e)
            return ""
        finally:
            if audio_path:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass

    def _transcribe_local(self, audio_path: str, language: str) -> Optional[str]:
        """使用本地 Whisper 模型识别"""
        if self.local_model is None:
            print("       本地模型未加载")
            return None

        result = self.local_model.transcribe(
            audio_path,
            language=language,
            fp16=False  # CPU 运行设为 False
        )

        return result["text"].strip() if result else None
