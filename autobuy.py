# autobuy.py
import sys, asyncio, time, os, csv, re, json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urljoin
from typing import List, Dict, Any, Optional

# ---- WindowsでPlaywrightのサブプロセスを安定化 ----
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from playwright.async_api import async_playwright, Page, BrowserContext

DEFAULT_BASE_URL = "https://only-five.jp"
EXPECTED_CSV_HEADER = [
    "timestamp","creator","index","post_url","status",
    "button_text","button_class","button_href","elapsed_ms","error"
]

# ========= ユーティリティ =========
def to_ts(dt_str: str, tzname: str = "Asia/Tokyo") -> float:
    """'YYYY-mm-dd HH:MM:SS' → epoch(ts)  / 秒は0に丸める（保険）"""
    try:
        tz = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        tz = timezone(timedelta(hours=9))
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    dt = dt.replace(second=0, microsecond=0)
    return dt.timestamp()

def ensure_csv(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(EXPECTED_CSV_HEADER)

def normalize_csv(path: str):
    """古いヘッダでも読めるよう最小補正（不足列を埋める）"""
    if not os.path.exists(path):
        return
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if not rows:
            return
        if rows[0] == EXPECTED_CSV_HEADER:
            return
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as g:
            w = csv.writer(g)
            w.writerow(EXPECTED_CSV_HEADER)
            for r in rows[1:]:
                r = (r + [""] * len(EXPECTED_CSV_HEADER))[:len(EXPECTED_CSV_HEADER)]
                w.writerow(r)
        os.replace(tmp, path)
    except Exception:
        pass

def precise_wait_to_ts(target_ts: float):
    """target_ts ぴったり待機（最後は高分解能）。"""
    while True:
        now = time.time()
        delta = target_ts - now
        if delta <= 1.5:
            break
        time.sleep(min(0.25, delta - 1.5))
    while True:
        now = time.time()
        delta = target_ts - now
        if delta <= 0.2:
            break
        time.sleep(0.05)
    deadline = time.perf_counter() + max(0.0, target_ts - time.time())
    while True:
        if time.perf_counter() >= deadline:
            break
        time.sleep(0.0005)

# ========= Bootstrap（手動ログイン） =========
async def _bootstrap_login_async(base_url: str, auth_state_path: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(base_url, wait_until="domcontentloaded")
        print("[*] ブラウザを閉じるとセッションを保存します。")
        try:
            await page.wait_for_event("close")
        except Exception:
            pass
        state = await ctx.storage_state()
        with open(auth_state_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
        await browser.close()
        print(f"[+] セッション保存: {auth_state_path}")

def bootstrap_login(base_url: str = DEFAULT_BASE_URL, auth_state_path: str = "auth.json"):
    return asyncio.run(_bootstrap_login_async(base_url, auth_state_path))

# ========= 自動ログイン =========
async def is_login_required(page: Page) -> bool:
    try:
        if await page.locator("input[type='password']").count() > 0:
            return True
        if await page.get_by_text("ログイン", exact=False).count() > 0:
            return True
    except Exception:
        pass
    return False

async def try_auto_login(ctx: BrowserContext, page: Page, base_url: str,
                         email: Optional[str], password: Optional[str],
                         login_url_candidates: Optional[list] = None) -> bool:
    if not email or not password:
        return False
    if not await is_login_required(page):
        if not login_url_candidates:
            login_url_candidates = [
                urljoin(base_url, "/users/sign_in"),
                urljoin(base_url, "/login"),
                urljoin(base_url, "/sign_in"),
            ]
        for u in login_url_candidates:
            try:
                await page.goto(u, wait_until="domcontentloaded", timeout=4000)
                if await is_login_required(page):
                    break
            except Exception:
                continue
    if not await is_login_required(page):
        return True

    email_candidates = [
        "input[name='email']","input[type='email']","input[name='user[email]']","#email",
    ]
    pass_candidates = [
        "input[name='password']","input[type='password']","input[name='user[password]']","#password",
    ]
    submit_candidates = [
        "button[type='submit']","input[type='submit']","text=ログイン","text=Sign in",
    ]
    filled = False
    for sel in email_candidates:
        if await page.locator(sel).count() > 0:
            await page.fill(sel, email); filled = True; break
    if not filled: return False
    filled = False
    for sel in pass_candidates:
        if await page.locator(sel).count() > 0:
            await page.fill(sel, password); filled = True; break
    if not filled: return False
    clicked = False
    for sel in submit_candidates:
        if await page.locator(sel).count() > 0:
            await page.click(sel); clicked = True; break
    if not clicked:
        try:
            await page.keyboard.press("Enter"); clicked = True
        except Exception:
            pass
    if not clicked: return False

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=4000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(300)
    except Exception:
        pass

    if await is_login_required(page):
        return False
    try:
        state = await ctx.storage_state()
        with open("auth.json", "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass
    return True

# ========= 高速化：一覧をHTML直読み =========
POST_RE = re.compile(r'href="(/posts/\d+)"')

async def fast_fetch_post_urls(page: Page, list_url: str, base_url: str, topn: int = 5) -> List[str]:
    html = await page.evaluate(
        """async (url) => {
            const res = await fetch(url, {cache: 'no-store'});
            return await res.text();
        }""",
        list_url
    )
    hrefs: List[str] = []
    for m in POST_RE.finditer(html):
        full = urljoin(base_url, m.group(1))
        if full not in hrefs:
            hrefs.append(full)
        if len(hrefs) >= topn:
            break
    return hrefs

# ========= 詳細判定（ページ再利用・即終了） =========
async def judge_on_page(detail_page: Page, url: str, buy_selector: str, t0: float,
                        status_csv: str, creator_name: str, idx: int,
                        perform_purchase: bool, post_click_timeout_ms: int):
    """
    detail_page を再利用して判定・必要でクリック。
    戻り: (status, url, text, class, href, elapsed_ms)
    """
    try:
        await detail_page.goto(url, timeout=2000, wait_until="domcontentloaded")
        btn = detail_page.locator(buy_selector).first
        status = "not_found"
        btn_text = btn_class = btn_href = ""
        if await btn.count() > 0:
            btn_class = (await btn.get_attribute("class")) or ""
            raw_text = await btn.inner_text()
            btn_text  = (raw_text or "").strip().replace("\n", "")
            btn_href  = (await btn.get_attribute("href")) or ""

            if ("disabled" not in btn_class) and ("charge_confirmation" in btn_href) and ("購入" in btn_text):
                status = "buyable"
                if perform_purchase:
                    try:
                        await btn.click()
                        await detail_page.wait_for_url("**/charge_confirmation*", timeout=post_click_timeout_ms)
                        status = "charge_confirmation"
                    except Exception:
                        status = "click_failed"
            elif ("disabled" in btn_class) or ("売り切れ" in btn_text):
                status = "sold_out"
            else:
                status = "unknown"

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        with open(status_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now().isoformat(timespec="seconds"),
                creator_name, idx, url, status,
                btn_text, btn_class, btn_href, f"{elapsed_ms:.1f}", ""
            ])
        return (status, url, btn_text, btn_class, btn_href, elapsed_ms)

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        with open(status_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now().isoformat(timespec="seconds"),
                creator_name, idx, url, "unknown", "", "", "",
                f"{elapsed_ms:.1f}", f"error: {e}"
            ])
        return ("unknown", url, "", "", "", elapsed_ms)

# ========= メイン（逐次＋ページ再利用） =========
async def _run_once_async(
    creator_name: str,
    creator_url: str,
    drop_time_ts: float,
    max_posts: int = 5,
    base_url: str = DEFAULT_BASE_URL,
    auth_state_path: str = "auth.json",
    headless: bool = True,
    block_resources = None,
    status_csv: str = "logs/status.csv",
    auto_login: bool = False,
    login_email: Optional[str] = None,
    login_password: Optional[str] = None,
    perform_purchase: bool = False,
    post_click_timeout_ms: int = 1500,
) -> Dict[str, Any]:

    ensure_csv(status_csv)
    normalize_csv(status_csv)

    block_resources = set(block_resources or [])

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-gpu",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-default-browser-check",
                "--no-first-run",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
            ],
        )
        ctx = await browser.new_context(
            storage_state=auth_state_path if os.path.exists(auth_state_path) else None,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            bypass_csp=True,
            java_script_enabled=True,
            service_workers="block",
        )

        # リソース遮断 & 分析系ドメイン遮断
        AD_ANALYTICS = ("googletagmanager.com","google-analytics.com","analytics.google.com")
        async def route_handler(route, request):
            if request.resource_type in block_resources:
                return await route.abort()
            url = request.url
            if any(d in url for d in AD_ANALYTICS):
                return await route.abort()
            return await route.continue_()
        await ctx.route("**/*", route_handler)

        # 一覧用ページ（cookie/セッションを持つ）をプレウォーム
        list_page = await ctx.new_page()
        await list_page.goto(creator_url, wait_until="domcontentloaded")

        # 必要なら事前にログイン
        if auto_login and await is_login_required(list_page):
            ok = await try_auto_login(ctx, list_page, base_url, login_email, login_password, None)
            if not ok:
                await ctx.close(); await browser.close()
                return {"finished": True, "found": "none", "error": "auto_login_failed"}

        # 判定用の detail_page を作っておく（実行時に new_page しない）
        detail_page = await ctx.new_page()

        # --- 定刻待機（:00） ---
        now_ts = time.time()
        if drop_time_ts > now_ts:
            precise_wait_to_ts(drop_time_ts)

        t0 = time.perf_counter()

        # --- 上位N件のURL抽出（fetchで最新HTMLを取得） ---
        hrefs = await fast_fetch_post_urls(list_page, creator_url, base_url, topn=max_posts)
        if not hrefs:
            await list_page.reload(wait_until="domcontentloaded")
            links = list_page.locator("a[href^='/posts/']")
            total = await links.count()
            for i in range(min(total, max_posts)):
                h = await links.nth(i).get_attribute("href")
                if h:
                    hrefs.append(urljoin(base_url, h))

        # --- 逐次チェック：最初の buyable/charge_confirmation で即終了 ---
        buy_selector = "a.buy-button"
        for idx, url in enumerate(hrefs, start=1):
            status, url, btn_text, btn_class, btn_href, elapsed_ms = await judge_on_page(
                detail_page, url, buy_selector, t0, status_csv, creator_name, idx,
                perform_purchase=perform_purchase,
                post_click_timeout_ms=post_click_timeout_ms
            )
            if status in ("buyable", "charge_confirmation"):
                await ctx.close(); await browser.close()
                return {
                    "finished": True,
                    "found": status,
                    "winner": {"url": url, "elapsed_ms": elapsed_ms, "rank": idx, "status": status},
                    "checked": idx
                }

        # 最初のN件に buyable がない
        await ctx.close(); await browser.close()
        return {"finished": True, "found": "none", "checked": len(hrefs)}

# ========= 外部公開API =========
def run_check(
    creator_name: str,
    creator_url: str,
    drop_time_str: str,   # "YYYY-mm-dd HH:MM:SS"
    max_posts: int = 5,
    base_url: str = DEFAULT_BASE_URL,
    auth_state_path: str = "auth.json",
    headless: bool = True,
    block_resources = None,
    status_csv: str = "logs/status.csv",
    auto_login: bool = False,
    login_email: Optional[str] = None,
    login_password: Optional[str] = None,
    perform_purchase: bool = False,
    post_click_timeout_ms: int = 1500,
):
    ts = to_ts(drop_time_str, "Asia/Tokyo")
    return asyncio.run(_run_once_async(
        creator_name, creator_url, ts, max_posts,
        base_url, auth_state_path, headless, block_resources, status_csv,
        auto_login, login_email, login_password,
        perform_purchase, post_click_timeout_ms
    ))
