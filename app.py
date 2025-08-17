# app.py
import sys, asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
import threading
import queue
import time
from datetime import datetime, timedelta
import pandas as pd
import os
import yaml

from autobuy import bootstrap_login, run_check

# --- 設定読込（dry_run や timeout をUI既定に反映） ---
def load_cfg(path="config.yaml"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}

CFG = load_cfg()
dry_run_cfg = bool(CFG.get("dry_run", True))
post_click_timeout_ms_cfg = int(CFG.get("post_click_timeout_ms", 1500))
status_csv_cfg = CFG.get("status_csv", "logs/status.csv")

# ========== グローバル（スレッド→UI渡し） ==========
GLOBAL_UI_QUEUE = queue.Queue()
def put_msg(msg): GLOBAL_UI_QUEUE.put(msg)
def drain_msgs():
    msgs = []
    while True:
        try:
            msgs.append(GLOBAL_UI_QUEUE.get_nowait())
        except queue.Empty:
            break
    return msgs

def precise_wait_to(target_dt: datetime):
    target_dt = target_dt.replace(second=0, microsecond=0)
    while True:
        now = datetime.now()
        delta = (target_dt - now).total_seconds()
        if delta <= 1.5: break
        time.sleep(min(0.25, max(0.0, delta - 1.5)))
    while True:
        now = datetime.now()
        delta = (target_dt - now).total_seconds()
        if delta <= 0.2: break
        time.sleep(0.05)
    deadline = time.perf_counter() + max(0.0, (target_dt - datetime.now()).total_seconds())
    while True:
        if time.perf_counter() >= deadline: break
        time.sleep(0.0005)

# ========== UI ==========
st.set_page_config(page_title="OnlyFive AutoBuyer", layout="centered")
st.title("OnlyFive AutoBuyer")

if "is_running" not in st.session_state:
    st.session_state["is_running"] = False
if "last_result" not in st.session_state:
    st.session_state["last_result"] = None

with st.expander("初回セットアップ（推奨）", expanded=False):
    st.markdown("""
- **事前ログイン**を済ませた状態で本番を迎えるのが最速です。  
  「🔑 ブラウザで手動ログイン（セッション保存）」で OnlyFive にログインして、タブを閉じると `auth.json` が保存されます。
- 必要なら「自動ログイン（事前）」で `auth.json` を作成しておいてください。
    """)

colA, colB = st.columns(2)
with colA:
    if st.button("🔑 ブラウザで手動ログイン（セッション保存）"):
        try:
            bootstrap_login()
            st.success("auth.json を保存しました。")
        except Exception as e:
            st.error(f"失敗: {e}")
with colB:
    st.caption("本番の「指定時刻に実行」時は `auth.json` を前提にログインロスをゼロにします。")

st.subheader("ターゲット設定")
creator_name = st.text_input("ターゲット名", value="CreatorA")
creator_url  = st.text_input("クリエイター一覧URL", value=CFG.get("creator_url","https://only-five.jp/creators/3544"))

today = datetime.now()
date_val = st.date_input("実行日", value=today.date())
time_val = st.time_input("実行時刻（JST）", value=(today + timedelta(minutes=1)).time(), step=60).replace(second=0, microsecond=0)

max_posts   = st.number_input("上位何件をチェックするか", min_value=1, max_value=10, value=int(CFG.get("max_posts",5)), step=1)
headless    = st.checkbox("ヘッドレス（非表示）で実行", value=bool(CFG.get("headless", True)))
block_assets= st.checkbox("画像/フォント/スタイルをブロックして高速化", value=bool(CFG.get("block_resources",["image","font","stylesheet"])!=[]))
status_csv  = status_csv_cfg
post_click_timeout_ms = post_click_timeout_ms_cfg

# ========== 実行関数 ==========
def run_now(auto_login: bool):
    dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    perform_purchase = (not dry_run_cfg)  # config.yaml の dry_run を尊重
    return run_check(
        creator_name=creator_name,
        creator_url=creator_url,
        drop_time_str=dt,
        max_posts=max_posts,
        headless=headless,
        block_resources=(["image","font","stylesheet"] if block_assets else []),
        status_csv=status_csv,
        auto_login=auto_login,  # 今すぐ実行は保険で可
        login_email=None, login_password=None,
        perform_purchase=perform_purchase,
        post_click_timeout_ms=post_click_timeout_ms,
    )

def run_at_in_thread(target_dt: datetime):
    try:
        target_dt = target_dt.replace(second=0, microsecond=0)
        put_msg(f"指定時刻まで待機中… {target_dt.strftime('%Y-%m-%d %H:%M:%S')} に実行します")
        precise_wait_to(target_dt)

        perform_purchase = (not dry_run_cfg)
        res = run_check(
            creator_name=creator_name,
            creator_url=creator_url,
            drop_time_str=target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            max_posts=max_posts,
            headless=headless,
            block_resources=(["image","font","stylesheet"] if block_assets else []),
            status_csv=status_csv,
            auto_login=False,  # 本番は事前ログイン前提
            login_email=None, login_password=None,
            perform_purchase=perform_purchase,
            post_click_timeout_ms=post_click_timeout_ms,
        )
        put_msg(("result", res))
        put_msg("完了しました。")
    except Exception as e:
        put_msg(f"エラー: {e}")
    finally:
        put_msg(("running", False))

def run_at(target_dt: datetime):
    st.session_state["is_running"] = True
    th = threading.Thread(target=run_at_in_thread, args=(target_dt,), daemon=True)
    th.start()

# ========== ボタン ==========
col1, col2 = st.columns(2)
with col1:
    if st.button("▶ 今すぐ実行（テスト・事前確認）"):
        try:
            res = run_now(auto_login=True)
            st.success("実行しました。下の結果・CSVを確認してください。")
            st.session_state["last_result"] = res
        except Exception as e:
            st.error(f"実行エラー: {e}")

with col2:
    if st.button("⏱ 指定時刻に実行（本番・:00）"):
        target_dt = datetime.combine(date_val, time_val).replace(second=0, microsecond=0)
        run_at(target_dt)
        st.info(f"予約しました: {target_dt.strftime('%Y-%m-%d %H:%M:00')}（:00 で実行）")

# ========== メッセージ反映 & 表示 ==========
for m in drain_msgs():
    if isinstance(m, tuple):
        tag, payload = m
        if tag == "result":
            st.session_state["last_result"] = payload
        elif tag == "running":
            st.session_state["is_running"] = bool(payload)
    else:
        st.write(m)

if st.session_state.get("is_running"):
    st.info("⏱ 実行中…（CSVは 1 秒ごとに自動更新）")

if st.session_state.get("last_result") is not None:
    st.subheader("直近の結果（メモリ）")
    st.json(st.session_state["last_result"])

st.subheader("CSVログ")
if os.path.exists(status_csv):
    try:
        import pandas as pd
        df = pd.read_csv(status_csv)
        st.dataframe(df.tail(50), use_container_width=True)
    except Exception as e:
        st.error(f"CSV読み込みエラー: {e}")
else:
    st.caption("まだCSVはありません。実行すると生成されます。")

if st.session_state.get("is_running"):
    time.sleep(1)
    st.rerun()
