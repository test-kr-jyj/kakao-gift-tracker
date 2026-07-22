# -*- coding: utf-8 -*-
"""
카카오톡 선물하기 랭킹 자동 수집기 (v2)
- 지정 카테고리/하위탭의 상위 N위(순위/브랜드/상품명/위시수/가격)를 시간별로 기록
- 화면 글자가 아니라, 페이지가 불러오는 JSON(products 배열)을 가로채 저장 → 안정적
- 스크롤하며 여러 페이지를 이어붙여 500위까지 수집
- 결과: data/ranking.sqlite(누적) + data/latest_*.csv(최신 스냅샷)

사용:
  python collector.py           # 실제 수집 (스케줄러가 매시간 실행)
  python collector.py --debug   # 가로챈 JSON 원본을 debug/ 에 저장 (구조 확인용)
"""

import sys, os, re, csv, json, sqlite3, datetime, urllib.parse
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# 추적할 카테고리
#   url    : 랭킹 카테고리 페이지 (건강 = category/8)
#   subtab : 카테고리 안의 하위 탭 텍스트. "전체"면 기본, 다른 값이면 그 탭을 클릭.
# ---------------------------------------------------------------------------
CATEGORIES = [
    {"name": "건강_전체",        "url": "https://gift.kakao.com/ranking/category/8", "subtab": "전체"},
    {"name": "이너뷰티_다이어트", "url": "https://gift.kakao.com/ranking/category/8", "subtab": "다이어트·이너뷰티"},
]

TOP_N         = 500
SCROLL_ROUNDS = 45     # 20개씩 페이징 → 500위 확보용
SCROLL_WAIT   = 800    # ms
HEADLESS      = True

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
DB_PATH   = os.path.join(DATA_DIR, "ranking.sqlite")

# 실제 카카오 JSON 구조에 맞춘 필드 경로 (점 표기 = 중첩)
F_ID    = ["productId", "id"]
F_NAME  = ["name", "displayName"]
F_BRAND = ["brand.name", "brandName"]
F_WISH  = ["wish.wishCount", "wishCount"]
F_PRICE = ["price.sellingPrice", "price.basicPrice", "sellingPrice"]


def now_kst():
    KST = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(datetime.timezone.utc).astimezone(KST)


def get_path(d, dotted):
    cur = d
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def first_of(d, paths):
    for p in paths:
        v = get_path(d, p)
        if v not in (None, ""):
            return v
    return None


def to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.sub(r"[^\d]", "", str(v))
    return int(m) if m else None


def find_product_lists(obj, out):
    """JSON 어디에 있든 key=='products' 인 (dict 리스트)를 모두 수집"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "products" and isinstance(v, list) and v and isinstance(v[0], dict):
                out.append(v)
            find_product_lists(v, out)
    elif isinstance(obj, list):
        for x in obj:
            find_product_lists(x, out)


def url_path(u):
    try:
        return urllib.parse.urlparse(u).path
    except Exception:
        return u


def build_rows(captured):
    """가로챈 (url, json) 목록 → 엔드포인트별로 products 이어붙이고, 가장 큰 목록 선택"""
    groups = {}   # path -> {"ids": set, "items": [ ]}
    for u, data in captured:
        lists = []
        find_product_lists(data, lists)
        if not lists:
            continue
        key = url_path(u)
        g = groups.setdefault(key, {"ids": set(), "items": []})
        for plist in lists:
            for it in plist:
                pid = first_of(it, F_ID) or it.get("name")
                if pid in g["ids"]:
                    continue
                g["ids"].add(pid)
                g["items"].append(it)

    if not groups:
        return []
    # 가장 많은 상품을 모은 엔드포인트 = 랭킹
    best = max(groups.values(), key=lambda g: len(g["items"]))["items"]

    rows = []
    for idx, it in enumerate(best[:TOP_N], start=1):
        name = first_of(it, F_NAME)
        if not name:
            continue
        rows.append({
            "rank":  idx,
            "brand": first_of(it, F_BRAND) or "",
            "name":  str(name),
            "wish":  to_int(first_of(it, F_WISH)),
            "price": to_int(first_of(it, F_PRICE)),
        })
    return rows


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS ranking(
        collected_at TEXT, category TEXT, rank INTEGER,
        brand TEXT, name TEXT, wish INTEGER, price INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_time ON ranking(collected_at, category)")
    con.commit()
    return con


def save_rows(con, category, rows, stamp):
    con.executemany("INSERT INTO ranking VALUES (?,?,?,?,?,?,?)",
        [(stamp, category, r["rank"], r["brand"], r["name"], r["wish"], r["price"]) for r in rows])
    con.commit()
    path = os.path.join(DATA_DIR, f"latest_{category}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["수집시각", "순위", "브랜드", "상품명", "위시수", "가격"])
        for r in rows:
            w.writerow([stamp, r["rank"], r["brand"], r["name"], r["wish"], r["price"]])


def click_subtab(page, subtab):
    if not subtab or subtab == "전체":
        return
    for sel in (subtab, subtab.replace("·", "")):
        try:
            el = page.get_by_text(sel, exact=False).first
            el.click(timeout=4000)
            page.wait_for_timeout(2500)
            return
        except Exception:
            continue


def collect_category(page, cat, debug=False):
    captured = []

    def on_response(resp):
        try:
            if "json" not in resp.headers.get("content-type", "").lower():
                return
            data = resp.json()
        except Exception:
            return
        captured.append((resp.url, data))
        if debug:
            fn = re.sub(r"[^a-zA-Z0-9]+", "_", resp.url)[-70:]
            try:
                with open(os.path.join(DEBUG_DIR, f"{cat['name']}__{fn}.json"), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    page.on("response", on_response)
    page.goto(cat["url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    click_subtab(page, cat.get("subtab"))
    for _ in range(SCROLL_ROUNDS):
        page.mouse.wheel(0, 24000)
        page.wait_for_timeout(SCROLL_WAIT)
    page.remove_listener("response", on_response)
    return build_rows(captured)


def main():
    debug = "--debug" in sys.argv
    os.makedirs(DEBUG_DIR, exist_ok=True)
    con = init_db()
    stamp = now_kst().strftime("%Y-%m-%d %H:%M")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            locale="ko-KR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        for cat in CATEGORIES:
            try:
                rows = collect_category(page, cat, debug=debug)
                if rows:
                    save_rows(con, cat["name"], rows, stamp)
                    print(f"[{stamp}] {cat['name']}: {len(rows)}개 저장")
                else:
                    print(f"[{stamp}] {cat['name']}: 상품목록 못 찾음")
            except Exception as e:
                print(f"[{stamp}] {cat['name']}: 오류 - {e}")
        browser.close()
    con.close()


if __name__ == "__main__":
    main()
