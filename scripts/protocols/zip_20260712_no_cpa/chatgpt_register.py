"""
ChatGPT 邮箱注册脚本 — OpenAI Auth API
依赖: curl_cffi (TLS 指纹), 本地邮箱库存适配器

注册流程:
  1. OAuth 初始化 (带 login_hint=email) → 服务端自动发送验证码
  2. 轮询邮箱获取验证码
  3. POST /api/accounts/email-otp/validate   → 验证邮箱验证码
  4. GET  /about-you                          → 导航到 about-you 页面
  5. POST /api/accounts/create_account       → 创建账号 (姓名+生日)
  6. GET  /api/auth/callback/openai           → OAuth 回调
"""

import asyncio
import json
import time
import uuid
import re
import random
import string
from datetime import datetime, timedelta
from typing import Optional
from curl_cffi import requests

# 随机姓名池
FIRST_NAMES = ["James", "John", "Robert", "Michael", "David", "William", "Richard", "Joseph", "Thomas", "Chris",
               "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen",
               "Daniel", "Matthew", "Anthony", "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
              "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White"]

def random_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"

def random_age() -> int:
    return random.randint(24, 36)

def random_password(length: int = 16) -> str:
    if length < 12:
        length = 12
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*"
    # 先从每类各取一个, 保证满足要求
    must = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(special),
    ]
    # 剩余位置从全部字符集中随机填充
    all_chars = upper + lower + digits + special
    rest = random.choices(all_chars, k=length - len(must))
    pwd_list = must + rest
    random.shuffle(pwd_list)
    return "".join(pwd_list)


# ============================================================
# Sentinel Token Provider (纯算法逆向实现)
# ============================================================
from sentinel_token import (
    SentinelTokenProvider as _SentinelImpl,
    generate_requirements_token,
    generate_enforcement_token,
    solve_pow,
    fnv1a_32,
)


class SentinelTokenProvider(_SentinelImpl):
    """继承 sentinel_token.py 中的实现, 支持 cookies 注入"""

    def __init__(self, impersonate: str = "firefox144", cookies: dict = None, proxy: str = None):
        super().__init__(impersonate=impersonate, cookies=cookies)
        self._proxy = proxy

    async def _get_session(self) -> requests.AsyncSession:
        if not self._session:
            kwargs = {"impersonate": self.impersonate}
            if self._proxy:
                kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
            kwargs["timeout"] = 60
            self._session = requests.AsyncSession(**kwargs)
        return self._session

    def set_session(self, session: requests.AsyncSession):
        """共享外部 session, 确保同一代理 IP"""
        self._session = session

    def set_cookies(self, cookies: dict):
        self._cookies = cookies


