# -*- coding: utf-8 -*-
"""
AstrBot 插件：学习通 DDL 提醒

功能：
  - 定时登录学习通抓取未交作业（可选考试）的截止时间
  - 截止前 N 天（可配置多项）通过当前聊天平台主动推送提醒，每条只推一次
  - 每日固定时刻（可选）推送「未交作业一览」
  - 登录方式可选：账号密码 / 手机短信验证码 / Cookie，命令 cxlogin 交互选择
  - 网页登录遇到图形验证码时，把验证码图片推到聊天窗口，回复 cxcode 验证码 即可
  - 可零配置使用：WebUI 留空也能用，账号在聊天里用 cxlogin / cxsetphone / cxpwd 设置
  - 指令：作业 / 课程 / cxlogin / cxsetphone / cxpwd / cxaccount / cxcode / cxsetcookie / cxbind / cxunbind / cxcheck / cxhelp

登录态、提醒状态持久化在 data/plugin_data/astrbot_plugin_chaoxing_ddl/data.json 中，重启、重装插件均不丢失。
"""

import asyncio
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import filter, MessageChain, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 按「文件路径」加载同目录的 chaoxing_api.py，而不是 from chaoxing_api import。
# 原因：AstrBot 用 importlib 按路径加载插件，若用裸模块名导入，会占用 sys.modules
# 里的全局名字 "chaoxing_api"。热重载 / 覆盖安装时旧的缓存模块不会被清掉，
# 于是新 main.py 会拿到旧模块，表现为 ImportError: cannot import name 'XXX'。
_API_NAME = "astrbot_plugin_chaoxing_ddl.chaoxing_api"
_API_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chaoxing_api.py")
sys.modules.pop(_API_NAME, None)  # 丢掉可能残留的旧版本
_spec = importlib.util.spec_from_file_location(_API_NAME, _API_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"无法加载学习通接口模块: {_API_PATH}")
api = importlib.util.module_from_spec(_spec)
sys.modules[_API_NAME] = api
_spec.loader.exec_module(api)

ChaoxingClient = api.ChaoxingClient
UNFINISHED_STATUS = api.UNFINISHED_STATUS  # 与接口侧共用的「未完成」状态表
LOGIN_MODE_AUTO = api.LOGIN_MODE_AUTO
LOGIN_MODE_COOKIE = api.LOGIN_MODE_COOKIE
LOGIN_MODE_PASSWORD = api.LOGIN_MODE_PASSWORD
LOGIN_MODE_SMS = api.LOGIN_MODE_SMS

# ---------- 持久化目录 ----------
# 按 AstrBot 上架规范，持久化数据必须写在 data/plugin_data/<plugin_name>/，
# 不能写在插件自身目录（重装/更新会丢数据）。
_PLUGIN_NAME = "astrbot_plugin_chaoxing_ddl"


def _resolve_data_dir() -> str:
    """优先用 AstrBot 官方 API 拿插件数据目录，失败时按约定路径兜底"""
    try:
        from astrbot.api.star import StarTools

        return str(StarTools.get_data_dir(_PLUGIN_NAME))
    except Exception:
        return os.path.join("data", "plugin_data", _PLUGIN_NAME)


_DATA_DIR = _resolve_data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)
_DATA_FILE = os.path.join(_DATA_DIR, "data.json")
_CAPTCHA_FILE = os.path.join(_DATA_DIR, "captcha.png")

_MODE_LABELS = {
    LOGIN_MODE_AUTO: "自动（Cookie → 密码 → 验证码）",
    LOGIN_MODE_PASSWORD: "账号密码登录",
    LOGIN_MODE_SMS: "手机验证码登录",
    LOGIN_MODE_COOKIE: "Cookie 登录",
}
_MODE_ALIASES = {
    "1": LOGIN_MODE_PASSWORD,
    "密码": LOGIN_MODE_PASSWORD,
    "账号密码": LOGIN_MODE_PASSWORD,
    "password": LOGIN_MODE_PASSWORD,
    "2": LOGIN_MODE_SMS,
    "验证码": LOGIN_MODE_SMS,
    "短信": LOGIN_MODE_SMS,
    "短信验证码": LOGIN_MODE_SMS,
    "sms": LOGIN_MODE_SMS,
    "code": LOGIN_MODE_SMS,
    "3": LOGIN_MODE_COOKIE,
    "cookie": LOGIN_MODE_COOKIE,
    "4": LOGIN_MODE_AUTO,
    "auto": LOGIN_MODE_AUTO,
    "自动": LOGIN_MODE_AUTO,
}

_MENU_TEXT = (
    "🔐 请选择学习通登录方式：\n"
    "1️⃣ 账号密码登录（用已保存的手机号+密码，缺密码会提示你输入）\n"
    "2️⃣ 手机验证码登录（发短信到你手机，再回复验证码）\n"
    "3️⃣ Cookie 登录（用已保存/配置的 Cookie，最稳）\n"
    "4️⃣ 自动（先试 Cookie，再密码，需要时弹图形验证码）\n\n"
    "回复数字或关键词（如 1 / 密码 / 短信 / cookie），120 秒内有效"
)

_CHOICE_TIMEOUT = 120
_SMS_THROTTLE_SECONDS = 600  # 自动模式下短信最多 10 分钟发一条，避免刷屏扣费
_COURSE_CACHE_SECONDS = 600  # 课程列表缓存 10 分钟，避免反复请求触发风控

_ASK_PHONE_TEXT = (
    "📱 还没有设置学习通账号，先把手机号告诉我：\n"
    "请直接回复 11 位手机号，例如 13800000000\n"
    "（也可以发 cxsetphone 13800000000，随时修改）"
)

_NO_ACCOUNT_HINT = (
    "还没有设置学习通账号。\n"
    "直接发「cxlogin」，机器人会引导你输入手机号和密码，"
    "不需要在 WebUI 配置里填任何东西。"
)


