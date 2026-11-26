"""
- 接口地址与 ``AES-128-ECB("1a2b3c4d" + k)`` 解密方案，公开最早见
  ``SSmJaE/MyUserScripts`` 的「U校园答案艹/u艹.js」（2020-06-19）。
- 自签 HS256 JWT + SECRET 的方案，公开最早见 ``Duster-Cule/UnipusHelperPro``
  （MIT License, Copyright (c) 2025 Duster-Cule）的 ``WebUtils.generateAuthToken``；
  本模块的 JWT 生成逻辑与该实现同源，MIT 条款允许在保留本声明的前提下使用。

"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

try:  # requests 是项目运行环境里的既有依赖，这里只做兜底，避免导入期崩溃
    import requests
except Exception:  # pragma: no cover - 仅在缺依赖时触发
    requests = None  # type: ignore[assignment]

#: 第三方还原出来的 HS256 签名密钥；官方换密钥后这里需要同步更新或改用配置项。
DEFAULT_SECRET = "a824b379f126b8b7aa5e33dee83fb0a05aa7462c"
#: 服务端只校验 iss 精确等于该值。
ISSUER = "c4f772063dcfa98e9c50"
DEFAULT_HOST = "https://ucontent.unipus.cn"
#: 服务端只校验 open_id 是 32 位 hex；拿不到真实值时用占位值即可取到答案。
PLACEHOLDER_OPEN_ID = "0" * 32
#: AES 密钥前缀，真正的 key = KEY_PREFIX + k。
KEY_PREFIX = "1a2b3c4d"
CIPHER_PREFIX = "unipus."
#: 单账号最小请求间隔（秒），缓存未命中时才生效。
MIN_REQUEST_INTERVAL = 0.8
#: 网络抖动重试间隔。
RETRY_DELAYS = (1.0, 3.0)

HEX32_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
HEX_SEGMENT_PATTERN = re.compile(r"^[0-9a-f]{12,}$", re.I)
COURSEWARE_NODE_PATTERN = re.compile(r"^[a-zA-Z]\d+[a-zA-Z0-9_-]*$")
ANSWER_API_PATH = "/course/api/v3/answer/{instance}/{node}/default"

CODE_MESSAGES: Dict[int, str] = {
    0: "成功",
    2: "该任务没有答案记录（taskId 可能取成了祖先节点）",
    100100: "用户不存在（openId 不是 32 位十六进制）",
    100200: "请求参数无效（openId 为空）",
}


class AnswerApiError(RuntimeError):
    """答案接口在本机侧失败（解密、依赖缺失等）。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def is_valid_open_id(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX32_PATTERN.match(value.strip()))


def normalize_open_id(value: Any) -> str:
    """格式合法就原样使用，否则退回占位值（服务端只校验格式）。"""
    text = str(value or "").strip()
    return text if is_valid_open_id(text) else PLACEHOLDER_OPEN_ID


def mask_open_id(value: Any) -> str:
    """日志用：只保留前 4 位。"""
    text = str(value or "")
    if not text:
        return "(空)"
    return text[:4] + "…" if len(text) > 4 else "…"


