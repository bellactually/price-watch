"""Price watch: checks one Shopify product and sends a phone alert via ntfy when
the price, sale status or stock changes. Runs on a timer in GitHub Actions."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PRODUCT_URL = "https://www.aesop.co.th/en/products/resurrection-rinse-free-hand-mist"
FEED_URL = os.environ.get("FEED_URL", PRODUCT_URL + ".js")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TEST_ALERT = os.environ.get("TEST_ALERT", "").lower() == "true"
CURRENCY = "THB"
RETRY_WAITS = [10, 30, 60]   # seconds to wait between attempts within one run
FAIL_ALERT_AFTER = 3         # consecutive failed runs before the phone is told
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def money(cents):
    return f"{cents / 100:,.2f} {CURRENCY}"


def notify(title, message, priority=3):
    print(f"ALERT [{priority}] {title}: {message}")
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
    urllib.request.urlopen(req, timeout=20).read()


def fetch_once():
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


def fetch():
    """Read the product, retrying a few times if the site turns us away."""
    for attempt, wait in enumerate(RETRY_WAITS + [None], start=1):
        try:
            return fetch_once()
        except Exception as exc:
            print(f"Attempt {attempt} failed: {exc}")
            if wait is None:
                raise
            if isinstance(exc, urllib.error.HTTPError):
                retry_after = exc.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = min(max(wait, int(retry_after)), 90)
            time.sleep(wait * float(os.environ.get("WAIT_SCALE", "1")))


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


def on_sale(s):
    return bool(s.get("compare_at_price")) and s["compare_at_price"] > s["price"]


def main():
    old = load_state()
    try:
        now = fetch()
    except Exception as exc:  # site blocked us, changed its feed, or is down
        print(f"Fetch failed: {exc}")
        if old is None:
            return 1
        old["fail_count"] = old.get("fail_count", 0) + 1
        if old["fail_count"] >= FAIL_ALERT_AFTER and not old.get("failing"):
            notify("Price watch can't read the page",
                   f"The last {old['fail_count']} checks failed ({exc}). "
                   "It will keep trying and tell you when it recovers.", 3)
            old["failing"] = True
        save_state(old)
        return 0

    now["failing"] = False
    now["fail_count"] = 0
    stock = "in stock" if now["available"] else "sold out"
    summary = f"{now['title']}: {money(now['price'])}, {stock}"
    print(summary)

    if TEST_ALERT:
        notify("Price watch test", f"Alerts are working. {summary}", 3)

    if old is None:
        notify("Price watch started", f"Now watching {summary}", 2)
    else:
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
        if now["available"] and not old["available"]:
            notify("Back in stock", summary, 4)
        elif old["available"] and not now["available"]:
            notify("Sold out", summary, 3)

    if now != old:
        save_state(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