def _mask_phone(phone: str) -> str:
    """138****0000 脱敏显示"""
    phone = (phone or "").strip()
    if len(phone) >= 7:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone or "未设置"


@register(
    "astrbot_plugin_chaoxing_ddl",
    "WuHen",
    "学习通作业 DDL 提醒：自动读取未交作业截止时间，剩最后 N 天时主动推送提醒",
    "0.5.2",
)
class ChaoxingDdlPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._client: ChaoxingClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._digest_task: asyncio.Task | None = None
        # 等待用户输入的状态：{"kind": "menu"|"captcha"|"sms", "future": Future}
        self._pending: dict | None = None
        self._last_umo: str | None = None
        self._last_error_date: str | None = None
        self._last_sms_sent: float = 0.0
        # 课程列表缓存：(取得时间, [课程dict])，避免每次查课程都重新登录+拉页面
        self._courses_cache: tuple[float, list[dict]] | None = None

    # ---------- 生命周期 ----------

    async def initialize(self):
        self._migrate_legacy_data()
        cfg_phone = str(self.config.get("phone", "") or "").strip()
        cfg_password = str(self.config.get("password", "") or "").strip()
        data = self._load_data()
        # 账号优先级：WebUI 配置 > 本地 data.json（QQ 指令里设置过的）
        phone = cfg_phone or str(data.get("phone") or "").strip()
        password = cfg_password or str(data.get("password") or "").strip()
        cookie = str(data.get("cookie") or self.config.get("cookie", "") or "").strip()

        # 关键：即使 WebUI 一个字段都没填，也要建好客户端。
        # 账号可以在 QQ 里用 cxlogin / cxsetphone / cxpwd 随时补上。
        self._client = ChaoxingClient(phone, password, cookie=cookie)
        self._client.captcha_handler = self._ask_captcha
        self._client.login_runner = self._ensure_login

        # 恢复上次的登录态，减少重复登录（也就少触发验证码）
        saved = data.get("session_cookies")
        if isinstance(saved, dict) and saved:
            self._client.import_cookies(saved)

        if self._has_account():
            await self._start_tasks()
        else:
            logger.info(
                "学习通DDL插件: 尚未配置账号。无需改 WebUI 配置，"
                "直接在 QQ 里发送 cxlogin（或 cxsetphone 手机号 / cxpwd 密码）即可开始使用。"
            )

    def _migrate_legacy_data(self):
        """旧版把 data.json 写在插件自身目录，重装会丢；迁移到 data/plugin_data/"""
        legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
        if not os.path.exists(legacy):
            return
        try:
            if os.path.exists(_DATA_FILE):
                # 新位置已有数据，直接丢弃旧文件，避免覆盖
                os.remove(legacy)
                logger.info("学习通DDL插件: 已清理插件目录内的旧版 data.json")
                return
            os.makedirs(_DATA_DIR, exist_ok=True)
            shutil.move(legacy, _DATA_FILE)
            logger.info(f"学习通DDL插件: 旧版 data.json 已迁移到 {_DATA_DIR}")
        except Exception as e:
            logger.warning(f"学习通DDL插件: 迁移旧版 data.json 失败（不影响使用）: {e}")

    def _has_account(self) -> bool:
        """是否已有可用登录凭据：Cookie，或手机号+密码"""
        client = self._client
        if client is None:
            return False
        cookie = getattr(client, "cookie", "") or ""
        try:
            jar = client.export_cookies() or {}
        except Exception:
            jar = {}
        if cookie or jar:
            return True
        return bool(getattr(client, "phone", "") and getattr(client, "password", ""))

    async def _start_tasks(self):
        """账号就绪后启动巡检/汇总（启动时无账号则等首次登录成功再启）"""
        if self._poll_task and not self._poll_task.done():
            return
        interval = max(15, int(self.config.get("check_interval_minutes", 60)))
        self._poll_task = asyncio.create_task(self._poll_loop(interval))
        logger.info(f"学习通DDL插件: 巡检已启动（间隔 {interval} 分钟）")

        try:
            digest_hour = int(self.config.get("digest_hour", 9))
        except Exception:
            digest_hour = 9
        if 0 <= digest_hour <= 23 and not (self._digest_task and not self._digest_task.done()):
            self._digest_task = asyncio.create_task(self._digest_loop(digest_hour))
            logger.info(f"学习通DDL插件: 每日汇总已启动（每天 {digest_hour} 点）")

        asyncio.create_task(self._safe_check())

    async def terminate(self):
        for task in (self._poll_task, self._digest_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._client:
            await self._client.close()
        logger.info("学习通DDL插件: 已停止")

    # ---------- 本地持久化 ----------

    def _load_data(self) -> dict:
        try:
            with open(_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_data(self, data: dict):
        try:
            with open(_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"学习通DDL插件: 写入 data.json 失败: {e}")

    def _save_session(self):
        if not self._client:
            return
        data = self._load_data()
        data["session_cookies"] = self._client.export_cookies()
        data["session_saved_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_data(data)

    # ---------- 推送目标 ----------

    def _get_targets(self) -> list[str]:
        data = self._load_data()
        bound = data.get("bound_umos", [])
        configured = self.config.get("push_targets", []) or []
        return list(dict.fromkeys([*configured, *bound]))

    async def _push_to(self, targets: list[str], chain: MessageChain, text_fallback: str = ""):
        for umo in targets:
            try:
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.error(f"学习通DDL插件: 推送失败 {umo}: {e}")
                if text_fallback:
                    try:
                        await self.context.send_message(
                            umo, MessageChain().message(text_fallback)
                        )
                    except Exception:
                        pass

    async def _push(self, text: str):
        await self._push_to(self._get_targets(), MessageChain().message(text))

    # ---------- 登录方式 ----------

    def _get_login_mode(self) -> str:
        data = self._load_data()
        mode = str(data.get("login_mode") or self.config.get("login_mode") or LOGIN_MODE_AUTO).lower()
        if mode not in _MODE_LABELS:
            mode = LOGIN_MODE_AUTO
        return mode

    def _set_login_mode(self, mode: str):
        data = self._load_data()
        data["login_mode"] = mode
        self._save_data(data)

    def _save_account(self, phone: str | None = None, password: str | None = None):
        """把账号写进 data.json 并同步到客户端（WebUI 留空也能用）"""
        data = self._load_data()
        if phone is not None:
            data["phone"] = phone
            if self._client:
                self._client.phone = phone
        if password is not None:
            data["password"] = password
            if self._client:
                self._client.password = password
        data["account_updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_data(data)
        self._courses_cache = None

    async def _ensure_login(self, mode: str | None = None, force: bool = False) -> bool:
        """登录编排：按选定的登录方式尝试；成功返回 True，失败抛异常。"""
        client = self._client
        if client is None:
            raise RuntimeError("插件尚未初始化（缺少账号配置）")
        if client.logged_in and not force:
            return True
        if force:
            client.mark_logged_out()

        mode = (mode or self._get_login_mode()).lower()
        errors: list[str] = []
        # 换了登录方式/账号，课程缓存可能已失效
        self._courses_cache = None

        # 一条凭据都没有：给可执行的引导，而不是底层异常
        if not client.phone and not client.cookie and not client.export_cookies():
            raise RuntimeError(_NO_ACCOUNT_HINT)

        if mode == LOGIN_MODE_PASSWORD and not (client.phone and client.password):
            raise RuntimeError(
                "密码登录需要手机号和密码。\n"
                "补一个即可：cxsetphone 13800000000 / cxpwd 你的密码，"
                "或改用 cxlogin 短信 走短信验证码（免密码）"
            )

        # 任何模式下都先试一次已有 Cookie：能省就省，少一次验证码
        if mode in (LOGIN_MODE_AUTO, LOGIN_MODE_COOKIE):
            if await client.check_cookie():
                client.mark_logged_in()
                self._save_session()
                logger.info("学习通DDL插件: Cookie 有效，直接复用登录态")
                return True
            if mode == LOGIN_MODE_COOKIE:
                raise RuntimeError("Cookie 无效或未配置，请用 cxsetcookie 提交或到 WebUI 填 cookie")

        if mode in (LOGIN_MODE_AUTO, LOGIN_MODE_PASSWORD):
            if client.phone and client.password:
                try:
                    await client.login_by_password()
                    self._save_session()
                    logger.info("学习通DDL插件: 密码登录成功")
                    return True
                except Exception as e:
                    errors.append(f"密码登录失败（{e}）")
            else:
                errors.append("未配置手机号/密码")

        if mode == LOGIN_MODE_SMS:
            code = await self._request_sms_code()
            if not code:
                raise RuntimeError("短信验证码登录未完成（发送失败或等待超时）")
            await client.login_by_sms(code)
            self._save_session()
            logger.info("学习通DDL插件: 短信验证码登录成功")
            return True

        if mode == LOGIN_MODE_AUTO and errors:
            raise RuntimeError("；".join(errors))
        raise RuntimeError("；".join(errors) or "登录失败：未配置可用的登录方式")

    async def _request_sms_code(self) -> str | None:
        """发送短信验证码并等待用户回复"""
        client = self._client
        if not client.phone:
            await self._push("⚠️ 未配置手机号，无法使用短信验证码登录，请到 WebUI 填写手机号")
            return None
        now = datetime.now().timestamp()
        if now - self._last_sms_sent < _SMS_THROTTLE_SECONDS:
            await self._push("⏳ 短信验证码刚发过，请稍等一会儿再试（避免频繁发送被限流）")
            return None
        try:
            resp = await client.send_sms_code()
            self._last_sms_sent = now
            logger.info(f"学习通DDL插件: 短信验证码发送响应: {resp[:120]}")
        except Exception as e:
            await self._push(f"❌ 短信验证码发送失败：{e}")
            return None

        masked = client.phone[:3] + "****" + client.phone[-2:] if len(client.phone) >= 6 else "手机"
        prompt = (
            f"📱 验证码已发送至 {masked}\n"
            f"请回复：cxcode 123456（6 位数字，直接发数字也可以）"
        )
        return await self._wait_user_input("sms", prompt)

    # ---------- 通用「等用户回一句」 ----------

    async def _wait_user_input(
        self,
        kind: str,
        prompt: str,
        timeout: int | None = None,
        image: bytes = b"",
        send_prompt: bool = True,
    ) -> str | None:
        timeout = timeout or int(self.config.get("captcha_timeout", 180) or 180)
        loop = asyncio.get_running_loop()

        # 若已有等待中的请求，先取消旧的，避免 future 泄漏
        if self._pending and not self._pending["future"].done():
            self._pending["future"].cancel()
        future = loop.create_future()
        self._pending = {"kind": kind, "future": future, "at": loop.time()}

        targets = self._get_targets()
        if self._last_umo and self._last_umo not in targets:
            targets = [*targets, self._last_umo]
        if not targets:
            logger.warning("学习通DDL插件: 没有可推送的会话，请先发送 cxbind")
            self._pending = None
            return None

        if send_prompt:
            chain = MessageChain().message(prompt)
            if image:
                try:
                    with open(_CAPTCHA_FILE, "wb") as f:
                        f.write(image)
                    chain = chain.file_image(_CAPTCHA_FILE)
                except Exception as e:
                    logger.error(f"学习通DDL插件: 保存验证码图片失败: {e}")
            await self._push_to(targets, chain, text_fallback=prompt)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return str(result).strip()
        except asyncio.TimeoutError:
            logger.warning(f"学习通DDL插件: 等待用户输入超时（{kind}）")
            # 手机号/密码的等待由对应指令自己给提示，避免重复打扰
            if kind not in ("phone", "pwd"):
                await self._push(f"⌛ 等待输入超时（{timeout} 秒），操作已取消，下次会自动重试。")
            return None
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.error(f"学习通DDL插件: 等待用户输入异常: {e}")
            return None
        finally:
            self._pending = None

    def _resolve_pending(self, text: str) -> str | None:
        """把用户回复交给等待中的协程；返回被满足的 kind，未匹配返回 None"""
        pending = self._pending
        if not pending or pending["future"].done():
            return None
        kind = pending["kind"]
        text = (text or "").strip()
        if not text:
            return None

        if kind == "menu":
            if text in ("取消", "cancel", "q"):
                pending["future"].set_result("")
                return "menu"
            mode = _MODE_ALIASES.get(text.lower())
            if not mode:
                return None
            pending["future"].set_result(mode)
            return "menu"

        # 手机号 / 密码：除命令外都接受，交给调用方校验格式
        if kind in ("phone", "pwd"):
            if text.startswith("/") or text.lower().startswith("cx"):
                return None
            if text in ("取消", "取消登录", "cancel", "q"):
                pending["future"].set_result("")
                return kind
            pending["future"].set_result(text)
            return kind

        # captcha / sms：3~8 位字母数字
        if re.fullmatch(r"[A-Za-z0-9]{3,8}", text):
            pending["future"].set_result(text)
            return kind
        return None

    async def _ask_captcha(self, image: bytes, tip: str) -> str | None:
        """网页登录要图形验证码时被调用"""
        prompt = (
            f"🔐 学习通网页登录需要图形验证码{'（' + tip + '）' if tip else ''}\n"
            f"请回复：cxcode 验证码（也可以直接发 4~6 位字符）\n"
            f"输错会自动换一张新图再问一次"
        )
        return await self._wait_user_input("captcha", prompt, image=image)

    # ---------- 巡检与提醒 ----------

    async def _poll_loop(self, interval_minutes: int):
        while True:
            await asyncio.sleep(interval_minutes * 60)
            await self._safe_check()

    async def _safe_check(self):
        try:
            await self._check_and_notify()
        except Exception as e:
            logger.error(f"学习通DDL插件: 巡检异常: {e}")
            # 静默失败最危险：每天最多主动告知一次
            today = datetime.now().strftime("%Y-%m-%d")
            if self._last_error_date != today:
                self._last_error_date = today
                await self._push(
                    f"⚠️ 学习通巡检失败：{e}\n"
                    f"可能是 Cookie/密码失效、需要验证码或接口变更。\n"
                    f"发送 cxlogin 可重新选择登录方式。"
                )

    async def _fetch_deadlines(self) -> list[dict]:
        if not self._client:
            return []
        course_filter = self.config.get("course_filter", []) or []
        include_exams = bool(self.config.get("include_exams", False))
        return await self._client.fetch_all_deadlines(course_filter, include_exams)

    # ---------- 课程列表 ----------

    async def _get_courses(self, refresh: bool = False) -> list[dict]:
        """取课程列表，带 10 分钟内存缓存；refresh=True 强制刷新"""
        client = self._client
        if client is None:
            raise RuntimeError("插件未初始化（缺少账号配置）")
        now = asyncio.get_running_loop().time()
        if not refresh and self._courses_cache:
            ts, cached = self._courses_cache
            if now - ts < _COURSE_CACHE_SECONDS:
                return cached
        await self._ensure_login()
        courses = await client.get_courses()
        self._courses_cache = (now, courses)
        return courses

    @staticmethod
    def _match_course(courses: list[dict], arg: str) -> dict | None:
        """按序号（从 1 开始）或课名匹配课程，支持模糊：高数 → 高等数学"""
        arg = (arg or "").strip()
        if not arg:
            return None
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(courses):
                return courses[idx]
            return None

        # 1) 直接包含：命中多个时取名字最短的（通常就是本意）
        hits = [c for c in courses if arg in c["name"]]
        if hits:
            return min(hits, key=lambda c: len(c["name"]))

        # 2) 逐字顺序匹配：把「高数」的每个字按顺序在课名里找一遍
        def _in_order(query: str, target: str) -> bool:
            it = iter(target)
            return all(ch in it for ch in query)

        hits = [c for c in courses if _in_order(arg, c["name"])]
        if hits:
            return min(hits, key=lambda c: len(c["name"]))
        return None

    @staticmethod
    def _format_course_list(courses: list[dict]) -> str:
        lines = [f"📚 我的课程（共 {len(courses)} 门）", ""]
        for i, course in enumerate(courses, 1):
            lines.append(f"{i}. {course['name']}")
        lines += [
            "",
            "💡 发送「课程 序号」查看该课程未交作业，例如：课程 1",
            "💡 发送「作业」查看全部未交作业与截止时间",
        ]
        return "\n".join(lines)

    def _get_remind_days(self) -> list[int]:
        """提醒节点（提前天数），配置非法时回退为 [2]"""
        raw = self.config.get("remind_days_before", [2]) or [2]
        days = [int(d) for d in raw if str(d).strip().lstrip("-").isdigit()]
        return sorted({max(1, d) for d in days}, reverse=True) or [2]

    def _build_help(self) -> str:
        """命令一览 + 当前运行状态"""
        mode = self._get_login_mode()
        mode_label = _MODE_LABELS.get(mode, mode)
        logged = "✅ 已登录" if (self._client and self._client.logged_in) else "⚠️ 未登录"
        phone = self._client.phone if self._client else ""
        phone_desc = _mask_phone(phone) if phone else "未设置（发 cxlogin 会引导你填）"
        bound = self._load_data().get("bound_umos", []) or []
        bound_desc = f"已绑定 {len(bound)} 个会话" if bound else "未绑定（发 cxbind 绑定）"
        remind_desc = "、".join(f"剩 {d} 天" for d in self._get_remind_days())
        try:
            interval = max(15, int(self.config.get("check_interval_minutes", 60)))
        except Exception:
            interval = 60
        try:
            digest_hour = int(self.config.get("digest_hour", 9))
        except Exception:
            digest_hour = 9
        digest_desc = f"每天 {digest_hour:02d}:00" if 0 <= digest_hour <= 23 else "已关闭"

        return "\n".join(
            [
                "📖 学习通助手 · 常用命令",
                "",
                "【查询】",
                "作业 —— 全部未交作业与截止时间",
                "课程 —— 我的课程列表",
                "课程 2 —— 第 2 门课的未交作业（也可用课名，如「课程 高数」）",
                "",
                "【账号】",
                "cxlogin —— 登录 / 首次使用时的引导设置（无需改 WebUI 配置）",
                "cxsetphone 13800000000 —— 设置手机号",
                "cxpwd 你的密码 —— 设置密码",
                "cxaccount —— 查看账号与登录状态",
                "cxcode 123456 —— 提交验证码",
                "cxsetcookie <cookie> —— 粘贴浏览器 Cookie 直接登录",
                "",
                "【提醒】",
                "cxbind —— 把当前会话设为提醒推送目标",
                "cxunbind —— 解绑当前会话",
                "cxcheck —— 立即巡检一次",
                "cxhelp —— 显示本帮助",
                "",
                "──────────────",
                f"账号：{phone_desc}",
                f"登录方式：{mode_label}",
                f"登录状态：{logged}",
                f"推送会话：{bound_desc}",
                f"提醒节点：{remind_desc}",
                f"巡检间隔：{interval} 分钟",
                f"每日汇总：{digest_desc}",
            ]
        )

    def _format_item(self, item: dict, now: datetime) -> str:
        ddl: datetime | None = item.get("ddl")
        if ddl:
            remain = ddl - now
            hours = int(remain.total_seconds() // 3600)
            if hours >= 48:
                time_desc = f"距截止还有 {hours // 24} 天 {hours % 24} 小时"
            elif hours >= 1:
                time_desc = f"距截止还有 {hours} 小时"
            else:
                time_desc = "马上到截止时间！"
            ddl_str = ddl.strftime("%m-%d %H:%M")
        else:
            time_desc = "无时间限制或未知，但还没做完，尽快处理！"
            ddl_str = "无 / 未知"
        return (
            f"📚 {item['course']}\n"
            f"📝 {item['kind']}：{item['title']}\n"
            f"⏰ 截止：{ddl_str}（{time_desc}）"
        )

    async def _check_and_notify(self) -> list[dict]:
        """巡检：抓取 DDL，评估提醒规则并推送。返回最新快照。"""
        if not self._client:
            logger.warning("学习通DDL插件: 未初始化（缺少账号配置），跳过巡检")
            return []

        items = await self._fetch_deadlines()
        now = datetime.now()

        remind_days = self._get_remind_days()

        data = self._load_data()
        triggered: set[str] = set(data.get("triggered", []))

        for item in items:
            ddl = item.get("ddl")
            if not ddl or ddl <= now:
                continue
            for days in remind_days:
                key = f"{item['course']}|{item['title']}|{days}"
                if key in triggered:
                    continue
                if now >= ddl - timedelta(days=days):
                    msg = (
                        f"⏰ 学习通{item['kind']}提醒\n"
                        f"{'─' * 14}\n"
                        f"{self._format_item(item, now)}\n"
                        f"{'─' * 14}\n"
                        f"💪 剩最后 {days} 天了，抓紧完成哦！"
                    )
                    await self._push(msg)
                    triggered.add(key)
                    logger.info(f"学习通DDL插件: 已推送提醒 [{key}]")

        # 只保留当前仍存在的任务的触发记录，避免 data.json 膨胀
        current_keys = {f"{it['course']}|{it['title']}|{d}" for it in items for d in remind_days}
        data["triggered"] = sorted(triggered & current_keys)
        self._save_data(data)
        return items

    # ---------- 每日汇总 ----------

    async def _digest_loop(self, digest_hour: int):
        sent_date = None
        while True:
            now = datetime.now()
            if now.hour == digest_hour and now.strftime("%Y-%m-%d") != sent_date:
                try:
                    items = await self._fetch_deadlines()
                    await self._push(self._format_digest(items, now))
                    sent_date = now.strftime("%Y-%m-%d")
                except Exception as e:
                    logger.error(f"学习通DDL插件: 每日汇总失败: {e}")
            await asyncio.sleep(60)

    def _format_digest(self, items: list[dict], now: datetime) -> str:
        digest_days = int(self.config.get("digest_days", 7) or 0)
        if digest_days > 0:
            limit = now + timedelta(days=digest_days)
            items = [it for it in items if it.get("ddl") is None or it["ddl"] <= limit]
        unfinished = [it for it in items if it.get("status") in UNFINISHED_STATUS]
        if not unfinished:
            return "✅ 学习通今日汇总：当前没有未完成的作业，可以放心休息啦～"
        lines = [f"📋 学习通未交作业一览（共 {len(unfinished)} 项）", ""]
        for it in unfinished[:15]:
            ddl = it.get("ddl")
            ddl_str = ddl.strftime("%m-%d %H:%M") if ddl else "未知"
            lines.append(f"• [{it['course']}] {it['title']} — 截止 {ddl_str}")
        if len(unfinished) > 15:
            lines.append(f"... 及另外 {len(unfinished) - 15} 项")
        return "\n".join(lines)

    # ---------- 指令：登录 ----------

    @filter.command("cxlogin", alias={"登录"})
    async def cmd_login(self, event: AstrMessageEvent):
        """选择登录方式并登录；没有配置账号时会先引导输入手机号/密码"""
        self._last_umo = event.unified_msg_origin
        client = self._client
        if client is None:
            yield event.plain_result("插件未初始化，请先在 AstrBot 里重载插件")
            return

        text = self._strip_cmd(event.message_str, "cxlogin", "登录")
        mode = _MODE_ALIASES.get(text.split()[0].lower()) if text.split() else None

        # 1) 一条凭据都没有：先要手机号（短信登录只差这一步）
        if not client.phone and not client.cookie and not client.export_cookies():
            yield event.plain_result(_ASK_PHONE_TEXT)
            phone = None
            for _ in range(3):
                raw = await self._wait_user_input(
                    "phone", _ASK_PHONE_TEXT, timeout=_CHOICE_TIMEOUT, send_prompt=False
                )
                if not raw:
                    break
                candidate = re.sub(r"\D", "", raw)
                if len(candidate) == 11:
                    phone = candidate
                    break
                yield event.plain_result(
                    f"⚠️ 手机号需要 11 位数字（收到 {len(candidate)} 位），请再回复一次"
                )
            if not phone:
                yield event.plain_result("已取消。可随时发 cxlogin 重新开始。")
                return
            self._save_account(phone=phone)
            yield event.plain_result(f"✅ 手机号已保存：{_mask_phone(phone)}")

        # 2) 选登录方式
        if not mode:
            yield event.plain_result(_MENU_TEXT)
            # 菜单已经发在当前会话了，不再向绑定会话重复推送
            mode = await self._wait_user_input(
                "menu", _MENU_TEXT, timeout=_CHOICE_TIMEOUT, send_prompt=False
            )
            if not mode:
                yield event.plain_result("已取消登录（可随时发送 cxlogin 重新选择）")
                return

        # 3) 密码登录但没密码：再问一次
        if mode == LOGIN_MODE_PASSWORD and not client.password:
            yield event.plain_result(
                "🔑 请直接回复学习通密码（也可以发 cxpwd 你的密码）\n"
                "密码只会保存在本机 data/plugin_data 插件数据目录里；不想存密码可改用 cxlogin 短信"
            )
            pwd = await self._wait_user_input(
                "pwd", "请回复学习通密码", timeout=_CHOICE_TIMEOUT, send_prompt=False
            )
            if not pwd:
                yield event.plain_result("已取消。可改用 cxlogin 短信 免密码登录。")
                return
            self._save_account(password=pwd)
            yield event.plain_result("✅ 密码已保存")

        # 4) Cookie 登录但没有 Cookie
        if mode == LOGIN_MODE_COOKIE and not (client.cookie or client.export_cookies()):
            yield event.plain_result(
                "还没有 Cookie。获取方式：浏览器登录 i.chaoxing.com → F12 → Network → "
                "任意请求 → 复制 Cookie，然后发 cxsetcookie <整段 cookie>"
            )
            return

        self._set_login_mode(mode)
        yield event.plain_result(f"正在用「{_MODE_LABELS.get(mode, mode)}」登录学习通…")
        try:
            await self._ensure_login(mode=mode, force=True)
        except Exception as e:
            hint = ""
            if mode == LOGIN_MODE_PASSWORD:
                hint = "\n提示：可改用 cxlogin 短信 或 cxlogin cookie"
            yield event.plain_result(f"❌ 登录失败：{e}{hint}")
            return

        # 首次登录成功后才真正开始巡检（没账号时不空转）
        await self._start_tasks()
        yield event.plain_result(
            f"✅ 登录成功（{_MODE_LABELS.get(mode, mode)}）\n"
            f"后续巡检会默认使用该方式，可发「作业」或「课程」试试。"
        )

    @filter.command("cxsetphone", alias={"设置手机号", "cxphone"})
    async def cmd_set_phone(self, event: AstrMessageEvent):
        """设置学习通手机号：cxsetphone 13800000000"""
        self._last_umo = event.unified_msg_origin
        if not self._client:
            yield event.plain_result("插件未初始化，请先重载插件")
            return
        arg = self._strip_cmd(event.message_str, "cxsetphone", "设置手机号", "cxphone").strip()
        phone = re.sub(r"\D", "", arg)
        if len(phone) != 11:
            yield event.plain_result(
                "用法：cxsetphone 13800000000（11 位手机号）\n设置后用 cxlogin 选择登录方式"
            )
            return
        self._save_account(phone=phone)
        has_pwd = "已设置" if self._client.password else "未设置"
        extra = ""
        if self._has_account():
            await self._start_tasks()
            extra = "\n巡检已启动，接下来会自动登录并推送提醒"
        yield event.plain_result(
            f"✅ 手机号已保存：{_mask_phone(phone)}（密码{has_pwd}）{extra}\n"
            f"接着发 cxlogin 短信 可用短信验证码登录，cxlogin 密码 需要先 cxpwd 设置密码"
        )

    @filter.command("cxpwd", alias={"设置密码", "cxpassword"})
    async def cmd_set_password(self, event: AstrMessageEvent):
        """设置学习通密码：cxpwd 你的密码"""
        self._last_umo = event.unified_msg_origin
        if not self._client:
            yield event.plain_result("插件未初始化，请先重载插件")
            return
        pwd = self._strip_cmd(event.message_str, "cxpwd", "设置密码", "cxpassword").strip()
        if not pwd:
            yield event.plain_result(
                "用法：cxpwd 你的密码\n不想存密码可以改用短信验证码登录：cxlogin 短信"
            )
            return
        self._save_account(password=pwd)
        has_phone = _mask_phone(self._client.phone) if self._client.phone else "未设置"
        extra = ""
        if self._has_account():
            await self._start_tasks()
            extra = "，巡检已启动"
        yield event.plain_result(
            f"✅ 密码已保存（手机号：{has_phone}{extra}）\n接着发 cxlogin 密码 即可立即登录"
        )

    @filter.command("cxaccount", alias={"账号", "cxstatus", "cx状态"})
    async def cmd_account(self, event: AstrMessageEvent):
        """查看当前账号与登录配置状态"""
        self._last_umo = event.unified_msg_origin
        client = self._client
        if client is None:
            yield event.plain_result("插件未初始化，请先重载插件")
            return
        data = self._load_data()
        phone = client.phone or "未设置"
        pwd = "已设置" if client.password else "未设置"
        cookie = "已设置" if (client.cookie or client.export_cookies()) else "未设置"
        logged = "✅ 已登录" if client.logged_in else "⚠️ 未登录"
        cfg_phone = str(self.config.get("phone", "") or "").strip()
        source = "WebUI 配置" if cfg_phone else ("本地 data.json" if phone != "未设置" else "—")
        yield event.plain_result(
            "\n".join(
                [
                    "👤 学习通账号状态",
                    f"手机号：{_mask_phone(phone) if phone != '未设置' else phone}",
                    f"密码：{pwd}",
                    f"Cookie：{cookie}",
                    f"登录状态：{logged}",
                    f"账号来源：{source}",
                    f"默认登录方式：{_MODE_LABELS.get(self._get_login_mode(), self._get_login_mode())}",
                    "",
                    "改账号：cxsetphone 手机号 / cxpwd 密码 / cxsetcookie <cookie>",
                    "开始登录：cxlogin",
                ]
            )
        )

    @filter.command("cxsetcookie", alias={"设置cookie"})
    async def cmd_set_cookie(self, event: AstrMessageEvent):
        """提交浏览器 Cookie 登录：cxsetcookie <整段 cookie>"""
        self._last_umo = event.unified_msg_origin
        if not self._client:
            yield event.plain_result("插件未初始化，请先在 AstrBot 里重载插件")
            return
        cookie = self._strip_cmd(event.message_str, "cxsetcookie", "设置cookie").strip()
        if "=" not in cookie:
            yield event.plain_result(
                "用法：cxsetcookie <整段 Cookie>\n"
                "获取方式：浏览器登录 i.chaoxing.com → F12 → Network → 任意请求 → "
                "复制 Request Headers 里 Cookie 后面的内容"
            )
            return
        data = self._load_data()
        data["cookie"] = cookie
        data["login_mode"] = LOGIN_MODE_COOKIE
        self._save_data(data)

        self._client.apply_cookie(cookie)
        self._client.mark_logged_out()
        if await self._client.check_cookie():
            self._client.mark_logged_in()
            self._save_session()
            await self._start_tasks()
            yield event.plain_result("✅ Cookie 有效，已登录学习通并保存登录态，巡检已启动")
        else:
            yield event.plain_result("⚠️ Cookie 已保存，但校验未通过（可能已过期或复制不完整）")

    @filter.command("cxcode")
    async def cmd_code(self, event: AstrMessageEvent):
        """提交验证码（短信验证码或图形验证码）：cxcode 123456"""
        self._last_umo = event.unified_msg_origin
        text = self._strip_cmd(event.message_str, "cxcode", "验证码").strip()
        code = text.split()[0] if text.split() else ""
        if not code:
            yield event.plain_result("用法：cxcode 123456（手机短信验证码或图片验证码）")
            return
        kind = self._resolve_pending(code)
        if kind == "sms":
            yield event.plain_result("✅ 验证码已提交，正在登录学习通…")
        elif kind == "captcha":
            yield event.plain_result("✅ 图形验证码已提交，正在重试登录…")
        elif kind == "menu":
            yield event.plain_result("✅ 已收到选择")
        else:
            yield event.plain_result("当前没有等待中的验证码请求（可能已超时，可发送 cxlogin 重新发起）")

    # ---------- 指令：作业与提醒 ----------

    @filter.command("作业")
    async def query_homework(self, event: AstrMessageEvent):
        """手动查询当前未交作业及临近 DDL"""
        self._last_umo = event.unified_msg_origin
        if not self._client:
            yield event.plain_result("插件未初始化，请先在 AstrBot 里重载插件")
            return
        if not self._has_account():
            yield event.plain_result(_NO_ACCOUNT_HINT)
            return
        show_all = self._strip_cmd(event.message_str, "作业").strip().lower() in (
            "全部", "all", "所有"
        )
        yield event.plain_result("正在登录学习通查询作业，请稍候…")
        try:
            items = await self._check_and_notify()
        except Exception as e:
            yield event.plain_result(f"查询失败：{e}\n可发送 cxlogin 重新选择登录方式")
            return
        now = datetime.now()

        # 诊断模式：列出抓到的所有条目（含已完成），便于核对状态识别
        if show_all:
            if not items:
                yield event.plain_result(
                    "学习通没有返回任何作业条目。\n"
                    "如果你的预习/作业在 App 里能看到，多半是「章节内任务」"
                    "（挂在课程目录里，不进作业列表），这类暂时抓不到。"
                )
                return
            lines = [f"📋 作业列表全部条目（共 {len(items)} 项，含已完成）", ""]
            for it in items[:30]:
                ddl = it.get("ddl")
                ddl_str = ddl.strftime("%m-%d %H:%M") if ddl else "无时间限制/未知"
                lines.append(
                    f"• [{it['course']}] {it['title']}\n"
                    f"  状态：{it.get('status') or '（空）'} ｜ 截止：{ddl_str}"
                )
            if len(items) > 30:
                lines.append(f"… 及另外 {len(items) - 30} 项")
            lines.append("")
            lines.append("💡 上面「状态」是学习通返回的原文。")
            lines.append("如果你的预习作业不在这里，说明它挂在章节任务里，不在作业列表。")
            yield event.plain_result("\n".join(lines))
            return

        pending = [
            it
            for it in items
            if it.get("status") in UNFINISHED_STATUS
            and (not it.get("ddl") or it["ddl"] > now)
        ]
        if not pending:
            extra = ""
            if items:
                extra = (
                    f"\n（本次抓到 {len(items)} 条作业记录，但状态都不属于未完成。\n"
                    f"发「作业 全部」可查看全部条目和状态原文，核对是否有漏）"
                )
            yield event.plain_result("✅ 当前没有未完成的作业，冲！" + extra)
            return
        msg = f"📋 未交作业 {len(pending)} 项：\n\n" + "\n\n".join(
            self._format_item(it, now) for it in pending[:10]
        )
        if len(pending) > 10:
            msg += f"\n\n… 及另外 {len(pending) - 10} 项"
        yield event.plain_result(msg)

    # ---------- 指令：课程与帮助 ----------

    @filter.command("课程", alias={"cxcourse", "我的课程", "courses"})
    async def cmd_courses(self, event: AstrMessageEvent):
        """查询我的学习通课程列表；带参数看该课程作业：课程 2 / 课程 高数"""
        self._last_umo = event.unified_msg_origin
        if not self._client:
            yield event.plain_result("插件未初始化，请先在 AstrBot 里重载插件")
            return
        if not self._has_account():
            yield event.plain_result(_NO_ACCOUNT_HINT)
            return
        arg = self._strip_cmd(
            event.message_str, "课程", "cxcourse", "我的课程", "courses"
        ).strip()
        yield event.plain_result("正在读取学习通课程列表…")
        try:
            courses = await self._get_courses()
        except Exception as e:
            yield event.plain_result(f"课程获取失败：{e}\n可发送 cxlogin 重新选择登录方式")
            return

        if not arg:
            yield event.plain_result(self._format_course_list(courses))
            return

        course = self._match_course(courses, arg)
        if not course:
            yield event.plain_result(
                f"没找到匹配「{arg}」的课程。\n发送「课程」可查看全部课程和序号。"
            )
            return

        now = datetime.now()
        try:
            items = await self._client.get_work_deadlines(course)
        except Exception as e:
            yield event.plain_result(f"《{course['name']}》作业查询失败：{e}")
            return
        pending = [
            it
            for it in items
            if it.get("status") in UNFINISHED_STATUS
            and (not it.get("ddl") or it["ddl"] > now)
        ]
        if not pending:
            yield event.plain_result(f"✅ 《{course['name']}》当前没有未完成的作业")
            return
        msg = f"📋 《{course['name']}》未交作业 {len(pending)} 项：\n\n" + "\n\n".join(
            self._format_item(it, now) for it in pending[:10]
        )
        if len(pending) > 10:
            msg += f"\n\n… 及另外 {len(pending) - 10} 项"
        yield event.plain_result(msg)

    @filter.command("cxhelp", alias={"帮助", "命令", "cx菜单"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看常用命令与当前运行状态"""
        self._last_umo = event.unified_msg_origin
        yield event.plain_result(self._build_help())

    @filter.command("cxbind")
    async def cmd_bind(self, event: AstrMessageEvent):
        """把当前会话绑定为提醒推送目标"""
        umo = event.unified_msg_origin
        self._last_umo = umo
        data = self._load_data()
        bound: list[str] = data.get("bound_umos", [])
        if umo in bound:
            yield event.plain_result("当前会话已绑定过学习通提醒啦")
            return
        bound.append(umo)
        data["bound_umos"] = bound
        self._save_data(data)
        yield event.plain_result("✅ 已绑定当前会话，作业 DDL 提醒将推送到这里")

    @filter.command("cxunbind")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解绑当前会话的提醒推送"""
        umo = event.unified_msg_origin
        data = self._load_data()
        bound: list[str] = data.get("bound_umos", [])
        if umo not in bound:
            yield event.plain_result("当前会话没有绑定过学习通提醒")
            return
        bound.remove(umo)
        data["bound_umos"] = bound
        self._save_data(data)
        yield event.plain_result("已解绑当前会话")

    @filter.command("cxcheck")
    async def cmd_check(self, event: AstrMessageEvent):
        """立即巡检一次学习通作业"""
        self._last_umo = event.unified_msg_origin
        if not self._client:
            yield event.plain_result("插件未初始化，请先在 AstrBot 里重载插件")
            return
        if not self._has_account():
            yield event.plain_result(_NO_ACCOUNT_HINT)
            return
        yield event.plain_result("正在巡检学习通…")
        try:
            await self._check_and_notify()
            yield event.plain_result("✅ 巡检完成，如临近 DDL 会自动推送提醒")
        except Exception as e:
            yield event.plain_result(f"巡检失败：{e}\n可发送 cxlogin 重新选择登录方式")

    # ---------- 纯文本输入兜底 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_plain_input(self, event: AstrMessageEvent):
        """等待输入期间，用户直接发数字/验证码也能被识别（无需带命令前缀）"""
        pending = self._pending
        if not pending or pending["future"].done():
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        self._last_umo = event.unified_msg_origin
        kind = self._resolve_pending(text)
        if not kind:
            return
        event.stop_event()
        if kind == "menu":
            mode = _MODE_ALIASES.get(text.lower(), text)
            yield event.plain_result(f"✅ 已选择：{_MODE_LABELS.get(mode, text)}")
        elif kind in ("phone", "pwd"):
            pass  # 手机号/密码由 cxlogin 流程自己回复，这里不额外提示（也避免把密码回显）
        elif kind == "sms":
            yield event.plain_result("✅ 短信验证码已提交，正在登录学习通…")
        else:
            yield event.plain_result("✅ 图形验证码已提交，正在重试登录…")

    # ---------- 工具 ----------

    @staticmethod
    def _strip_cmd(text: str, *names: str) -> str:
        """去掉消息里的命令名，返回参数部分"""
        text = (text or "").strip()
        for name in names:
            for prefix in (f"/{name}", name):
                if text.startswith(prefix):
                    return text[len(prefix):].strip()
        return text