# ============================================================
# OpenAI Auth Client
# ============================================================
class OpenAIAuthClient:
    BASE_URL = "https://auth.openai.com"
    CHATGPT_URL = "https://chatgpt.com"

    def __init__(self, impersonate: str = "firefox144", sentinel: Optional[SentinelTokenProvider] = None, proxy: str = None):
        self.impersonate = impersonate
        self.proxy = proxy
        self.sentinel = sentinel or SentinelTokenProvider(proxy=proxy)
        self._session: Optional[requests.AsyncSession] = None
        self.device_id: str = str(uuid.uuid4())
        self.cookies: dict = {}

    async def _get_session(self) -> requests.AsyncSession:
        if not self._session:
            kwargs = {"impersonate": self.impersonate}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            kwargs["timeout"] = 60
            self._session = requests.AsyncSession(**kwargs)
        return self._session

    async def share_session_with_sentinel(self):
        """让 sentinel provider 共享同一 session, 确保同一代理 IP"""
        s = await self._get_session()
        self.sentinel.set_session(s)

    def _common_headers(self, referer: str = None) -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if referer:
            headers["referer"] = referer
        return headers

    async def _add_sentinel_headers(self, headers: dict, flow: str, referer: str) -> dict:
        """添加 sentinel token 头 (合并 token + so_token, 避免重复 PoW)"""
        token = await self.sentinel.get_token(flow, self.device_id)
        if token:
            headers["openai-sentinel-token"] = json.dumps(token)
            # so_token 复用同一缓存, 不再触发额外 PoW
            so_token = await self.sentinel.get_so_token(flow, self.device_id)
            if so_token:
                headers["openai-sentinel-so-token"] = json.dumps(so_token)
        return headers

    # ---- Step 6: 创建账号 ----
    async def create_account(self, name: str, birthdate: str) -> dict:
        """POST /api/accounts/create_account → 创建账号 (姓名+生日)"""
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/create_account"
        referer = f"{self.BASE_URL}/about-you"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "oauth_create_account",
            referer
        )

        body = {"name": name, "birthdate": birthdate}
        resp = await s.post(url, json=body, headers=headers)
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}

    # ---- Step 7: OAuth 回调 ----
    async def oauth_callback(self, code: str, state: str) -> dict:
        """GET /api/auth/callback/openai?code=... → OAuth 回调"""
        s = await self._get_session()
        url = f"{self.CHATGPT_URL}/api/auth/callback/openai"
        params = {
            "code": code,
            "scope": "openid email profile offline_access model.request model.read organization.read organization.write",
            "state": state,
        }
        headers = {
            "referer": f"{self.BASE_URL}/",
            "upgrade-insecure-requests": "1",
        }
        resp = await s.get(url, params=params, headers=headers, allow_redirects=False)
        return {"status": resp.status_code, "location": resp.headers.get("location", "")}

    # ---- Email 注册: 初始化页面 (带 login_hint=email) ----
    async def init_page_email(self, email: str) -> dict:
        """
        邮箱注册流程初始化 (从 chatgpt.com 表单提交邮箱):
          chatgpt.com → /api/auth/csrf → /api/auth/signin/openai (带 login_hint=email)
          → auth.openai.com/api/accounts/authorize → 302 到 /email-verification
          服务端自动发送验证码邮件
        """
        s = await self._get_session()

        # 1a. 访问 chatgpt.com 获取初始 cookies
        await s.get(self.CHATGPT_URL)

        # 1b. 获取 CSRF token
        csrf_resp = await s.get(f"{self.CHATGPT_URL}/api/auth/csrf")
        if csrf_resp.status_code != 200:
            raise Exception(f"CSRF 请求失败: {csrf_resp.status_code}")
        csrf_token = csrf_resp.json().get("csrfToken")

        # 1c. 发起 OAuth 登录 (带 login_hint=email)
        from urllib.parse import urlencode
        params = urlencode({
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "login_hint": email,
        })
        signin_resp = await s.post(
            f"{self.CHATGPT_URL}/api/auth/signin/openai?{params}",
            data={"callbackUrl": f"{self.CHATGPT_URL}/", "csrfToken": csrf_token, "json": "true"},
            headers={"content-type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        loc = ""
        try:
            loc = signin_resp.json().get("url", "")
        except Exception:
            loc = signin_resp.headers.get("location", "")

        # 1d. 跟随重定向链: authorize → 302 → /email-verification (服务端自动发验证码)
        final_resp = None
        while loc:
            final_resp = await s.get(loc, allow_redirects=False)
            loc = final_resp.headers.get("location", "")
            if not loc:
                break

        # 从 session cookie jar 提取设备 ID
        for cookie in s.cookies.jar:
            if cookie.name == "oai-did":
                self.device_id = cookie.value
                break
        self.cookies = {c.name: c.value for c in s.cookies.jar}
        return {"status": final_resp.status_code if final_resp else 0, "cookies": self.cookies, "device_id": self.device_id}

    # ---- Email 注册: 验证邮箱 OTP ----
    async def validate_email_otp(self, code: str) -> dict:
        """POST /api/accounts/email-otp/validate → 验证邮箱验证码"""
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/email-otp/validate"
        referer = f"{self.BASE_URL}/email-verification"
        headers = self._common_headers(referer=referer)
        headers["accept"] = "application/json"
        body = {"code": code}
        resp = await s.post(url, json=body, headers=headers)
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}

    # ---- Email 注册: 设置密码 ----
    async def register_password_email(self, email: str, password: str) -> dict:
        """POST /api/accounts/user/register → 提交密码 (邮箱注册)"""
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/user/register"
        referer = f"{self.BASE_URL}/create-account/password"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "username_password_create",
            referer
        )
        body = {"password": password, "username": email}
        resp = await s.post(url, json=body, headers=headers)
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}

    async def close(self):
        if self._session:
            await self._session.close()


