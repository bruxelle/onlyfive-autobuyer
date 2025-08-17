#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import argparse
import time
import json
import os
import csv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urljoin

import yaml
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# ========= Utilities =========
def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def to_ts(tstr: str, tzname: str) -> float:
    try:
        tz = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        if tzname in ("Asia/Tokyo", "JST", "Japan"):
            tz = timezone(timedelta(hours=9))
        else:
            raise
    dt = datetime.strptime(tstr, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    return dt.timestamp()

def ensure_csv(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "run_at_iso","target_name","rank",
                "post_url","status",                 # buyable / sold_out / unknown / not_found
                "button_text","button_class","button_href",
                "elapsed_ms","notes"
            ])


# ========= Bootstrap (manual login) =========
async def bootstrap_login(base_url: str, auth_state_path: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(base_url, wait_until="domcontentloaded")
        print("[*] OnlyFiveに手動でログインしてください。タブを閉じるとセッションを保存します。")
        try:
            await page.wait_for_event("close")
        except Exception:
            pass
        state = await ctx.storage_state()
        with open(auth_state_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
        await browser.close()
        print(f"[+] セッション保存: {auth_state_path}")


# ========= Core =========
async def run_target(p, cfg: dict, target: dict):
    tzname = cfg.get("timezone", "Asia/Tokyo")
    drop_ts = to_ts(cfg["drop_time"], tzname)

    headless = bool(cfg.get("headless", True))
    auth_state_path = cfg.get("auth_state_path", "auth.json")
    block_types = set(cfg.get("block_resources", []))

    # タイムアウト等
    post_click_timeout_ms  = int(cfg.get("post_click_timeout_ms", 3000))
    detail_wait_timeout_ms = int(cfg.get("detail_wait_timeout_ms", 3500))
    status_csv             = cfg.get("status_csv", "logs/status.csv")
    max_posts              = int(cfg.get("max_posts", 5))  # ← 上位N件

    ensure_csv(status_csv)

    base_url = cfg.get("base_url", "https://only-five.jp")
    start_url = target["url"]

    # 一覧→詳細リンク（/posts/...）
    post_link_selector = target.get("post_link_selector", "a[href^='/posts/']")
    # 購入ボタンそのもの（いただいた仕様に準拠）
    buy_button_selector = target.get("buy_button_selector", "a.buy-button")

    browser = await p.chromium.launch(
        headless=headless,
        args=["--disable-gpu","--disable-features=IsolateOrigins,site-per-process"],
    )
    ctx = await browser.new_context(
        storage_state=auth_state_path,
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1280, "height": 900},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"),
    )

    if block_types:
        async def route_handler(route, request):
            if request.resource_type in block_types:
                return await route.abort()
            return await route.continue_()
        await ctx.route("**/*", route_handler)

    page = await ctx.new_page()
    await page.goto(start_url, wait_until="networkidle")
    print(f"[{target['name']}] 一覧ページ: {start_url}")

    # 定刻待機（直前は細かく）
    while True:
        now = time.time()
        if now >= drop_ts:
            break
        await asyncio.sleep(0.05 if (drop_ts - now) > 0.5 else 0.001)

    # 定刻で1回リロード
    t0 = time.perf_counter()
    print(f"[{target['name']}] 定刻→リロード")
    await page.reload(wait_until="networkidle")

    # 上位N件の /posts/ リンクを抽出（href取得して直接遷移の方が速くて確実）
    links = page.locator(post_link_selector)
    total = await links.count()
    limit = min(total, max_posts)
    print(f"[{target['name']}] posts total={total}, checking top {limit}")

    hrefs = []
    for i in range(limit):
        try:
            href = await links.nth(i).get_attribute("href")
            if href:
                full = urljoin(base_url, href)
                if full not in hrefs:
                    hrefs.append(full)
        except Exception:
            continue

    # 1件ずつ開いてステータス判定（購入ボタンは押さない）
    for idx, url in enumerate(hrefs, start=1):
        detail = await ctx.new_page()
        try:
            await detail.goto(url, timeout=detail_wait_timeout_ms, wait_until="domcontentloaded")
            try:
                await detail.wait_for_load_state("networkidle", timeout=1200)
            except Exception:
                pass

            btn = detail.locator(buy_button_selector).first
            status = "not_found"
            btn_text = btn_class = btn_href = ""

            if await btn.count() > 0:
                btn_class = (await btn.get_attribute("class")) or ""
                btn_text  = ((await btn.inner_text()) or "").strip().replace("\n", "")
                btn_href  = (await btn.get_attribute("href")) or ""

                # 判定ルール
                if ("disabled" not in btn_class) and ("charge_confirmation" in btn_href) and ("購入" in btn_text):
                    status = "buyable"
                elif ("disabled" in btn_class) or ("売り切れ" in btn_text):
                    status = "sold_out"
                else:
                    status = "unknown"

            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            # CSVへ記録（見つけた順で1行）
            with open(status_csv, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    target["name"],
                    idx, url, status,
                    btn_text, btn_class, btn_href,
                    f"{elapsed_ms:.1f}", ""
                ])

            print(f"[{target['name']}] #{idx}/{limit} [{status}] {url} text='{btn_text}' class='{btn_class}' href='{btn_href}' (+{elapsed_ms:.1f} ms)")

            # ★ 最初の buyable で即終了
            if status == "buyable":
                print(f"[{target['name']}] buyable を検出したため即終了します（残りの候補はスキップ）。CSV: {status_csv}")
                await detail.close()
                # ブラウザも閉じてタスク終了
                await ctx.close()
                await browser.close()
                return

        except Exception as e:
            with open(status_csv, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    target["name"],
                    idx, url, "unknown", "", "", "",
                    f"{(time.perf_counter()-t0)*1000.0:.1f}",
                    f"error: {e}"
                ])
            print(f"[{target['name']}] #{idx}/{limit} ERROR {url}: {e}")
        finally:
            # buyableでreturnした場合を除き、通常はクローズ
            if not detail.is_closed():
                await detail.close()

    print(f"[{target['name']}] 上位{limit}件をチェックしました（buyableは見つからず）。CSV: {status_csv}")



# ========= Entrypoint =========
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.bootstrap:
        await bootstrap_login(cfg["base_url"], cfg.get("auth_state_path", "auth.json"))
        return

    async with async_playwright() as p:
        tasks = [run_target(p, cfg, t) for t in cfg["targets"]]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