def build_token(open_id: str = PLACEHOLDER_OPEN_ID,
                secret: str = DEFAULT_SECRET,
                issuer: str = ISSUER,
                expires_ms: Optional[int] = None,
                now: Optional[float] = None) -> str:
    """生成 ``x-annotator-auth-token`` 需要的自签 JWT。

    服务端只校验「签名 + iss + open_id 32 位 hex」，``exp`` / ``aud`` /
    ``name`` / ``email`` 均不校验，保留它们只为与现网客户端保持一致。
    """
    if not secret:
        raise AnswerApiError("答案接口签名密钥为空")
    clock = time.time() if now is None else float(now)
    ttl = 31536000000 if expires_ms is None else int(expires_ms)
    header = {"typ": "JWT", "alg": "HS256"}
    payload = {
        "open_id": normalize_open_id(open_id),
        "iss": issuer,
        "name": "",
        "email": "",
        "administrator": False,
        "exp": int(clock * 1000) + ttl,
        "aud": "edx.unipus.cn",
    }
    signing = ".".join((
        _b64u(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ))
    signature = hmac.new(secret.encode("utf-8"), signing.encode("utf-8"),
                         hashlib.sha256).digest()
    return f"{signing}.{_b64u(signature)}"


def parse_player_url(url: str) -> Tuple[str, str]:
    """从播放器地址里取出 ``(courseInstanceId, taskId)``。

    - ``courseInstanceId``：hash 里第一个以 ``course-`` 开头的段（也兼容查询串）；
    - ``taskId``：hash 里最后一个 12 位以上的 hex 段。
    """
    text = str(url or "").strip()
    if not text:
        raise ValueError("播放器地址为空")
    parts = urlsplit(text)
    fragment = unquote(parts.fragment or "")
    frag_path = fragment.split("?", 1)[0]
    segments = [seg for seg in re.split(r"[/?#]", frag_path) if seg]

    instance = next((seg for seg in segments if seg.startswith("course-")), "")
    if not instance:
        query = parse_qs(parts.query)
        for key in ("courseInstanceId", "course_instance_id", "courseId"):
            for value in query.get(key, []):
                if value.startswith("course-"):
                    instance = value
                    break
            if instance:
                break
    if not instance:
        raise ValueError("地址里找不到课程实例 id（course-…）")

    hexes = [seg for seg in segments if HEX_SEGMENT_PATTERN.match(seg)]
    if hexes:
        return instance, hexes[-1]

    if "courseware" in segments:
        tail = segments[segments.index("courseware") + 1:]
        nodes = [seg for seg in tail if COURSEWARE_NODE_PATTERN.match(seg)]
        if nodes:
            return instance, nodes[-1]

    raise ValueError("地址里找不到 taskId（12 位以上的 hex 段）")


def open_id_from_token(token: str) -> Optional[str]:
    """从平台 ``jwt`` cookie 的值里解出 ``openId``。"""
    text = unquote(str(token or "").strip())
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    parts = text.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    for key in ("openId", "open_id", "openid", "openID"):
        value = claims.get(key)
        if is_valid_open_id(value):
            return value.strip()
    return None


def read_open_id(driver: Any) -> Optional[str]:
    """从 Selenium 会话的 ``jwt`` cookie 里取 openId（拿不到返回 None）。"""
    if driver is None:
        return None
    try:
        cookie = driver.get_cookie("jwt")
    except Exception:
        return None
    if not cookie:
        return None
    return open_id_from_token((cookie or {}).get("value") or "")


def load_aes():
    """惰性加载 AES，未装依赖时给出可执行的提示。"""
    try:
        from Crypto.Cipher import AES  # type: ignore
        return AES
    except Exception as exc:  # pragma: no cover - 仅在缺依赖时触发
        raise AnswerApiError(
            "缺少 pycryptodome 依赖，无法解密答案，请运行 `pip install -r requirements.txt` 安装"
        ) from exc


def decrypt_answers(data: str, k: str) -> List[Dict[str, Any]]:
    """解密 ``data`` 字段，返回大题数组。"""
    if not isinstance(data, str) or not data.startswith(CIPHER_PREFIX):
        raise AnswerApiError("答案密文格式不正确（缺少 unipus. 前缀）")
    key = (KEY_PREFIX + str(k or "")).encode("utf-8")
    if len(key) != 16:
        raise AnswerApiError(f"AES 密钥长度异常（{len(key)} 字节），k={k!r}")
    try:
        raw = bytes.fromhex(data[len(CIPHER_PREFIX):])
    except ValueError as exc:
        raise AnswerApiError("答案密文不是合法 hex") from exc
    AES = load_aes()
    plain = AES.new(key, AES.MODE_ECB).decrypt(raw).rstrip(b"\x00")
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnswerApiError("答案明文不是合法 JSON（k 可能已轮换）") from exc
    if not isinstance(payload, list):
        raise AnswerApiError("答案明文不是数组")
    return [item for item in payload if isinstance(item, dict)]


def child_answers(item: Dict[str, Any]) -> Optional[List[List[str]]]:
    """把一个大题展平成「每个小题的答案值数组」。

    ``answer`` 是字符串里的 JSON（双层编码）；小题答案在
    ``answer.children[].answers``。主观题该字段为 ``null``，这里返回空数组。
    解析失败返回 ``None``，调用方据此放弃该大题。
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("answer")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        payload = {}
    if not isinstance(payload, dict):
        return None
    children = payload.get("children")
    if children is None:
        return []
    if not isinstance(children, list):
        return None
    result: List[List[str]] = []
    for child in children:
        if not isinstance(child, dict):
            return None
        values = child.get("answers")
        if values is None:
            result.append([])
        elif isinstance(values, str):
            result.append([values.strip()] if values.strip() else [])
        elif isinstance(values, (list, tuple)):
            cleaned = [str(value).strip() for value in values
                       if value is not None and str(value).strip()]
            result.append(cleaned)
        else:
            result.append([str(values).strip()])
    return result


def flatten_items(items: Sequence[Dict[str, Any]]) -> List[Optional[str]]:
    """按页面小题顺序展平成答案列表；主观题为 ``None``。"""
    flat: List[Optional[str]] = []
    for item in items or []:
        values = child_answers(item)
        if values is None:
            continue
        for answers in values:
            flat.append(answers[0] if answers else None)
    return flat


def is_objective_answers(values: Optional[Sequence[Sequence[str]]]) -> bool:
    """整道大题都是客观题（每个小题都有值）才算可用。"""
    if not values:
        return False
    return all(bool(answer) for answer in values)


# --------------------------------------------------------------------------- #
# 网络请求
# --------------------------------------------------------------------------- #
def _session(session: Any = None):
    if session is not None:
        return session
    if requests is None:  # pragma: no cover - 仅在缺依赖时触发
        raise AnswerApiError("缺少 requests 依赖，无法访问答案接口")
    return requests


def fetch_answers(instance: str, node: str,
                  open_id: str = PLACEHOLDER_OPEN_ID,
                  *,
                  secret: str = DEFAULT_SECRET,
                  host: str = DEFAULT_HOST,
                  timeout: float = 30.0,
                  session: Any = None,
                  retries: int = 2,
                  sleep=None,
                  log=None) -> Dict[str, Any]:
    """请求并解密答案。

    返回统一字典：成功 ``{"ok": True, "code": 0, "k", "items", "flat", ...}``；
    失败 ``{"ok": False, "code", "message"}``（``code`` 为业务码或 HTTP 状态码）。
    """
    instance = str(instance or "").strip()
    node = str(node or "").strip()
    if not instance or not node:
        return {"ok": False, "code": -1, "message": "课程实例或任务节点为空"}
    boss = _session(session)
    url = f"{host.rstrip('/')}" + ANSWER_API_PATH.format(instance=instance, node=node)
    headers = {
        "x-annotator-auth-token": build_token(open_id, secret=secret),
        "Accept": "application/json",
    }
    nap = sleep if sleep is not None else time.sleep
    attempts = max(0, int(retries)) + 1
    last_error = ""
    for attempt in range(attempts):
        try:
            response = boss.get(url, headers=headers, timeout=timeout)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 200:
                try:
                    body = response.json()
                except Exception:
                    return {"ok": False, "code": status, "message": "响应不是 JSON"}
                if not isinstance(body, dict):
                    return {"ok": False, "code": status, "message": "响应结构异常"}
                code = body.get("code")
                if code != 0:
                    return {"ok": False, "code": code,
                            "message": body.get("message") or CODE_MESSAGES.get(code, "未知错误")}
                k = str(body.get("k") or "")
                try:
                    items = decrypt_answers(str(body.get("data") or ""), k)
                except AnswerApiError as exc:
                    return {"ok": False, "code": code, "message": str(exc), "k": k}
                return {
                    "ok": True,
                    "code": 0,
                    "k": k,
                    "publish_version": body.get("publish_version"),
                    "items": items,
                    "flat": flatten_items(items),
                }
            last_error = f"HTTP {status}"
            if status in (400, 401, 403, 404):
                return {"ok": False, "code": status, "http_status": status,
                        "message": f"HTTP {status}（签名、iss 或任务标识不正确）"}
            if status < 500:
                return {"ok": False, "code": status, "http_status": status,
                        "message": f"HTTP {status}"}
        if attempt < attempts - 1:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            if log:
                log(f"    答案接口请求失败（{last_error}），{delay:.0f}s 后重试")
            nap(delay)
    return {"ok": False, "code": -1, "message": f"请求失败：{last_error}"}


# --------------------------------------------------------------------------- #
# 本地缓存
# --------------------------------------------------------------------------- #
def default_cache_path() -> Path:
    try:
        from .paths import PROJECT_ROOT  # 延迟导入，便于单独测试本模块
        root = Path(PROJECT_ROOT)
    except Exception:
        root = Path(__file__).resolve().parent.parent
    return root / ".cache" / "answer_api.json"


def today_stamp(moment: Optional[float] = None) -> str:
    clock = time.time() if moment is None else float(moment)
    return datetime.fromtimestamp(clock).strftime("%Y%m%d")


def task_key(instance: str, node: str) -> str:
    return f"{instance}|{node}"


class AnswerCache:
    """按 ``(courseInstanceId, taskId)`` 缓存解密后的答案。

    答案在同一 ``k`` 周期（当天）内不变，所以缓存只在 ``k`` 等于当天日期时命中；
    跨天、解析失败或文件损坏一律当作未命中，绝不因为缓存拖住任务。
    """

    VERSION = 1

    def __init__(self, path: Optional[Any] = None, *, enabled: bool = True):
        self.path = Path(path) if path is not None else Path(default_cache_path())
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        if not self.enabled:
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return {}
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _write(self, entries: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump({"version": self.VERSION, "entries": entries}, handle,
                          ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink()  # type: ignore[possibly-undefined]
            except Exception:
                pass

    def get(self, instance: str, node: str, *, k: Optional[str] = None,
            moment: Optional[float] = None) -> Optional[Dict[str, Any]]:
        stamp = k or today_stamp(moment)
        with self._lock:
            entry = self._read().get(task_key(instance, node))
        if not isinstance(entry, dict):
            return None
        if str(entry.get("k") or "") != stamp:
            return None
        items = entry.get("items")
        if not isinstance(items, list) or not items:
            return None
        return entry

    def put(self, instance: str, node: str, *, k: str,
            items: Sequence[Dict[str, Any]], publish_version: Any = None) -> None:
        with self._lock:
            entries = self._read()
            entries[task_key(instance, node)] = {
                "k": str(k or ""),
                "publish_version": publish_version,
                "ts": int(time.time()),
                "items": list(items),
            }
            self._write(entries)

    def clear(self) -> bool:
        """清空缓存文件（诊断用）；缓存被关闭时不做任何事。"""
        if not self.enabled:
            return False
        with self._lock:
            self._write({})
        return True


# --------------------------------------------------------------------------- #
# 高层门面：按任务准备 + 大题对齐取用
# --------------------------------------------------------------------------- #
def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return default
    return text not in ("0", "false", "no", "off", "disabled", "disable")


class OfficialAnswerProvider:
    """把答案接口接到现有答题流程上的一层门面。

    用法：``prepare(url, driver)`` 一次，随后每个题目组用
    ``peek_aligned(小题数)`` 拿答案、``consume(index)`` 确认消费；
    任何一步不满足就返回 ``None``，调用方照常走 AI。
    """

    def __init__(self, config: Any = None, *,
                 cache: Optional[AnswerCache] = None,
                 session: Any = None,
                 log: Any = None,
                 sleep: Any = None,
                 clock: Any = None):
        self.config = config
        self.enabled = _truthy(getattr(config, "answer_api_enabled", True))
        self.secret = (str(getattr(config, "answer_api_secret", "") or "").strip()
                       or DEFAULT_SECRET)
        self.host = (str(getattr(config, "answer_api_host", "") or "").strip()
                     or DEFAULT_HOST)
        try:
            self.timeout = float(getattr(config, "answer_api_timeout", 30) or 30)
        except (TypeError, ValueError):
            self.timeout = 30.0
        self.timeout = min(120.0, max(5.0, self.timeout))
        self.log = log if log is not None else (lambda message: print(message))
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.time
        self._session = session
        self._configured_open_id = str(getattr(config, "answer_api_open_id", "") or "").strip()
        self.cache = cache if cache is not None else AnswerCache(
            enabled=_truthy(getattr(config, "answer_api_cache", True)))

        self.instance = ""
        self.node = ""
        self.task_url = ""
        self.k = ""
        self.publish_version: Any = None
        self.items: List[Dict[str, Any]] = []
        self.flat: List[Optional[str]] = []
        self.open_id = ""
        self.open_id_source = ""
        self.active = False
        self.abandoned = False
        self.reason = "尚未准备" if self.enabled else "已在配置里关闭官方答案接口"
        self._cursor = 0
        self._last_request = 0.0
        self.stats = {"requests": 0, "cache_hits": 0, "used": 0, "fallback": 0}

    # -- 状态 ------------------------------------------------------------- #
    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def remaining(self) -> int:
        return max(0, len(self.flat) - self._cursor) if self.flat else 0

    @property
    def task_key(self) -> str:
        return task_key(self.instance, self.node) if self.instance and self.node else ""

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "abandoned": self.abandoned,
            "reason": self.reason,
            "instance": self.instance,
            "node": self.node,
            "k": self.k,
            "items": len(self.items),
            "flat": len(self.flat),
            "cursor": self._cursor,
            "open_id": mask_open_id(self.open_id),
            "open_id_source": self.open_id_source,
            "stats": dict(self.stats),
        }

    # -- 准备 ------------------------------------------------------------- #
    def _resolve_open_id(self, driver: Any) -> None:
        if is_valid_open_id(self._configured_open_id):
            self.open_id = self._configured_open_id.strip()
            self.open_id_source = "config"
            return
        from_cookie = read_open_id(driver)
        if from_cookie:
            self.open_id = from_cookie
            self.open_id_source = "cookie"
            return
        self.open_id = PLACEHOLDER_OPEN_ID
        self.open_id_source = "placeholder"

    def _throttle(self) -> None:
        elapsed = self._clock() - self._last_request
        if 0 < elapsed < MIN_REQUEST_INTERVAL:
            self._sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = self._clock()

    def prepare(self, url: Optional[str] = None, driver: Any = None,
                candidates: Optional[Sequence[str]] = None) -> bool:
        """定位当前任务并拉取答案；失败只记录原因，不抛异常。

        ``candidates`` 用于补充候选地址（例如新版播放器外壳地址解析不出
        taskId 时，可把内容 iframe 的 src 一起传进来逐个尝试）。
        """
        self.reset()
        if not self.enabled:
            self.reason = "已在配置里关闭官方答案接口"
            return False

        urls: List[str] = [str(url or "").strip()]
        if driver is not None:
            try:
                urls.append(str(driver.current_url or ""))
            except Exception:
                pass
        urls.extend(str(item or "").strip() for item in (candidates or []))

        instance = node = ""
        raw_url = ""
        parse_error = ""
        for candidate in urls:
            if not candidate:
                continue
            try:
                instance, node = parse_player_url(candidate)
            except ValueError as exc:
                parse_error = parse_error or str(exc)
                continue
            raw_url = candidate
            break
        if not (instance and node):
            self.reason = f"未能从当前地址解析任务：{parse_error or '地址为空'}"
            return False

        self.instance, self.node, self.task_url = instance, node, raw_url
        self._resolve_open_id(driver)

        cached = self.cache.get(instance, node)
        if cached:
            self.items = [item for item in (cached.get("items") or []) if isinstance(item, dict)]
            self.flat = flatten_items(self.items)
            self.k = str(cached.get("k") or "")
            self.publish_version = cached.get("publish_version")
            self.active = bool(self.items)
            self.reason = "命中本地缓存" if self.active else "缓存内容为空"
            self.stats["cache_hits"] += 1
            if self.active:
                self.log(f"    官方答案已就绪（命中缓存）：{len(self.items)} 个大题 / "
                         f"{len(self.flat)} 个小题")
            return self.active

        self._throttle()
        self.stats["requests"] += 1
        result = fetch_answers(instance, node, self.open_id, secret=self.secret,
                               host=self.host, timeout=self.timeout,
                               session=self._session, log=self.log,
                               sleep=self._sleep)
        if not result.get("ok"):
            self.reason = str(result.get("message") or "答案接口未返回答案")
            self.active = False
            return False

        self.items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
        self.flat = list(result.get("flat") or [])
        self.k = str(result.get("k") or "")
        self.publish_version = result.get("publish_version")
        self.active = bool(self.items)
        self.reason = "已取到答案" if self.active else "接口返回空答案"
        if self.active:
            self.cache.put(instance, node, k=self.k, items=self.items,
                           publish_version=self.publish_version)
            self.log(f"    官方答案已就绪：{len(self.items)} 个大题 / "
                     f"{len(self.flat)} 个小题")
        return self.active

    # -- 取答案 ----------------------------------------------------------- #
    def answers_at(self, index: int) -> Optional[List[List[str]]]:
        if not self.active or index < 0 or index >= len(self.items):
            return None
        return child_answers(self.items[index])

    def peek_aligned(self, child_count: int, lookahead: int = 2,
                     ) -> Optional[Tuple[int, List[List[str]]]]:
        """按「小题数一致」在游标附近找可用大题，不推进游标。"""
        if not self.active or self.abandoned or child_count <= 0:
            return None
        limit = min(len(self.items), self._cursor + max(0, int(lookahead)) + 1)
        for index in range(self._cursor, limit):
            values = child_answers(self.items[index])
            if values is None or len(values) != child_count:
                continue
            return index, values
        return None

    def answers_for(self, group_index: Optional[int], child_count: int,
                    lookahead: int = 2) -> Optional[Tuple[int, List[List[str]]]]:
        """优先按大题序号直取；序号对不上时再按游标附近的小题数对齐。"""
        if not self.active or self.abandoned or child_count <= 0:
            return None
        if group_index is not None and int(group_index) >= 0:
            values = self.answers_at(int(group_index))
            if values is not None and len(values) == child_count \
                    and is_objective_answers(values):
                return int(group_index), values
        return self.peek_aligned(child_count, lookahead)

    def consume(self, index: int) -> None:
        """确认使用第 ``index`` 个大题：游标后移并记一次命中。"""
        self._cursor = max(self._cursor, int(index) + 1)
        self.stats["used"] += 1

    def mark_fallback(self) -> None:
        self.stats["fallback"] += 1

    def abandon(self, reason: str = "") -> None:
        """本任务后续不再使用官方答案（避免游标错位导致错填）。"""
        self.abandoned = True
        if reason:
            self.reason = reason
            self.log(f"    官方答案接口本任务停用：{reason}；剩余题目交回 AI")

    def reset(self) -> None:
        self.instance = ""
        self.node = ""
        self.task_url = ""
        self.k = ""
        self.publish_version = None
        self.items = []
        self.flat = []
        self.open_id = ""
        self.open_id_source = ""
        self.active = False
        self.abandoned = False
        self._cursor = 0

    # -- 诊断 ------------------------------------------------------------- #
    def describe(self) -> str:
        if not self.enabled:
            return "官方答案接口已关闭"
        if not self.active:
            return f"官方答案接口未命中（{self.reason}）"
        return (f"官方答案接口就绪：{self.instance}/{self.node}，"
                f"{len(self.items)} 个大题，游标 {self._cursor}，k={self.k}")


# --------------------------------------------------------------------------- #
# 命令行诊断
# --------------------------------------------------------------------------- #
def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m unipus_helper.answer_api",
        description="直接查询 U校园客观题答案（只读，用于联调与排障）")
    parser.add_argument("--url", help="播放器地址（含 course-… 与 taskId 的 hash）")
    parser.add_argument("--instance", help="courseInstanceId，如 course-v2:xxx")
    parser.add_argument("--node", help="taskId，如 6629e3aae0010bf")
    parser.add_argument("--open-id", default="", help="32 位 hex 的 openId；留空用占位值")
    parser.add_argument("--no-cache", action="store_true", help="跳过本地缓存")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    instance, node = (args.instance or "").strip(), (args.node or "").strip()
    if args.url and not (instance and node):
        try:
            instance, node = parse_player_url(args.url)
        except ValueError as exc:
            print(f"解析地址失败：{exc}")
            return 2
    if not (instance and node):
        parser.print_help()
        return 2

    cache = AnswerCache(enabled=not args.no_cache)
    cached = None if args.no_cache else cache.get(instance, node)
    if cached:
        items, k, version = cached.get("items") or [], cached.get("k"), cached.get("publish_version")
    else:
        result = fetch_answers(instance, node, args.open_id or PLACEHOLDER_OPEN_ID)
        if not result.get("ok"):
            print(f"接口失败：code={result.get('code')} {result.get('message')}")
            return 1
        items, k, version = result["items"], result["k"], result.get("publish_version")
        cache.put(instance, node, k=k, items=items, publish_version=version)

    if args.json:
        print(json.dumps({"instance": instance, "node": node, "k": k,
                          "publish_version": version, "items": items},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"课程实例: {instance}")
    print(f"任务节点: {node}  (k={k}, publish_version={version})")
    for index, item in enumerate(items):
        values = child_answers(item) or []
        print(f"  大题 {index + 1}: {len(values)} 个小题")
        for number, answers in enumerate(values, 1):
            print(f"    小题 {number}: {answers[0] if answers else '<无答案，走 AI>'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