# ============================================================
# 邮箱注册编排器
# ============================================================
async def _try_register_one_email(
    email: str, password: str, name: str, birthdate: str,
    sentinel: SentinelTokenProvider, proxy: str, mail,
    _t0: float, otp_timeout: int = 10,
) -> dict | None:
    """单次邮箱注册尝试, 失败返回 None"""
    def _ts():
        return f"[{time.time()-_t0:.1f}s]"

    auth = OpenAIAuthClient(sentinel=sentinel, proxy=proxy)

    print(f"  {_ts()} [+] 邮箱: {email}")

    # 1. 初始化页面 (带 login_hint=email) → 服务端自动发送验证码
    print(f"  {_ts()} [..] 初始化 OpenAI 页面 (邮箱模式)...")
    since = time.time()  # 在初始化前记录时间, 验证码可能在初始化过程中就发出
    await auth.share_session_with_sentinel()
    try:
        init = await auth.init_page_email(email)
    except Exception as e:
        print(f"  [x] 初始化失败: {e}")
        await auth.close()
        return None
    print(f"  [+] 设备ID: {init['device_id'][:12]}...")

    sentinel.set_cookies(init["cookies"])

    # 2. 轮询邮箱获取验证码 (otp_timeout 秒超时)
    print(f"  {_ts()} [..] 等待邮箱验证码 ({otp_timeout}s 超时)...")
    code = await mail.poll_verify_code(email, timeout=otp_timeout, interval=3, since=since)
    if not code:
        print(f"  [x] {otp_timeout}s 内未收到验证码, 换邮箱")
        await auth.close()
        return None
    print(f"  [+] 验证码: {code}")

    # 3. 验证邮箱 OTP
    print(f"  {_ts()} [..] 提交邮箱验证码...")
    validate_result = await auth.validate_email_otp(code)
    if isinstance(validate_result, dict) and "error" in validate_result:
        err_code = validate_result['error'].get('code', '')
        print(f"  [x] 验证失败: {err_code}")
        await auth.close()
        return None
    print(f"  {_ts()} [+] 邮箱验证通过")

    # 4. 导航到 about-you 页面 (OTP 验证后直接跳转)
    about_you_url = validate_result.get("continue_url", "") if isinstance(validate_result, dict) else ""
    if about_you_url:
        print(f"  {_ts()} [..] 导航到 about-you 页面...")
        s = await auth._get_session()
        await s.get(about_you_url, headers={
            "referer": f"{auth.BASE_URL}/email-verification"
        })
        print(f"  {_ts()} [+] 已访问 about-you")

    # 5. 创建账号
    print("  [..] 创建账号...")
    create_ok = False
    for create_attempt in range(3):
        create_result = await auth.create_account(name, birthdate)
        if isinstance(create_result, dict) and "error" in create_result:
            err_code = create_result['error'].get('code', '')
            if err_code == 'registration_disallowed' and create_attempt < 2:
                print(f"  [!] registration_disallowed, 重试 ({create_attempt+1}/3)")
                await asyncio.sleep(2)
                continue
            print(f"  [x] 创建失败: {err_code}")
            break
        print("  [+] 账号创建成功")
        create_ok = True
        break

    if not create_ok:
        await auth.close()
        return None

    # 8. OAuth 回调 (continue_url 包含 code 和 state)
    access_token = None
    session_token = ""
    account_id = ""
    continue_url = create_result.get("continue_url", "") if isinstance(create_result, dict) else ""
    if continue_url:
        print("  [..] OAuth 回调...")
        s = await auth._get_session()
        # 直接访问 continue_url (chatgpt.com/api/auth/callback/openai?code=...&state=...)
        cb_resp = await s.get(continue_url, allow_redirects=True)
        print(f"  [+] 回调状态: {cb_resp.status_code}")

        # 9. 获取 session (accessToken)
        print("  [..] 获取 session...")
        sess_resp = await s.get(f"{auth.CHATGPT_URL}/api/auth/session")
        try:
            sess_data = sess_resp.json()
            access_token = sess_data.get("accessToken", "")
            session_token = sess_data.get("sessionToken", "")
            account_id = sess_data.get("account", {}).get("id", "")
            if access_token:
                print(f"  [+] accessToken: {access_token[:20]}...")
            else:
                print(f"  [!] 未获取到 accessToken: {sess_resp.text[:200]}")
        except Exception:
            print(f"  [!] session 解析失败: {sess_resp.text[:200]}")

    print()
    print("  +------------------------------------+")
    print(f"  | [+] 邮箱注册成功                    |")
    print("  +------------------------------------+")
    print(f"  | 邮箱:   {email:<24} |")
    print(f"  | 密码:   {password:<24} |")
    print(f"  | 姓名:   {name:<24} |")
    print("  +------------------------------------+")

    await auth.close()
    return {
        "email": email,
        "password": password,
        "name": name,
        "access_token": access_token,
        "session_token": session_token,
        "account_id": account_id,
    }


