"""
iPhone Duo 香港 Apple Store 監察器（門市自取 + 網上落單）

用真 Chrome（Playwright headless）打開 Apple 官網購買頁，檢查兩樣嘢：
  1. 門市自取：你揀嘅 Apple Store 有冇貨可以自取
  2. 網上落單：「Add to Bag」掣撳唔撳到
狀態一變（例如某間門市由無貨變有貨）就發 Telegram + Windows 通知。

用法：
    python iphone_duo_monitor.py                 # 每 3 分鐘檢查一次，一直行
    python iphone_duo_monitor.py --once          # 只檢查一次（俾 Task Scheduler / 雲端用）
    python iphone_duo_monitor.py --telegram-id   # 搵你個 Telegram chat ID
    python iphone_duo_monitor.py --test-notify   # 試發通知
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# ===== 設定 =====
# 想監察邊幾款就留低邊幾行（其他可以加 # 註解咗佢）
MODELS = {
    "256gb-night-sky": ("MK2E4ZA/A", "iPhone Duo 256GB Night Sky"),
    "256gb-star-white": ("MK2D4ZA/A", "iPhone Duo 256GB Star White"),
    # "512gb-night-sky": ("MK2G4ZA/A", "iPhone Duo 512GB Night Sky"),
    # "512gb-star-white": ("MK2F4ZA/A", "iPhone Duo 512GB Star White"),
    # "1tb-night-sky": ("MK2J4ZA/A", "iPhone Duo 1TB Night Sky"),
    # "1tb-star-white": ("MK2H4ZA/A", "iPhone Duo 1TB Star White"),
    # "2tb-night-sky": ("MK2L4ZA/A", "iPhone Duo 2TB Night Sky"),
    # "2tb-star-white": ("MK2K4ZA/A", "iPhone Duo 2TB Star White"),
}
# 門市自取：以呢個地區搜尋附近門市
PICKUP_LOCATION = "Central"
# 只通知呢啲門市（名入面包含呢啲字就算）。留空 [] = 全港門市都通知
PICKUP_STORES = []  # 例如 ["Causeway Bay", "ifc mall", "Festival Walk"]
# 網上落單（送貨）有貨都通知？False = 只係網上落單可以揀門市自取先通知
ALERT_ONLINE = False

CHECK_EVERY_SECONDS = 180  # 唔好太密，避免俾 Apple 封
# Telegram：可以直接填喺度，或者用環境變數 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WINDOWS_TOAST = sys.platform == "win32"
# ================

BASE_URL = "https://www.apple.com/hk/shop/buy-iphone/iphone-duo/7.6-inch-display-"
STATE_FILE = Path(__file__).with_name("iphone_duo_state.json")
SOLD_OUT_WORDS = ("currently unavailable", "sold out", "暫時缺貨", "已售罄")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data, timeout=15)
    except Exception as e:
        log(f"Telegram 發送失敗：{e}")


def windows_toast(title, body):
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode({json.dumps(title)})) | Out-Null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode({json.dumps(body)})) | Out-Null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('iPhone Duo Monitor').Show($n)"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


def notify(title, body, url=None):
    telegram(f"{title}\n{body}" + (f"\n{url}" if url else ""))
    if WINDOWS_TOAST:
        windows_toast(title, body)


def open_model_page(page, slug):
    page.goto(BASE_URL + slug, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("button[name=add-to-cart]", timeout=30000)


def check_online(page):
    """網上落單：返回 'available' 或 'unavailable'"""
    # 揀「不折抵」同「不要 AppleCare」，Add to Bag 先會解鎖；呢啲選項係頁面載入後先慢慢出現
    page.wait_for_selector("input[name=tradeupinline][value=noTradeIn]", state="attached", timeout=30000)
    page.locator("input[name=tradeupinline][value=noTradeIn]").first.check(force=True)
    page.wait_for_function(
        "() => { const a = document.querySelectorAll('input[name=applecare-options]');"
        " return a.length >= 2 && [...a].every(e => !e.disabled); }",
        timeout=15000,
    )
    page.locator("input[name=applecare-options]").nth(1).check(force=True)
    text = page.inner_text("body").lower()
    if any(w in text for w in SOLD_OUT_WORDS):
        return "unavailable"
    try:
        page.wait_for_function("() => !document.querySelector('button[name=add-to-cart]').disabled", timeout=10000)
        return "available"
    except Exception:
        return "unavailable"


def check_pickup(page, part):
    """門市自取：返回 (None, 原因) 或者 ({門市名: 'available'/'unavailable'}, 第一個有貨嘅自取時間文字)"""
    # 喺真頁面入面發請求，用返頁面嘅 cookies，Apple 先唔會當係機械人
    result = page.evaluate(
        """async ([part, loc]) => {
            const q = new URLSearchParams({'fae': 'true', 'pl': 'true', 'mts.0': 'regular',
                                           'parts.0': part, 'location': loc});
            const r = await fetch('/hk/shop/fulfillment-messages?' + q, {headers: {'Accept': 'application/json'}});
            return {status: r.status, text: await r.text()};
        }""",
        [part, PICKUP_LOCATION],
    )
    if result["status"] != 200:
        return None, f"Apple 未開放門市查貨（HTTP {result['status']}）"
    try:
        stores = json.loads(result["text"])["body"]["content"]["pickupMessage"]["stores"]
    except (ValueError, KeyError, TypeError):
        return None, "門市資料格式唔認得"
    out, quote = {}, ""
    for s in stores:
        name = s.get("storeName", "?")
        if PICKUP_STORES and not any(k.lower() in name.lower() for k in PICKUP_STORES):
            continue
        info = (s.get("partsAvailability") or {}).get(part, {})
        ok = info.get("pickupDisplay") == "available"
        out[name] = "available" if ok else "unavailable"
        if ok and not quote:
            quote = info.get("pickupSearchQuote") or info.get("storePickupQuote") or ""
    return out, quote


def run_once(browser):
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    first_run = not state
    page = browser.new_page(locale="en-HK")
    try:
        for slug, (part, name) in MODELS.items():
            url = BASE_URL + slug
            try:
                open_model_page(page, slug)
            except Exception as e:
                log(f"{name}: 開唔到頁面（{type(e).__name__}），下次再試")
                continue

            # 1. 門市自取
            try:
                stores, info = check_pickup(page, part)
            except Exception as e:
                stores, info = None, f"檢查失敗（{type(e).__name__}）"
            if stores is None:
                log(f"{name} 門市自取：{info}")
            else:
                have = [s for s, v in stores.items() if v == "available"]
                log(f"{name} 門市自取：{len(have)}/{len(stores)} 間有貨 {have}")
                newly = [s for s in have if state.get(f"pickup|{slug}|{s}") != "available"]
                if newly:
                    notify("📍 iPhone Duo 可以網上落單、門市自取！", f"{name}\n可自取門市：{', '.join(newly)}\n{info}".strip(), url)
                for s, v in stores.items():
                    state[f"pickup|{slug}|{s}"] = v

            # 2. 網上落單
            try:
                status = check_online(page)
            except Exception as e:
                log(f"{name} 網上落單：檢查失敗（{type(e).__name__}），下次再試")
                continue
            log(f"{name} 網上落單：{status}")
            key = f"online|{slug}"
            if ALERT_ONLINE and status == "available" and (state.get(key) != "available" or first_run):
                notify("🛒 iPhone Duo 有得買！", f"{name} 而家可以網上落單", url)
            state[key] = status
    finally:
        page.close()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def show_telegram_chat_id():
    if not TELEGRAM_BOT_TOKEN:
        print("請先填 TELEGRAM_BOT_TOKEN")
        return
    with urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=15) as r:
        updates = json.load(r).get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in updates if "message" in u}
    if not chats:
        print("搵唔到。請喺 Telegram 打開你個 bot，撳 Start 或者隨便send一句，再行多次。")
    for cid, who in chats.items():
        print(f"TELEGRAM_CHAT_ID = \"{cid}\"   ({who})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只檢查一次就退出")
    ap.add_argument("--test-notify", action="store_true", help="試發通知")
    ap.add_argument("--telegram-id", action="store_true", help="顯示你嘅 Telegram chat ID")
    args = ap.parse_args()

    if args.telegram_id:
        show_telegram_chat_id()
        return
    if args.test_notify:
        notify("iPhone Duo Monitor", "通知測試成功 ✅")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            if args.once:
                run_once(browser)
                return
            log(f"開始監察 {len(MODELS)} 款，每 {CHECK_EVERY_SECONDS} 秒檢查一次（Ctrl+C 停止）")
            while True:
                run_once(browser)
                time.sleep(CHECK_EVERY_SECONDS)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
