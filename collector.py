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
    {"name": "건강_전체",        "slug": "health",     "url": "https://gift.kakao.com/ranking/category/8", "subtab": "전체"},
    {"name": "이너뷰티_다이어트", "slug": "innerbeauty", "url": "https://gift.kakao.com/ranking/category/8", "subtab": "다이어트·이너뷰티"},
]

# 순위와 상관없이 항상 그래프/변동 추적에 포함할 브랜드(부분일치). 경쟁사도 여기 추가 가능.
TRACK_BRANDS  = ["타이거모닝"]

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


def write_latest(slug, rows, stamp):
    """대시보드 현재 순위표용 — 전체 순위(최대 500)를 최신 스냅샷으로 저장"""
    path = os.path.join(DATA_DIR, f"latest_{slug}.json")
    obj = {"t": stamp, "items": [
        {"r": r["rank"], "b": r["brand"], "n": r["name"], "w": r["wish"]}
        for r in rows[:TOP_N]
    ]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _keep_items(rows, keep_top):
    """상위 keep_top + 순위 밖이라도 추적 브랜드(TRACK_BRANDS) 상품은 항상 포함"""
    items, seen = [], set()
    for r in rows[:keep_top]:
        items.append({"r": r["rank"], "b": r["brand"], "n": r["name"], "w": r["wish"]})
        seen.add(r["name"])
    for r in rows[keep_top:]:
        if r["name"] in seen:
            continue
        if any(b in (r["brand"] or "") for b in TRACK_BRANDS):
            items.append({"r": r["rank"], "b": r["brand"], "n": r["name"], "w": r["wish"]})
            seen.add(r["name"])
    return items


def update_history(slug, rows, stamp, keep_top=100, keep_snaps=720):
    """대시보드용 시간대별 히스토리 JSON 누적 (data/history_<slug>.json)"""
    path = os.path.join(DATA_DIR, f"history_{slug}.json")
    hist = []
    if os.path.exists(path):
        try:
            hist = json.load(open(path, encoding="utf-8"))
        except Exception:
            hist = []
    snap = {"t": stamp, "items": _keep_items(rows, keep_top)}
    hourkey = stamp[:13]                       # "YYYY-MM-DD HH" (시간 단위로 묶음)
    if hist and str(hist[-1].get("t", ""))[:13] == hourkey:
        hist[-1] = snap                        # 같은 시간대면 최신값으로 교체(그래프는 시간당 1점)
    else:
        hist.append(snap)
    hist = hist[-keep_snaps:]                  # 오래된 스냅샷 정리(약 30일)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


def update_daily(slug, rows, stamp, keep_top=100, keep_days=366):
    """대시보드 장기(최대 1년)용 — 하루 1개 요약(그날 마지막 실행값으로 갱신)"""
    path = os.path.join(DATA_DIR, f"daily_{slug}.json")
    day = stamp[:10]                            # YYYY-MM-DD
    hist = []
    if os.path.exists(path):
        try:
            hist = json.load(open(path, encoding="utf-8"))
        except Exception:
            hist = []
    snap = {"t": day, "items": _keep_items(rows, keep_top)}
    if hist and hist[-1].get("t") == day:       # 오늘자면 최신값으로 교체
        hist[-1] = snap
    else:
        hist.append(snap)
    hist = hist[-keep_days:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


def click_subtab(page, subtab):
    """가운뎃점(·/・/ㆍ) 문자 차이에 흔들리지 않게, 단어 사이를 '아무 글자 1개'로 매칭"""
    if not subtab or subtab == "전체":
        return False
    parts = [p for p in re.split(r"[^\w가-힣]+", subtab) if p]
    pattern = re.compile(".?".join(map(re.escape, parts)))
    try:
        page.get_by_text(pattern).first.click(timeout=6000)
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def collect_category(page, cat, debug=False):
    captured = []
    phase = {"v": 0}   # 0 = 하위탭 클릭 이전, 1 = 클릭 이후(해당 탭 데이터)

    def on_response(resp):
        try:
            if "json" not in resp.headers.get("content-type", "").lower():
                return
            data = resp.json()
        except Exception:
            return
        captured.append((resp.url, data, phase["v"]))
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

    sub = cat.get("subtab")
    needs_click = bool(sub) and sub != "전체"
    if needs_click:
        phase["v"] = 1          # 지금부터 오는 응답 = 선택한 하위탭 데이터
        click_subtab(page, sub)

    for _ in range(SCROLL_ROUNDS):
        page.mouse.wheel(0, 24000)
        page.wait_for_timeout(SCROLL_WAIT)
    page.remove_listener("response", on_response)

    if debug:
        _dump_probe(page, cat, captured)

    # 하위탭 카테고리는 클릭 이후(phase 1) 응답만 사용해 '전체' 데이터 오염 방지
    min_phase = 1 if needs_click else 0
    use = [(u, d) for (u, d, ph) in captured if ph >= min_phase]
    return build_rows(use)


def _dump_probe(page, cat, captured):
    """디버그: 잡은 요청 주소 목록 + 하위탭 버튼 구조 + 현재 화면 상품명을 저장"""
    # 1) 모든 요청 주소(전체 URL, phase 태그 포함)
    try:
        lines = [f"[phase{ph}] {u}" for (u, d, ph) in captured]
        with open(os.path.join(DEBUG_DIR, f"_manifest_{cat['name']}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    # 2) 하위탭 버튼 구조 + 현재 상품명
    try:
        info = page.evaluate("""() => {
            const labels = ['전체','홍삼','건강식품','영양제','다이어트','이너뷰티','즙','환'];
            const subtabs = [];
            const seen = new Set();
            for (const e of Array.from(document.querySelectorAll('a,button,span,li,div'))) {
                const t = (e.textContent||'').trim();
                if (t && t.length <= 14 && labels.some(l => t.includes(l))) {
                    const key = t + '|' + e.tagName;
                    if (seen.has(key)) continue; seen.add(key);
                    subtabs.push({text:t, tag:e.tagName, cls:(e.className||'').toString().slice(0,120),
                                  href:e.getAttribute('href')||'', html:e.outerHTML.slice(0,400)});
                }
            }
            const prods = [];
            for (const im of Array.from(document.querySelectorAll('img')).slice(0,60)) {
                const a = im.getAttribute('alt')||''; if (a && a.length>6) prods.push(a);
            }
            return {subtabs, firstProducts: prods.slice(0,12)};
        }""")
        with open(os.path.join(DEBUG_DIR, f"_subtabs_{cat['name']}.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        with open(os.path.join(DEBUG_DIR, f"_subtabs_{cat['name']}.json"), "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f, ensure_ascii=False)


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
                    write_latest(cat["slug"], rows, stamp)
                    update_history(cat["slug"], rows, stamp)
                    update_daily(cat["slug"], rows, stamp)
                    print(f"[{stamp}] {cat['name']}: {len(rows)}개 저장")
                else:
                    print(f"[{stamp}] {cat['name']}: 상품목록 못 찾음")
            except Exception as e:
                print(f"[{stamp}] {cat['name']}: 오류 - {e}")
        browser.close()
    con.close()


if __name__ == "__main__":
    main()
