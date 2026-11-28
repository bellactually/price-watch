"""Price watch: checks one Shopify product and sends a phone alert via ntfy when
the price, sale status or stock changes. Runs in GitHub Actions.

LOOP_MINUTES > 0: stay running and check every few minutes for that long
(used by the scheduled runs, so checks don't depend on GitHub's slow timer).
LOOP_MINUTES = 0: check once (used by manual test runs)."""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

PRODUCT_URL = "https://www.aesop.co.th/en/products/resurrection-rinse-free-hand-mist"
FEED_URL = os.environ.get("FEED_URL", PRODUCT_URL + ".js")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TEST_ALERT = os.environ.get("TEST_ALERT", "").lower() == "true"
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0"))
CHECK_EVERY = float(os.environ.get("CHECK_EVERY", "180"))      # seconds between checks
BACKOFF_EVERY = float(os.environ.get("BACKOFF_EVERY", "600"))  # seconds to wait after a refused check
SAVE_TO_GIT = os.environ.get("GITHUB_ACTIONS") == "true"
CURRENCY = "THB"
FAIL_ALERT_AFTER = 4      # consecutive failed checks (~40 min) before the phone is told
RESTOCK_ALERTS = 3        # back-in-stock alert is sent this many times, one per check
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def money(cents):
    return f"{cents / 100:,.2f} {CURRENCY}"


def notify(title, message, priority=3):
    print(f"ALERT [{priority}] {title}: {message}", flush=True)
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, alert not sent")
        return
    body = json.dumps({
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "click": PRODUCT_URL,
    }).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as exc:  # one retry, then give up for this alert
        print(f"ntfy failed ({exc}), retrying")
        time.sleep(5)
        urllib.request.urlopen(req, timeout=20).read()


def fetch():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        product = json.loads(resp.read().decode("utf-8"))
    variants = product["variants"]
    cheapest = min(variants, key=lambda v: v["price"])
    return {
        "title": product["title"],
        "price": cheapest["price"],
        "compare_at_price": cheapest.get("compare_at_price"),
        "available": any(v["available"] for v in variants),
    }


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    if SAVE_TO_GIT:
        try:
            subprocess.run(["git", "add", STATE_FILE], check=True)
            if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
                subprocess.run(["git", "commit", "-q", "-m", "Update last seen price"], check=True)
                subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], check=True)
                subprocess.run(["git", "push", "-q"], check=True)
        except subprocess.CalledProcessError as exc:
            print(f"Saving to GitHub failed ({exc}); will retry on the next change")


def on_sale(s):
    return bool(s.get("compare_at_price")) and s["compare_at_price"] > s["price"]


def check(old):
    """One check. Returns (new_state, ok)."""
    try:
        now = fetch()
    except Exception as exc:  # site refused us, changed its feed, or is down
        print(f"Check failed: {exc}", flush=True)
        if old is None:
            return None, False
        old = dict(old)
        old["fail_count"] = old.get("fail_count", 0) + 1
        if old["fail_count"] >= FAIL_ALERT_AFTER and not old.get("failing"):
            notify("Price watch can't read the page",
                   f"The last {old['fail_count']} checks failed ({exc}). "
                   "It will keep trying and tell you when it recovers.", 3)
            old["failing"] = True
        return old, False

    stock = "in stock" if now["available"] else "sold out"
    summary = f"{now['title']}: {money(now['price'])}, {stock}"
    print(summary, flush=True)
    now["failing"] = False
    now["fail_count"] = 0
    now["restock_alerts"] = old.get("restock_alerts", 0) if old else 0

    if old is None:
        notify("Price watch started", f"Now watching {summary}", 2)
        return now, True

    if old.get("failing"):
        notify("Price watch recovered", summary, 2)
    if now["price"] < old["price"]:
        notify("PRICE DROP",
               f"{now['title']} is now {money(now['price'])} (was {money(old['price'])}), {stock}.", 5)
    elif now["price"] > old["price"]:
        notify("Price went up",
               f"{now['title']} is now {money(now['price'])} (was {money(old['price'])}).", 2)
    if on_sale(now) and not on_sale(old):
        notify("ON SALE",
               f"{now['title']}: {money(now['price'])}, marked down from "
               f"{money(now['compare_at_price'])}, {stock}.", 5)

    if now["available"]:
        if not old["available"]:
            now["restock_alerts"] = 0
        if not old["available"] or 0 < now["restock_alerts"] < RESTOCK_ALERTS:
            now["restock_alerts"] += 1
            reminder = "" if now["restock_alerts"] == 1 else f" (reminder {now['restock_alerts']} of {RESTOCK_ALERTS})"
            notify("BACK IN STOCK",
                   f"{now['title']} is back in stock at {money(now['price'])}. Tap to open the page.{reminder}", 5)
    else:
        now["restock_alerts"] = 0
        if old["available"]:
            notify("Sold out", summary, 3)
    return now, True


def main():
    state = load_state()
    deadline = time.time() + LOOP_MINUTES * 60
    first = True
    while True:
        new, ok = check(state)
        if first and TEST_ALERT and ok:
            stock = "in stock" if new["available"] else "sold out"
            notify("Price watch test", f"Alerts are working. {new['title']}: {money(new['price'])}, {stock}", 3)
        first = False
        if new is None:
            return 1
        if new != state:
            save_state(new)
            state = new
        wait = CHECK_EVERY * random.uniform(1, 1.15) if ok else BACKOFF_EVERY
        if time.time() + wait > deadline:
            return 0
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
