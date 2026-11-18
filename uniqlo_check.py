"""Uniqlo VN sale watch: alerts via ntfy when the AIRism Ultra Stretch UV full-zip
jacket (E483281-000) is on sale in White (00) or Light Gray (02) at or under the
price ceiling. Uniqlo moves sale colours to separate price-group pages (/01, /02...),
so every group is checked. Runs on a timer in GitHub Actions."""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

PRODUCT = "E483281-000"
NAME = "AIRism UV Ultra Stretch Full-Zip Jacket"
WANTED = {"00": "White", "02": "Light Gray"}
CEILING = 391000  # VND
GROUPS = ["00", "01", "02", "03", "04", "05", "06"]
API = ("https://www.uniqlo.com/vn/api/commerce/v5/vi/products/{p}/price-groups/{g}/l2s"
       "?withPrices=true&withStocks=true&includePreviousPrice=false"
       "&withMemberPricing=true&httpFailure=true")
PAGE = "https://www.uniqlo.com/vn/vi/products/{p}/{g}"
CLIENT_IDS = ["uq.vn.web-spa", "uq.vn.web", ""]
STATE_FILE = os.environ.get("UNIQLO_STATE_FILE", "uniqlo_state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TEST_ALERT = os.environ.get("TEST_ALERT", "").lower() == "true"
FAIL_ALERT_AFTER = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def vnd(v):
    return f"{int(v):,}".replace(",", ".") + " VND"


def notify(title, message, click, priority=3):
    print(f"ALERT [{priority}] {title}: {message}")
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, alert not sent")
        return
    body = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": message,
                       "priority": priority, "click": click}).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def get_json(url, client_id):
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Referer": PAGE.format(p=PRODUCT, g="00")}
    if client_id:
        headers["x-fr-clientid"] = client_id
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_group(g):
    """Return list of rows for one price group, None if the group doesn't exist."""
    url = API.format(p=PRODUCT, g=g)
    last_err = None
    for cid in CLIENT_IDS:
        try:
            data = get_json(url, cid)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_err = f"HTTP {exc.code} (client id {cid or 'none'})"
            continue
        except Exception as exc:
            last_err = f"{exc} (client id {cid or 'none'})"
            continue
        res = data.get("result")
        if not res or "l2s" not in res:
            last_err = f"no result: {str(data)[:150]} (client id {cid or 'none'})"
            continue
        rows = []
        for l2 in res["l2s"]:
            lid = l2["l2Id"]
            price = res.get("prices", {}).get(lid) or {}
            stock = res.get("stocks", {}).get(lid) or {}
            base = (price.get("base") or {}).get("value")
            promo = (price.get("promo") or {}).get("value")
            rows.append({"color": l2["color"]["displayCode"],
                         "size": l2["size"]["displayCode"],
                         "base": base, "promo": promo,
                         "price": promo if promo else base,
                         "stock": stock.get("statusCode", "")})
        return rows
    raise RuntimeError(f"group {g}: {last_err}")


def scan():
    groups = {}
    for g in GROUPS:
        rows = read_group(g)
        if rows is not None and rows:
            groups[g] = rows
    if "00" not in groups and "01" not in groups:
        raise RuntimeError("neither /00 nor /01 returned data")
    return groups


def matches(groups):
    hits = []
    for g, rows in groups.items():
        for r in rows:
            if r["color"] not in WANTED or r["price"] is None:
                continue
            discounted = g != "00" or (r["promo"] and r["base"] and r["promo"] < r["base"])
            if discounted and r["price"] <= CEILING:
                hits.append(dict(r, group=g))
    return hits


def sale_colours(groups):
    out = {}
    for g, rows in groups.items():
        if g == "00":
            continue
        for r in rows:
            out.setdefault(r["color"], set()).add(r["price"])
    return ", ".join(f"{c} {'/'.join(vnd(p) for p in sorted(ps) if p)}" for c, ps in sorted(out.items())) or "none"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    state = load_state()
    page01 = PAGE.format(p=PRODUCT, g="01")
    try:
        groups = scan()
    except Exception as exc:
        print(f"Scan failed: {exc}")
        state["fail_count"] = state.get("fail_count", 0) + 1
        if TEST_ALERT:
            notify("Uniqlo watch test: FAILED", f"Couldn't read Uniqlo: {exc}"[:240], page01, 4)
        elif state["fail_count"] >= FAIL_ALERT_AFTER and not state.get("failing"):
            notify("Uniqlo watch can't read the page",
                   f"The last {state['fail_count']} checks failed ({exc})"[:240]
                   + ". It keeps trying and will tell you when it recovers.", page01, 3)
            state["failing"] = True
        save_state(state)
        return 0

    if state.get("failing"):
        notify("Uniqlo watch recovered", "Checks are working again.", page01, 2)
    state["failing"] = False
    state["fail_count"] = 0

    for g, rows in sorted(groups.items()):
        colours = sorted({r["color"] for r in rows})
        print(f"group {g}: colours {colours}; " + "; ".join(
            f"{r['color']}/{r['size']} {r['price']} {r['stock']}" for r in rows))

    hits = matches(groups)
    summary_sale = sale_colours(groups)
    print(f"sale colours: {summary_sale}; wanted hits: {len(hits)}")

    if hits:
        best = min(hits, key=lambda r: r["price"])
        in_stock = sorted({f"{WANTED[r['color']]} {r['size']}" for r in hits
                           if r["stock"] and r["stock"] != "STOCK_OUT"})
        signature = sorted(f"{r['group']}-{r['color']}-{r['size']}-{r['price']}-{r['stock']}" for r in hits)
        link = PAGE.format(p=PRODUCT, g=best["group"]) + f"?colorDisplayCode={best['color']}"
        if signature != state.get("last_hit") or TEST_ALERT:
            stock_txt = ("in stock: " + ", ".join(in_stock)) if in_stock else "all sizes sold out"
            notify(f"Uniqlo SALE: {WANTED[best['color']]} {vnd(best['price'])}",
                   f"{NAME} is {vnd(best['price'])} ({stock_txt}). "
                   "Run it past your budget check first.", link, 5)
        state["last_hit"] = signature
    else:
        state["last_hit"] = []

    today = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    if TEST_ALERT and not hits:
        notify("Uniqlo watch test",
               f"Alerts are working. White/Grey not at or under {vnd(CEILING)}. "
               f"Sale colours now: {summary_sale}.", page01, 3)
    elif not hits and today.weekday() == 0 and state.get("last_health") != today.date().isoformat():
        notify("Uniqlo watch OK",
               f"White/Grey not at or under {vnd(CEILING)} yet. Sale colours: {summary_sale}.",
               page01, 2)
        state["last_health"] = today.date().isoformat()

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
