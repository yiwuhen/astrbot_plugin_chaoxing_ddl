# -*- coding: utf-8 -*-
"""
学习通（超星）非官方接口客户端

接口思路来自社区逆向成果（参考 DanMo661/xuexitong-cli、chaoxing-mcp、Yuuki 的学习通 APP 逆向等，
2026 年实测可用）：

登录（三种方式，可自选）
  A. Cookie   ：直接用浏览器 Cookie，最省事
  B. 密码     ：APP 协议 POST https://passport2-api.chaoxing.com/v11/loginregister?cx_xxt_passport=json
                logininfo = Base64(AES-ECB(json{"uname","password"}, key=z4ok6lu^oWp4_AES)), loginType=1, entype=1
                失败回退网页协议 POST https://passport2.chaoxing.com/fanyalogin（可能需要图形验证码）
  C. 短信验证码：POST https://passport2-api.chaoxing.com/api/sendcaptcha
                （发码 enc = MD5(手机号 + jsDyctOCnay7uotq + 毫秒时间戳)）
                再 logininfo = Base64(AES-ECB(json{"uname","code"})), loginType=2, entype=1

数据抓取
  1. fid：GET https://i.chaoxing.com/base 页面变量
  2. 课程列表：GET https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata?courseType=1&courseFid=<fid>
  3. 课程中间页：GET https://mooc1.chaoxing.com/visit/stucoursemiddle 提取 enc / openc / workEnc / examEnc
  4. 作业列表：GET https://mooc1.chaoxing.com/mooc2/work/list?...&enc=<workEnc>&stuenc=<页面enc>
  5. 作业详情：列表项 data 直链，解析「作答时间: xx 至 yy」，yy 即截止时间（无年份，就近推断）

注意：学习通无官方开放 API，接口改版可能失效，届时需对应更新本文件的解析规则。
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

logger = logging.getLogger(__name__)

# 网页登录 AES key = iv（逆向自 passport2-static.chaoxing.com/js/fanya/login.js）
_WEB_AES_KEY = b"u2oh6Vu^HWe4_AES"
# APP 登录 logininfo 的 AES-ECB 密钥（Keys.KEY_26）
_APP_AES_KEY = b"z4ok6lu^oWp4_AES"
# 发送短信验证码的 enc 盐（Keys.KEY_27）
_SMS_ENC_KEY = "jsDyctOCnay7uotq"

_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://i.chaoxing.com/",
}

_APP_HEADERS = {
    "User-Agent": (
        "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 6 Build/AP1A.240505.004) "
        "(schild:051343d7c970cf951aaa13a49e1ec102) (device:Pixel 6) "
        "Language/zh_CN_#Hans com.chaoxing.mobile/ChaoXingStudy_3_6.7.1_android_phone_10935_311 "
        "(@Kalimdor)_bc3fa95e31ca4621998848e3746cd20a"
    ),
    "Accept-Language": "zh_CN_#Hans",
}

# 视为「未完成」的作业/考试状态文本（尽量收集各种叫法，宁可多报不可漏报）
_UNFINISHED_STATUS = {
    "未交", "未提交", "待提交", "待做", "未做", "未完成", "待完成",
    "未开始", "待开始", "待作答", "待批阅", "进行中",
}
# 供插件侧（main.py）使用，避免两处状态表不一致
UNFINISHED_STATUS = _UNFINISHED_STATUS

# 登录方式
LOGIN_MODE_AUTO = "auto"
LOGIN_MODE_PASSWORD = "password"
LOGIN_MODE_SMS = "sms"
LOGIN_MODE_COOKIE = "cookie"


class NeedCaptcha(Exception):
    """网页登录需要图形验证码。plugin 侧捕获后向用户索要，再回填 validate 重试。"""

    def __init__(self, image: bytes = b"", message: str = ""):
        super().__init__(message or "需要图形验证码")
        self.image = image
        self.message = message


def aes_encrypt_web(text: str) -> str:
    """网页登录：AES-CBC + PKCS7 + Base64，key = iv = u2oh6Vu^HWe4_AES"""
    data = text.encode("utf-8")
    data += bytes([16 - len(data) % 16]) * (16 - len(data) % 16)
    cipher = AES.new(_WEB_AES_KEY, AES.MODE_CBC, _WEB_AES_KEY)
    return base64.b64encode(cipher.encrypt(data)).decode("utf-8")


def encrypt_login_info(payload: dict) -> str:
    """APP 登录：Base64(AES-ECB-PKCS7(紧凑 JSON))"""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    cipher = AES.new(_APP_AES_KEY, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(raw, AES.block_size))).decode("utf-8")


def sms_send_enc(phone: str, timestamp_ms: int) -> str:
    """发送短信验证码的 enc = MD5(手机号 + 盐 + 毫秒时间戳)"""
    return hashlib.md5(f"{phone}{_SMS_ENC_KEY}{timestamp_ms}".encode("utf-8")).hexdigest()


def _infer_year(dt: datetime, now: datetime) -> datetime:
    """详情页时间无年份，取与今天最接近的年份补全"""
    for y in sorted({now.year - 1, now.year, now.year + 1}, key=lambda y: abs(y - now.year)):
        try:
            return dt.replace(year=y)
        except ValueError:
            continue
    return dt


class ChaoxingClient:
    """学习通客户端：多方式登录 + 课程/作业/考试 DDL 抓取"""

    def __init__(self, phone: str = "", password: str = "", cookie: str = ""):
        self.phone = (phone or "").strip()
        self.password = (password or "").strip()
        self.cookie = (cookie or "").strip()
        # 由插件注入：登录编排入口（决定用哪种方式登录）
        self.login_runner = None
        # 网页登录遇到图形验证码时的回调：async (image: bytes, tip: str) -> str|None
        self.captcha_handler = None
        self.fid: Optional[str] = None
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            trust_env=False,  # 绕过本机代理，避免代理掉线殃及
            timeout=30,
            follow_redirects=True,
            headers=_WEB_HEADERS,
        )
        if self.cookie:
            self.apply_cookie(self.cookie)

    # ---------- Cookie 与登录态 ----------

    def apply_cookie(self, cookie: str):
        """把浏览器复制的 Cookie 字符串（a=1; b=2）塞进 cookie jar"""
        self.cookie = (cookie or "").strip()
        for part in self.cookie.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            try:
                self.client.cookies.set(k, v, domain=".chaoxing.com", path="/")
            except Exception:
                pass

    def export_cookies(self) -> dict:
        """导出 cookie jar，供插件持久化（重启后免登录）"""
        out = {}
        try:
            for c in self.client.cookies.jar:
                if c.name and c.value:
                    out[c.name] = c.value
        except Exception:
            pass
        return out

    def import_cookies(self, cookies: dict):
        for k, v in (cookies or {}).items():
            try:
                self.client.cookies.set(k, v, domain=".chaoxing.com", path="/")
            except Exception:
                pass

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def mark_logged_out(self):
        self._logged_in = False

    def mark_logged_in(self):
        self._logged_in = True

    async def close(self):
        await self.client.aclose()

    async def check_cookie(self) -> bool:
        """校验当前 cookie jar 是否仍然有效（能拿到个人主页即可）"""
        try:
            resp = await self.client.get("https://i.chaoxing.com/base")
            if "passport2.chaoxing.com/login" in str(resp.url):
                return False
            text = resp.text
            m = re.search(r'fid["\']?\s*[:=]\s*["\']?(\d+)', text)
            if m:
                self.fid = m.group(1)
                return True
            return "个人空间" in text or "我的课程" in text
        except Exception:
            return False

    # ---------- 方式 C：短信验证码登录 ----------

    async def send_sms_code(self) -> str:
        """向账号绑定手机号发送短信验证码，返回服务端提示文本"""
        if not self.phone:
            raise RuntimeError("未配置手机号，无法使用短信验证码登录")
        ts = int(time.time() * 1000)
        resp = await self.client.post(
            "https://passport2-api.chaoxing.com/api/sendcaptcha",
            data={
                "to": self.phone,
                "countrycode": "86",
                "time": str(ts),
                "enc": sms_send_enc(self.phone, ts),
            },
            headers=_APP_HEADERS,
        )
        return resp.text.strip()

    async def login_by_sms(self, code: str) -> bool:
        """短信验证码登录（loginType=2）"""
        async with self._login_lock:
            self._logged_in = False
            return await self._login_app(
                payload={"uname": self.phone, "code": code.strip()}, login_type=2
            )

    # ---------- 方式 B：密码登录 ----------

    async def login_by_password(self) -> bool:
        """密码登录：先走 APP 协议（通常不弹图形验证码），失败回退网页协议"""
        async with self._login_lock:
            self._logged_in = False
            try:
                ok = await self._login_app(
                    payload={"uname": self.phone, "password": self.password}, login_type=1
                )
                if ok:
                    return True
            except Exception:
                pass
            # 回退网页协议（可能需要图形验证码）；此处已在锁内，调用不加锁版本
            return await self._login_web_unlocked()

    async def _login_app(self, payload: dict, login_type: int) -> bool:
        resp = await self.client.post(
            "https://passport2-api.chaoxing.com/v11/loginregister",
            params={"cx_xxt_passport": "json"},
            data={
                "logininfo": encrypt_login_info(payload),
                "countrycode": "86",
                "loginType": str(login_type),
                "roleSelect": "true",
                "entype": "1",
            },
            headers=_APP_HEADERS,
        )
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"登录接口返回异常: {resp.text[:120]}")
        if not data.get("status"):
            mes = data.get("mes") or "未知原因"
            raise RuntimeError(f"登录失败: {mes}")
        # 成功后会返回 SSO 地址，需要跟随一次才能把学习通站点 cookie 拿到
        url = data.get("url") or "https://i.chaoxing.com"
        try:
            await self.client.get(url)
        except Exception:
            pass
        await self.client.get("https://i.chaoxing.com/base")
        self._logged_in = True
        return True

    # ---------- 方式 B 回退：网页协议 ----------

    async def login_web(self) -> bool:
        """网页协议登录（fanyalogin）。遇到图形验证码时通过 captcha_handler 与用户交互。"""
        async with self._login_lock:
            return await self._login_web_unlocked()

    async def _login_web_unlocked(self) -> bool:
        """实际执行网页登录（调用方需已持有 _login_lock）"""
        self._logged_in = False
        validate = ""
        last_err = ""
        for _ in range(3):
            try:
                resp = await self.client.post(
                    "https://passport2.chaoxing.com/fanyalogin",
                    data={
                        "fid": "-1",
                        "uname": aes_encrypt_web(self.phone),
                        "password": aes_encrypt_web(self.password),
                        "refer": "https://i.chaoxing.com",
                        "t": "true",
                        "forbidotherlogin": "0",
                        "validate": validate,
                        "doubleFactorLogin": "0",
                        "independentId": "0",
                    },
                )
                data = resp.json()
            except Exception as e:
                last_err = f"请求异常: {e}"
                break

            if data.get("status"):
                self._logged_in = True
                return True

            mes = str(data.get("mes", "") or "").strip()
            last_err = mes or "未知原因"
            need_vcode = (
                data.get("needVcode")
                or "验证码" in mes
                or "vcode" in mes.lower()
                or "captcha" in mes.lower()
            )
            if need_vcode and self.captcha_handler:
                image = await self.fetch_captcha_image()
                code = await self.captcha_handler(image, mes)
                if not code:
                    raise RuntimeError("登录需要图形验证码，但未获取到输入（已超时或取消）")
                validate = code
                continue
            if need_vcode:
                raise RuntimeError(f"登录需要图形验证码，但插件未提供交互通道: {mes}")
            break

        # 旧版明文接口兜底
        try:
            resp = await self.client.get(
                "https://passport2-api.chaoxing.com/v11/loginregister",
                params={
                    "uname": self.phone,
                    "code": self.password,
                    "loginType": "1",
                    "roleSelect": "true",
                    "cx_xxt_passport": "json",
                },
            )
            if resp.json().get("mes") == "验证通过":
                self._logged_in = True
                return True
        except Exception:
            pass

        raise RuntimeError(f"学习通登录失败: {last_err}")

    async def fetch_captcha_image(self) -> bytes:
        """拉取网页图形验证码图片（每次请求都刷新）"""
        try:
            resp = await self.client.get(
                "https://passport2.chaoxing.com/num/code",
                params={"_": str(int(time.time() * 1000))},
                headers={"Referer": "https://passport2.chaoxing.com/login?fid=&refer="},
            )
            return resp.content
        except Exception:
            return b""

    # ---------- 统一登录入口 ----------

    async def login(self) -> bool:
        """登录入口：交给插件的 login_runner 编排；没有编排时按 密码 → 网页 兜底"""
        if self._logged_in:
            return True
        if self.login_runner is not None:
            return await self.login_runner()
        return await self.login_by_password()

    async def _ensure_login(self):
        if not self._logged_in:
            await self.login()

    async def _get(self, url: str, *, params: dict | None = None) -> httpx.Response:
        """带自动重登的 GET：检测到被踢回登录页则重登一次"""
        await self._ensure_login()
        resp = await self.client.get(url, params=params)
        text_head = resp.text[:2000]
        if "passport2.chaoxing.com" in str(resp.url) or "请登录" in text_head or (
            "location.href" in text_head and "passport" in text_head
        ):
            self._logged_in = False
            await self.login()
            resp = await self.client.get(url, params=params)
        return resp

    # ---------- 课程 ----------

    async def _get_fid(self):
        if self.fid:
            return
        resp = await self._get("https://i.chaoxing.com/base")
        m = re.search(r'fid["\']?\s*[:=]\s*["\']?(\d+)', resp.text)
        if not m:
            raise RuntimeError("无法从个人主页获取 fid，可能登录失效或页面改版")
        self.fid = m.group(1)

    async def get_courses(self) -> list[dict]:
        """返回 [{'course_id','class_id','cpi','name'}]"""
        await self._get_fid()
        resp = await self._get(
            "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
            params={"courseType": "1", "courseFid": self.fid},
        )
        html = resp.text
        courses: list[dict] = []
        seen = set()
        for m in re.finditer(
            r'courseid=(\d+)&[^"\']*?clazzid=(\d+)(?:[^"\']*?cpi=(\d+))?', html, re.I
        ):
            course_id, class_id, cpi = m.group(1), m.group(2), m.group(3) or ""
            key = (course_id, class_id)
            if key in seen:
                continue
            seen.add(key)
            # 课名一般在链接后方的 course-name span 的 title 里
            tail = html[m.end(): m.end() + 2000]
            nm = re.search(r'course-name[^>]*title="([^"]+)"', tail) or re.search(
                r'title="([^"]+)"[^>]*>\s*<[^>]*course-name', tail
            )
            name = nm.group(1).strip() if nm else f"课程{course_id}"
            courses.append(
                {
                    "course_id": course_id,
                    "class_id": class_id,
                    "cpi": cpi,
                    "name": name,
                }
            )
        if not courses:
            raise RuntimeError("未解析到任何课程，页面可能已改版")
        return courses

    # ---------- 作业 / 考试 DDL ----------

    async def _get_middle_page_enc(self, course: dict) -> dict:
        """课程中间页，提取 enc / openc / workEnc / examEnc"""
        resp = await self._get(
            "https://mooc1.chaoxing.com/visit/stucoursemiddle",
            params={
                "courseid": course["course_id"],
                "clazzid": course["class_id"],
                "cpi": course["cpi"],
                "ismooc2": "1",
                "v": "2",
            },
        )
        html = resp.text

        def _input_val(input_id: str) -> str:
            m = re.search(
                rf'<input[^>]*id="{input_id}"[^>]*value="([^"]*)"', html
            ) or re.search(
                rf'<input[^>]*value="([^"]*)"[^>]*id="{input_id}"', html
            )
            return m.group(1) if m else ""

        return {
            "enc": _input_val("enc"),
            "openc": _input_val("openc"),
            "workEnc": _input_val("workEnc"),
            "examEnc": _input_val("examEnc"),
        }

    async def get_work_deadlines(self, course: dict) -> list[dict]:
        """抓取一门课的未交作业截止时间，返回 [{'title','ddl','status','url','kind'}]"""
        encs = await self._get_middle_page_enc(course)
        if not encs["workEnc"]:
            return []
        params = {
            "courseId": course["course_id"],
            "classId": course["class_id"],
            "cpi": course["cpi"],
            "ut": "s",
            "openc": encs["openc"],
            "enc": encs["workEnc"],
            "t": str(int(time.time() * 1000)),
            "stuenc": encs["enc"],
            "isdisplaytable": "2",
        }
        resp = await self._get("https://mooc1.chaoxing.com/mooc2/work/list", params=params)
        items: list[dict] = []

        # 每个作业条目是 <li ... onclick="goTask(this);" data="详情直链" ... aria-label="标题 ; 状态">
        for li_m in re.finditer(r"<li[^>]*goTask[^>]*>(.*?)</li>", resp.text, re.S | re.I):
            tag = li_m.group(0)
            data_m = re.search(r'data="([^"]+)"', tag)
            if not data_m:
                continue
            aria = re.search(r'aria-label="([^"]*)"', tag)
            title, status = "", ""
            if aria:
                # 页面可能用英文分号或中文分号分隔「标题 ; 状态」
                parts = re.split(r"[;；]", aria.group(1))
                title = parts[0].strip()
                status = parts[1].strip() if len(parts) > 1 else ""
            if not title:
                txt = re.sub(r"<[^>]+>", " ", tag)
                txt = re.sub(r"\s+", " ", txt).strip()
                title = txt[:50]
            detail_url = data_m.group(1)
            if detail_url.startswith("/"):
                detail_url = "https://mooc1.chaoxing.com" + detail_url

            ddl = None
            if status in _UNFINISHED_STATUS:
                ddl = await self._fetch_detail_deadline(detail_url)
            logger.debug(f"学习通作业条目: [{course['name']}] {title!r} 状态={status!r} ddl={ddl}")
            items.append(
                {
                    "course": course["name"],
                    "title": title,
                    "ddl": ddl,
                    "status": status,
                    "url": detail_url,
                    "kind": "作业",
                }
            )
        return items

    async def _fetch_detail_deadline(self, url: str) -> Optional[datetime]:
        """作业详情页解析「作答时间: xx 至 yy」，yy 即截止时间（无年份，就近推断）"""
        try:
            resp = await self._get(url)
            m = re.search(
                r"作答时间[：:]\s*([0-9\-/.]+\s+[0-9:]+)\s*至\s*([0-9\-/.]+\s+[0-9:]+)",
                resp.text,
            )
            if not m:
                return None
            end_str = m.group(2).replace("/", "-")
            now = datetime.now()
            for fmt in ("%m-%d %H:%M", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(end_str, fmt)
                    if "%Y" not in fmt:
                        dt = _infer_year(dt, now)
                    return dt
                except ValueError:
                    continue
        except Exception:
            pass
        return None

    async def get_exam_deadlines(self, course: dict) -> list[dict]:
        """抓取一门课的考试安排（尽力而为：部分学校页面结构不同会解析不到，静默返回空）"""
        try:
            encs = await self._get_middle_page_enc(course)
            if not encs["examEnc"]:
                return []
            params = {
                "courseId": course["course_id"],
                "classId": course["class_id"],
                "cpi": course["cpi"],
                "ut": "s",
                "openc": encs["openc"],
                "enc": encs["examEnc"],
                "t": str(int(time.time() * 1000)),
                "stuenc": encs["enc"],
            }
            resp = await self._get("https://mooc1.chaoxing.com/mooc2/exam/list", params=params)
            items: list[dict] = []
            for li_m in re.finditer(r"<li[^>]*goTask[^>]*>(.*?)</li>", resp.text, re.S | re.I):
                tag = li_m.group(0)
                aria = re.search(r'aria-label="([^"]*)"', tag)
                title, status = "", ""
                if aria:
                    parts = aria.group(1).split(";")
                    title = parts[0].strip()
                    status = parts[1].strip() if len(parts) > 1 else ""
                if status not in _UNFINISHED_STATUS:
                    continue
                dt_m = re.search(
                    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", re.sub(r"<[^>]+>", " ", tag)
                )
                ddl = None
                if dt_m:
                    try:
                        ddl = datetime.strptime(dt_m.group(1), "%Y-%m-%d %H:%M")
                    except ValueError:
                        pass
                items.append(
                    {
                        "course": course["name"],
                        "title": title or "未命名考试",
                        "ddl": ddl,
                        "status": status,
                        "url": "",
                        "kind": "考试",
                    }
                )
            return items
        except Exception:
            return []

    async def fetch_all_deadlines(
        self, course_filter: list[str] | None = None, include_exams: bool = False
    ) -> list[dict]:
        """抓取全部课程的未完成作业（可选考试）DDL，返回按截止时间排序的列表"""
        courses = await self.get_courses()
        if course_filter:
            kw = [k.lower() for k in course_filter if k]
            if kw:
                courses = [c for c in courses if any(k in c["name"].lower() for k in kw)]
        results: list[dict] = []
        failures: list[str] = []
        for course in courses:
            try:
                results.extend(await self.get_work_deadlines(course))
                if include_exams:
                    results.extend(await self.get_exam_deadlines(course))
            except Exception as e:
                # 单门课失败不影响其他课程，但要记下来
                failures.append(f"{course['name']}: {e}")
        if courses and len(failures) == len(courses):
            # 全部失败通常是登录态失效或接口改版，不能伪装成「没有作业」
            raise RuntimeError(f"所有课程都抓取失败（可能是登录失效）：{failures[0]}")
        if failures:
            logger.warning(f"学习通部分课程抓取失败: {failures[:3]}")
        results.sort(key=lambda x: x["ddl"] or datetime.max)
        return results