async def register_chatgpt_email(
    email: str = None,
    password: str = None,
    name: str = None,
    age: int = None,
    max_retries: int = 10,
    otp_timeout: int = 30,
    sentinel: Optional[SentinelTokenProvider] = None,
    proxy: str = None,
):
    """
    邮箱注册流程 (30s 没收到验证码就换邮箱):
      1. OpenAI 初始化 (带 login_hint=email) → 服务端自动发送验证码
      2. 30s 内未收到验证码 → 换邮箱重试
      3. 验证邮箱 OTP → 设置密码 → 创建账号 → OAuth 回调
    """
    raise RuntimeError("Use the local survival experiment adapter with the configured mailbox inventory")

    sentinel = sentinel or SentinelTokenProvider(proxy=proxy)
    mail = MailClient()

    _t0 = time.time()

    print()
    print("  +-----------------------------+")
    print("  |   ChatGPT 邮箱注册           |")
    print("  +-----------------------------+")
    print()

    name = name or random_name()
    age = age or random_age()
    password = password or random_password()
    birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    print(f"  身份: {name}  年龄: {age}  密码: {password[:4]}****")

    for attempt in range(1, max_retries + 1):
        print(f"\n  -- 尝试 {attempt}/{max_retries} --")

        # 获取邮箱
        if email and attempt == 1:
            cur_email = email
        else:
            try:
                cur_email = await mail.get_unbound_email()
            except Exception:
                cur_email = None
            if not cur_email:
                print("  [x] 没有可用邮箱")
                break

        result = await _try_register_one_email(
            cur_email, password, name, birthdate,
            sentinel, proxy, mail, _t0, otp_timeout,
        )
        if result:
            return result

    print(f"\n  [x] {max_retries} 次尝试后仍失败")
    return None



# ============================================================
# Main
# ============================================================

PROXY = ""
_accounts_lock = asyncio.Lock()

def _append_account(line: str):
    """同步文件写入 (供 asyncio.to_thread 调用)"""
    with open("accounts.txt", "a", encoding="utf-8") as f:
        f.write(line)


async def main():
    import sys
    print()
    print("  ===================================")
    print("  |       ChatGPT 批量注册工具         |")
    print("  ===================================")
    print()

    # 命令行: python chatgpt_register.py [email] [并发数] [总次数]
    provider = sys.argv[1] if len(sys.argv) > 1 else "email"
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    total = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    if provider == "email":
        print(f"  模式: 邮箱注册  并发: {concurrency}  总数: {total}")
        print()

        async def _run_email_one(idx: int, sem: asyncio.Semaphore):
            async with sem:
                tag = f"[{idx}]"
                print(f"\n{'='*50}")
                print(f" {tag} 开始邮箱注册")
                print(f"{'='*50}")
                try:
                    result = await register_chatgpt_email(proxy=PROXY)
                    return result
                except Exception as e:
                    print(f" {tag} 注册异常: {e}")
                    return None

        semaphore = asyncio.Semaphore(concurrency)
        tasks = [_run_email_one(i + 1, semaphore) for i in range(total)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r)
        print()
        print(f"  {'='*40}")
        print(f"  结果: {success}/{total} 成功  ({success*100//total}%)")
        print(f"  {'='*40}")
        return

    print(f"  [x] 未知模式: {provider}, 可选: email")


if __name__ == "__main__":
    asyncio.run(main())
