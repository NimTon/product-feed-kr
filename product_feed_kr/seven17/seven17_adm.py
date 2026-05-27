"""seven17.kr / 그누보드5：管理员会话（登录）。"""

from __future__ import annotations

from urllib.parse import quote

from playwright.sync_api import Page


def login_url_with_redirect(base: str, redirect_full_url: str) -> str:
    """redirect_full_url 须为登录成功后跳转的完整 URL（含 https）。"""
    b = base.rstrip("/")
    return f"{b}/bbs/login.php?url={quote(redirect_full_url, safe='')}"


def login_admin(
    page: Page,
    *,
    base: str,
    mb_id: str,
    mb_password: str,
    redirect_full_url: str,
    goto_timeout_ms: int = 120_000,
) -> None:
    page.goto(
        login_url_with_redirect(base, redirect_full_url),
        wait_until="domcontentloaded",
        timeout=goto_timeout_ms,
    )
    page.fill("#login_id", mb_id)
    page.fill("#login_pw", mb_password)
    page.click('form#flogin input[type="submit"]')
    # networkidle 在后台页常被长连接/轮询拖很久；load 即可进入下一步。
    page.wait_for_load_state("load", timeout=goto_timeout_ms)
