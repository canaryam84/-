"""급등 종목 수집기.

언론사 RSS와 구글 뉴스 RSS에 실린 기사 제목·출처·링크, 한국거래소 상장법인 목록만 사용합니다. 가입/키 불필요.
시세 데이터는 직접 수집·재배포하지 않고, 기사에 보도된 상승률만 인용합니다.
앱이 직접 쓰는 문장(요약, 이유)은 없고, 기사 제목은 원문 그대로 링크합니다.

실행:
  python collector/collect.py                 # 세 시장 모두, 가장 최근 하루
  python collector/collect.py korea           # 국내만 (나머지 시장 기록은 그대로)
  python collector/collect.py --days 5        # 최근 5거래일(코인은 5일)치 기록을 함께 채움
  python collector/collect.py --refilter      # 다시 수집하지 않고 삭제 목록(blocklist.txt)을 지난 기록 전체에 반영
"""
import email.utils
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import holidays  # 공휴일 달력 (pip install -r requirements.txt)

THRESHOLD = 20.0     # 해외 주식·코인: 이 퍼센트 이상 올랐다고 보도된 종목만 표시
KO_THRESHOLD = 15.0  # 국내 주식 기준
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "site" / "archive"
BLOCKLIST = ROOT / "blocklist.txt"
CACHE = ROOT / "collector" / "cache"
UA = {"User-Agent": "Mozilla/5.0 (compatible; RiseBriefBot/1.0)"}
WEEKDAYS = "월화수목금토일"

# 언론사 RSS (한 곳이 막혀도 나머지로 계속 동작)
# (실시간 탭용 Cloudflare Worker의 worker/src/rules.js에도 같은 목록이 있음 — 바꾸면 함께 바꿔 주세요)
FEEDS_KO = [
    ("연합뉴스", "https://www.yna.co.kr/rss/news.xml"),
    ("연합뉴스", "https://www.yna.co.kr/rss/market.xml"),
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml"),
    ("이데일리", "http://rss.edaily.co.kr/edaily_news.xml"),
    ("이데일리", "http://rss.edaily.co.kr/stock_news.xml"),
    ("아시아경제", "https://www.asiae.co.kr/rss/stock.htm"),
    ("뉴시스", "https://newsis.com/RSS/economy.xml"),
    ("이투데이", "https://rss.etoday.co.kr/eto/etoday_news_all.xml"),
    ("헤럴드경제", "https://biz.heraldcorp.com/rss/google/finance"),
    ("인포스탁데일리", "https://www.infostockdaily.co.kr/rss/allArticle.xml"),
    ("한국경제", "https://www.hankyung.com/feed/finance"),
    ("매일경제", "https://www.mk.co.kr/rss/50200011/"),
    ("조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/category/stock/?outputType=xml"),
    ("서울경제", "https://www.sedaily.com/rss/finance"),
    ("토큰포스트", "https://www.tokenpost.kr/rss"),
    ("블록미디어", "https://www.blockmedia.co.kr/feed"),
]
FEEDS_EN = [
    ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("Benzinga", "https://www.benzinga.com/feed"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
]
SPAM_SOURCES = {"Calgary Roughnecks"}

# 기간성 표현이 들어간 기사는 '하루 상승'이 아니므로 제외
# ('달러'의 '달'은 기간이 아님. '1시간 새'처럼 짧은 시간 변동도 하루 등락이 아니므로 제외)
PERIOD_KO = re.compile(r"(\d+\s*(년|개월|달(?!러)|주간|주 ?새|거래일|시간 ?새|시간 ?만)|주간|올해|연초|연중|올 ?들어|상장 ?이후|"
                       r"저점 대비|고점 대비|누적|한 ?달(?!러)|일주일|이번 ?주|지난 ?주|분기|만에|열흘|보름|며칠|장외|수익률|점유율|도미넌스|김프|김치프리미엄)")
PERIOD_EN = re.compile(r"(in 20\d\d|in \d+ (sessions|days|trading days)|sessions|this week|past week|from (its |the )?IPO|IPO price|debut|this year|year to date|ytd|in (a|one|two|three|six|\d+) (week|month|year)s?|months?|weeks?|since|so far|annual|decade|all[- ]time)", re.I)


# ---------------------------------------------------------------- 수집 공통
def fetch(url, binary=False, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if binary:
                return data
            m = re.search(rb'encoding=["\']([\w-]+)["\']', data[:200])
            return data.decode(m.group(1).decode() if m else "utf-8", "replace")
        except Exception as e:  # 네트워크 오류는 재시도
            if i == retries:
                print(f"  ! 실패: {url[:90]} ({e})", file=sys.stderr)
                return None
            time.sleep(1.5)


def tag(block, name):
    m = re.search(rf"<{name}[^>]*>(?:\s*<!\[CDATA\[)?(.*?)(?:\]\]>\s*)?</{name}>", block, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_rss(text, default_source):
    items = []
    for m in re.finditer(r"<item[\s>](.*?)</item>", text or "", re.S):
        block = m.group(1)
        title = re.sub(r"<[^>]+>", "", tag(block, "title"))
        link = tag(block, "link") or tag(block, "guid")
        source = tag(block, "source") or default_source
        if not title or not link or source in SPAM_SOURCES:
            continue
        if title.endswith(" - " + source):  # 구글 뉴스는 제목 끝에 언론사명이 붙음
            title = title[: -len(source) - 3]
        pub = tag(block, "pubDate") or tag(block, "dc:date")
        ts = None
        try:
            ts = email.utils.parsedate_to_datetime(pub)
        except Exception:
            try:
                ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                pass
        if ts:  # 시간대 표기가 없으면(예: '2026-10-10 13:54:00') 한국 언론사이므로 한국 시간으로 봄
            ts = (ts.replace(tzinfo=KST) if ts.tzinfo is None else ts).astimezone(KST)
        items.append({"title": re.sub(r"\s+", " ", title).strip(), "link": link, "source": source, "_ts": ts})
    return items


_feed_cache = {}


def google_news(query, lang, start, end):
    params = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"} if lang == "ko" else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    # 날짜 범위 검색 (앞뒤 하루 여유를 두고 가져온 뒤 시각으로 다시 거름)
    q = f"{query} after:{(start - timedelta(days=1)).date()} before:{(end + timedelta(days=1)).date()}"
    return parse_rss(fetch("https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, **params})), "")


def article_pool(feeds, queries, lang, start, end):
    """언론사 RSS + 구글 뉴스 검색 결과를 합쳐 start~end 사이 기사만 중복 없이 반환."""
    items = []
    for source, url in feeds:  # 언론사 RSS는 한 번만 받아 여러 날짜에 재사용
        if url not in _feed_cache:
            _feed_cache[url] = parse_rss(fetch(url), source)
        items += _feed_cache[url]
    for q in queries:
        items += google_news(q, lang, start, end)
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W", "", it["title"])[:60]
        if key in seen or not it["_ts"] or not (start <= it["_ts"] < end):
            continue
        seen.add(key)
        out.append(it)
    return out


def word_in(word, text):
    """'캡'이 '코인마켓캡'에 걸리지 않도록 단어 경계를 확인."""
    return re.search(r"(?<![가-힣A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", text) is not None


# ---------------------------------------------------------------- 휴장일 · 기준 시각
# 공휴일은 오픈소스 holidays 패키지 기준. 거래소만 쉬는 날은 아래에 추가로 적어 둠.
# (임시공휴일이 새로 지정되면 requirements.txt의 holidays 버전을 올리거나 여기에 날짜를 추가)
KRX_EXTRA = {"05-01": "노동절", "12-31": "연말 휴장일"}
NYSE_KO = {"New Year's Day": "새해 첫날", "Martin Luther King Jr. Day": "마틴 루서 킹 데이",
           "Washington's Birthday": "대통령의 날", "Good Friday": "성금요일", "Memorial Day": "메모리얼 데이",
           "Juneteenth National Independence Day": "준틴스", "Independence Day": "독립기념일",
           "Labor Day": "노동절", "Thanksgiving Day": "추수감사절", "Christmas Day": "크리스마스"}


def kr_closed(d):
    """국내 증시 공휴일 휴장 사유 (주말은 따로 판단). 공휴일이 아니면 None."""
    return holidays.KR(years=d.year).get(d) or KRX_EXTRA.get(d.strftime("%m-%d"))


def us_closed(d):
    name = holidays.financial_holidays("NYSE", years=d.year).get(d)
    if not name:
        return None
    base = re.sub(r"\s*\(observed\)", "", name)
    return NYSE_KO.get(base, base) + (" 대체" if "observed" in name else "")


def trading_days(expected, closed, n):
    """expected(포함)부터 거꾸로 주말·휴장일을 건너뛴 거래일 n개."""
    out, d = [], expected
    while len(out) < n:
        if d.weekday() < 5 and not closed(d):
            out.append(d)
        d -= timedelta(days=1)
    return out


def next_trading_day(d, closed):
    d += timedelta(days=1)
    while d.weekday() >= 5 or closed(d):
        d += timedelta(days=1)
    return d


def closed_days(session, last, closed, market, prefix):
    """직전 거래일 다음 날부터 last까지의 휴장일(주말 포함)을 {날짜: 기록}으로 (날짜 목록에 '휴장'으로 남기기 위해)."""
    out = {}
    d = trading_days(session - timedelta(days=1), closed, 1)[0] + timedelta(days=1)
    while d <= last:
        name = closed(d) or ("주말" if d.weekday() >= 5 else None)  # 주말과 공휴일이 겹치면 공휴일 이름
        if name:
            reason = "주말이라" if name == "주말" else f"{name}{ro(name)}"
            out[d.isoformat()] = {"label": f"{prefix}{fmt_day(d)} 휴장", "closed": name, "items": [],
                                  "message": f"{reason} {market} 증시가 열리지 않았어요."}
        d += timedelta(days=1)
    return out


def last_weekday_closed_by(local_now, close_hour, close_minute=0):
    """장 마감 시각이 지난 가장 최근 평일 (휴장 여부는 따로 판단)."""
    d = local_now.date()
    if (local_now.hour, local_now.minute) < (close_hour, close_minute):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def ny_tz(dt):
    """뉴욕 시간대 (서머타임: 3월 둘째 일요일 ~ 11월 첫째 일요일). 외부 패키지 없이 계산."""
    y = dt.astimezone(timezone.utc).year
    mar = datetime(y, 3, 8 + (6 - datetime(y, 3, 8).weekday()) % 7, 7, tzinfo=timezone.utc)
    nov = datetime(y, 11, 1 + (6 - datetime(y, 11, 1).weekday()) % 7, 6, tzinfo=timezone.utc)
    return timezone(timedelta(hours=-4 if mar <= dt.astimezone(timezone.utc) < nov else -5))


def ro(word):
    """조사 '로/으로' (받침 없거나 ㄹ받침이면 '로')."""
    c = word.strip()[-1:]
    if not ("가" <= c <= "힣"):
        return "(으)로"
    return "로" if (ord(c) - 0xAC00) % 28 in (0, 8) else "으로"


def fmt_day(d):
    return f"{d.month}월 {d.day}일({WEEKDAYS[d.weekday()]})"


# ---------------------------------------------------------------- 국내 주식
def load_krx_names():
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / "krx_names.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        return json.loads(cache.read_text("utf-8"))
    raw = fetch("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13", binary=True)
    if not raw:
        return json.loads(cache.read_text("utf-8")) if cache.exists() else {}
    text = raw.decode("euc-kr", "replace")
    names = {}
    for row in re.finditer(r"<tr>(.*?)</tr>", text, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
        if len(cells) >= 3 and cells[0]:
            names[html.unescape(cells[0])] = {"market": cells[1], "code": cells[2]}
    cache.write_text(json.dumps(names, ensure_ascii=False), "utf-8")
    return names


def market_name(m):
    return "코스피" if m == "유가" else m  # 거래소 목록은 코스피를 '유가(증권시장)'로 표기


KO_PCT = re.compile(r"(상한가|(\d{1,3}(?:\.\d+)?)\s*%\s*(?:대\s*)?(?:급등|폭등|상승|뛰|올라|치솟|강세|↑|오른|껑충))")
# 해외 증시 기사 (국내 종목의 '美 진출', '미국 허가' 같은 소식은 해외 기사가 아니므로 증시·시장 표현일 때만)
FOREIGN_KO = re.compile(r"(베트남|중국증시|일본증시|홍콩증시|대만증시|호치민|인도증시|\[중국|\[베트남|\[일본|\[미국|\[美|"
                        r"美증시|美 증시|미국 증시|미국증시|미 증시|뉴욕증시|뉴욕 증시|뉴욕|나스닥|다우|S&P|월가|日증시|中증시|"
                        r"개장전|미국 특징주|美 특징주)")  # 국내에도 넥스트레이드 프리마켓이 있어 '프리마켓'은 제외하지 않음


# ---------------------------------------------------------------- 주요 지수 등락률 (마감 기사에 보도된 값)
# 화면에는 소수점 둘째 자리까지 보여줌. 가장 많이 보도된 값을 기준으로, 그와 맞는 소수점 둘째 자리 값을
# 한국어 기사 → 영어 마감 기사 순서로 찾음.
IDX_RE = re.compile(
    # 이름 뒤에 숫자·글자가 붙으면 다른 지수('코스피200')라 제외하되, 조사('코스닥도', '코스닥은')는 허용
    r"(?P<n>코스피|코스닥|다우(?:지수|존스)?|나스닥(?:지수)?|비트코인|Bitcoin|BTC)(?:(?=[도은는이가의])|(?![\d가-힣A-Za-z]))"
    r"(?:,\s*)?[^…,·;\[\]]{0,20}?"  # '코스피, 1.37% 상승'처럼 이름 바로 뒤 쉼표는 허용
    # '3.57포인트(0.44%) 오른'처럼 괄호 안의 등락률도 읽음
    r"(?P<v>\d{1,2}(?:\.\d{1,2})?)\s*%\)?\s*(?P<dae>대)?\s*"
    r"(?P<w>상승|하락|급등|급락|폭등|폭락|올라|내려|오른|내린|↑|↓|강세|약세|뛰|빠|떨어|밀려|반등|추락|후퇴|하회|상회)")
IDX_DOWN = re.compile(r"하락|급락|폭락|내려|내린|↓|약세|빠|떨어|밀려|추락|후퇴|하회")
IDX_NAME = {"코스피": "코스피", "코스닥": "코스닥", "다우": "다우", "다우지수": "다우", "다우존스": "다우",
            "나스닥": "나스닥", "나스닥지수": "나스닥", "비트코인": "BTC", "Bitcoin": "BTC", "BTC": "BTC"}

EN_IDX_NAME = (r"(?P<n>Dow Jones Industrial Average|Dow Jones|Dow|Nasdaq Composite|Nasdaq|KOSPI|KOSDAQ|Kospi|Kosdaq)"
               r"(?:\s*\(\.?DJIA?\)|\s+Index|\s+index|\s+industrials)?")
EN_IDX_VERB = (r"rises|rose|gains|gained|jumps|jumped|climbs|climbed|rallies|rallied|surges|surged|adds|added|advances|advanced|"
               r"edges up|soars|soared|falls|fell|drops|dropped|slides|slid|sinks|sank|tumbles|tumbled|loses|lost|declines|"
               r"declined|slips|slipped|plunges|plunged|edges down|closes up|closes down|closed up|closed down|ends up|ends down")
EN_PCT = r"(?P<v>\d{1,2}(?:\.\d{1,2})?)\s*(?:%|percent)"
EN_IDX_RES = [
    # 'The Dow Jones Index Closes 0.49% Higher'
    re.compile(EN_IDX_NAME + r"\s+(?:closes|closed|ends|ended|finishes|finished|settles|settled)\s+" + EN_PCT + r"\s+(?P<w>higher|lower)", re.I),
    # 'Dow Jones Industrial Average up 0.49%'
    re.compile(EN_IDX_NAME + r"\s+(?P<w>up|down)\s+" + EN_PCT, re.I),
    # 'Dow rises 0.8%', 'KOSPI falls 2.62 percent'
    re.compile(EN_IDX_NAME + r"\s+(?P<w>" + EN_IDX_VERB + r")\s+(?:about\s+|nearly\s+|more than\s+|a\s+)?" + EN_PCT, re.I),
]
EN_IDX_DOWN = re.compile(r"lower|fall|fell|drop|slid|sink|sank|tumbl|los|declin|slip|plung|down", re.I)
EN_CLOSE = re.compile(r"\bclos|\bends?\b|\bended\b|\bfinish|\bsettle", re.I)
EN_NAME = {"dow": "다우", "nasdaq": "나스닥", "kospi": "코스피", "kosdaq": "코스닥"}


def _candidate(name, value, sign_down, approx, score, it):
    v = float(value) * (-1 if sign_down else 1)
    return {"name": name, "v": v, "dec": len(value.split(".")[1]) if "." in value else 0,
            "approx": approx, "score": score, "it": it}


def index_candidates_ko(pool, wanted, limit=15.0, latest=False):
    out = []
    for it in pool:
        title = it["title"]
        if PERIOD_KO.search(title) or re.search(r"출발|개장|장중|오전|장 초반|선물|전망", title):  # 마감 값만
            continue
        for m in IDX_RE.finditer(title):
            name = IDX_NAME[m.group("n")]
            if name in wanted and float(m.group("v")) <= limit:
                # 계속 변하는 값(비트코인)은 최신 기사일수록, 나머지는 '마감' 기사일수록 높은 점수
                score = (it["_ts"].timestamp() if it["_ts"] else 0) if latest else (2 if "마감" in title else 1)
                out.append(_candidate(name, m.group("v"), IDX_DOWN.search(m.group("w")), bool(m.group("dae")), score, it))
    return out


def index_candidates_en(pool, wanted):
    out = []
    for it in pool:
        if re.search(r"futures|premarket|pre-market|open|midday|week|month|year|streak", it["title"], re.I):
            continue  # 장중·선물·기간 수치는 빼고 마감 값만
        for rx in EN_IDX_RES:
            for m in rx.finditer(it["title"]):
                name = EN_NAME[m.group("n").split()[0].lower()]
                # 영어 기사는 장중 대략치("falls 1%")가 섞여 있어 소수점 둘째 자리까지 적힌 마감 값만 씀
                if name in wanted and float(m.group("v")) <= 15 and re.fullmatch(r"\d+\.\d\d", m.group("v")):
                    out.append(_candidate(name, m.group("v"), EN_IDX_DOWN.search(m.group("w")), False,
                                          3 if EN_CLOSE.search(it["title"]) else 1, it))
    return out


def pick_indices(cands, wanted, latest=False):
    """이름마다 대표값(가장 많이 보도된 값, 비트코인은 가장 최근 값)을 정하고, 그와 맞는 소수점 둘째 자리 값을 고름."""
    out = []
    for name in wanted:
        cs = [c for c in cands if c["name"] == name]
        if not cs:
            continue
        votes = {}
        for c in cs:  # 같은 값끼리 표를 모음 (비트코인은 최신 점수)
            key = (c["v"], c["approx"])
            votes[key] = max(votes.get(key, 0), c["score"]) if latest else votes.get(key, 0) + c["score"]
        top = max(cs, key=lambda c: (votes[(c["v"], c["approx"])], c["dec"], not c["approx"]))
        if latest:  # 비트코인처럼 계속 변하는 값은 소수점 둘째 자리까지 적힌 가장 최근 값
            exact_now = [x for x in cs if x["dec"] == 2 and not x["approx"]]
            if exact_now:
                top = max(exact_now, key=lambda x: x["score"])

        def fits(c):  # 대표값과 같은 방향이고, 대표값이 대략적인 값이면 그 범위 안
            if (c["v"] < 0) != (top["v"] < 0):
                return False
            a, b = abs(c["v"]), abs(top["v"])
            if top["approx"]:                 # '3%대' → 3.00~3.99
                return int(a) == int(b)
            if top["dec"] == 0:               # '3%' → 반올림이므로 2.50~3.49 (예: 2.98%)
                return abs(a - b) < 0.5
            return abs(a - b) < 0.05 + 1e-9 if top["dec"] == 1 else a == b  # '1.2%' → 1.15~1.25

        precise = [c for c in cs if c["dec"] == 2 and not c["approx"] and fits(c)]
        best = max(precise, key=lambda c: votes[(c["v"], c["approx"])]) if precise else top
        exact = best["dec"] == 2 and not best["approx"]
        # 소수점 둘째 자리 값을 끝내 못 찾으면 보도된 정밀도 그대로 (예: +1.2%, +3%대)
        text = f"{best['v']:+.2f}%"  # 화면에는 항상 소수점 둘째 자리까지 (예: 5% → +5.00%)
        out.append({"name": name, "pct": best["v"], "text": text, "exact": exact,
                    "source": best["it"]["source"], "link": best["it"]["link"]})
    return out


def index_moves(ko_pool, wanted, en_fetch=None, limit=15.0, latest=False):
    """한국어 기사로 먼저 정하고, 소수점 둘째 자리 값이 없으면 영어 마감 기사(en_fetch)까지 찾아 다시 정함."""
    cands = index_candidates_ko(ko_pool, wanted, limit, latest)
    picked = pick_indices(cands, wanted, latest)
    if en_fetch and any(n not in {p["name"] for p in picked if p["exact"]} for n in wanted):
        cands += index_candidates_en(en_fetch(), [n for n in wanted if n in EN_NAME.values()])
        picked = pick_indices(cands, wanted, latest)
    return picked



def collect_korea(now, n=1):
    """최근 국내 거래일 n개의 결과를 {날짜: 결과}로 반환. 휴장 안내도 함께."""
    expected = last_weekday_closed_by(now, 15, 30)
    sessions = trading_days(expected, kr_closed, n)
    morning = lambda d: datetime.combine(d, datetime.min.time(), KST) + timedelta(hours=8)
    queries = ["상한가", "특징주 급등", "주가 급등 %", "급등 마감 코스닥", "상한가 직행", "주가 폭등",
               "애프터마켓 급등", "시간외 급등", "넥스트레이드 급등",
               "테마 상승세에", "장중 상한가 종목", "상한가 종목 마감", "급등주", "20%대 급등", "10%대 급등"]
    names = load_krx_names()
    sorted_names = sorted((n for n in names if len(n) >= 2), key=len, reverse=True)  # 긴 이름 우선
    name_re = re.compile("|".join(re.escape(n) for n in sorted_names))
    results = {}
    for d in sessions:  # 그날 아침 8시 ~ 다음 거래일 아침 8시 사이 보도만 그날 장으로 봄 (날짜끼리 겹치지 않게)
        window = article_pool(FEEDS_KO, queries, "ko", morning(d), morning(next_trading_day(d, kr_closed)))
        # 16:30에 한 번, 넥스트레이드 애프터마켓이 끝난 20:00 뒤에 한 번 더 정리 (애프터마켓 상승분까지 포함)
        after_close = now >= datetime.combine(d, datetime.min.time(), KST) + timedelta(hours=20)
        label = "정규장·애프터마켓 마감 후 보도 기준" if after_close else "정규장 마감 후 보도 기준"
        # 지수: 그날 장 마감(15:20) 뒤 ~ 다음 날 아침에 나온 마감 기사
        idx_from = datetime.combine(d, datetime.min.time(), KST) + timedelta(hours=15, minutes=20)
        idx_to = morning(next_trading_day(d, kr_closed))
        idx_pool = article_pool([], ["코스피 마감", "코스닥 마감", "코스피 코스닥 장 마감", "마감시황 코스피", "코스피 상승 마감",
                                     "코스피 하락 마감", "코스닥 상승 마감", "코스닥 하락 마감"], "ko", idx_from, idx_to)
        en_fetch = lambda a=idx_from, b=idx_to: article_pool(  # 소수점 둘째 자리 값이 없을 때만 영어 기사도 찾음
            [], ["KOSPI closes", "KOSPI KOSDAQ close percent", "Seoul shares close"], "en", a, b)
        results[d.isoformat()] = {"label": f"{fmt_day(d)} {label}", "threshold": KO_THRESHOLD,
                                  "indices": index_moves(idx_pool, ["코스피", "코스닥"], en_fetch),
                                  "items": extract_korea(window, names, name_re)}
    return results, closed_days(sessions[-1], now.date(), kr_closed, "국내", "")


def standalone(title, m):
    """종목명이 다른 단어의 일부가 아닌지 확인.
    앞에 글자가 붙거나('SK하닉'), 뒤에 조사가 아닌 글자가 붙으면('유나이티드헬스', '레이더') 제외."""
    before = title[m.start() - 1] if m.start() > 0 else " "
    after = title[m.end(): m.end() + 4]
    if re.match(r"[가-힣A-Za-z0-9]", before):
        return False
    # 조사 뒤에 글자가 바로 이어지면 조사가 아님 ('SK에코플랜트'의 '에')
    if re.match(r"[가-힣A-Za-z0-9]", after) and not re.match(r"((은|는|이|가|을|를|의|도|에|와|과|로|으로|만)(?![가-힣A-Za-z0-9])|株|주가|우\b)", after):
        return False
    return not re.match(r"\s*(그룹|관련주|계열|테마)", after)


def extract_korea(pool, names, name_re):
    found = {}
    for it in pool:
        title = it["title"]
        if PERIOD_KO.search(title) or FOREIGN_KO.search(title) or "하한가" in title:
            continue
        matches = [m for m in name_re.finditer(title) if standalone(title, m)]
        for i, m in enumerate(matches):
            # 이름 뒤에서 가장 가까운 상승 표현을 찾되, 다음 종목명 앞까지만 봄
            # ('에이엔피·유진테크·형지I&C 상한가'처럼 ·로 이어진 종목은 상승 표현을 공유)
            j = i
            while j + 1 < len(matches) and re.fullmatch(r"\s*[·,]\s*", title[matches[j].end(): matches[j + 1].start()]):
                j += 1
            end = matches[j].end() + 30
            if j + 1 < len(matches):
                end = min(end, matches[j + 1].start())
            window = title[matches[j].end(): end]
            pm = KO_PCT.search(window)
            # '하락 후', '전날 상한가 이어'처럼 그날 상승이 아닌 표현은 제외
            if not pm or re.search(r"하락|급락|약세|내려|뚝|전날|전일|어제|지난", window[: pm.start()]):
                continue
            pct = 30.0 if pm.group(1) == "상한가" else float(pm.group(2))
            if pct < KO_THRESHOLD or pct > 30.5:  # 국내는 하루 최대 +30%
                continue
            name = m.group(0)
            e = found.setdefault(name, {"name": name, "sub": f"{market_name(names[name]['market'])} · {names[name]['code']}",
                                        "pct": pct, "limitUp": False, "articles": []})
            e["pct"] = max(e["pct"], pct)
            e["limitUp"] = e["limitUp"] or pm.group(1) == "상한가"
            # 그 종목만 다룬 기사이거나, 'A·B·C 상한가'처럼 종목을 나열하고 바로 등락을 적은 기사는 확실한 근거로 봄
            listed = j > i or (i > 0 and re.fullmatch(r"\s*[·,]\s*", title[matches[i - 1].end(): m.start()]))
            e["articles"].append({**it, "_solo": len(matches) == 1 or bool(listed), "_after": bool(AFTER_MARKET.search(title))})
    for e in found.values():  # 애프터마켓 기사로만 확인된 종목은 '애프터마켓' 표시
        e["afterMarket"] = all(a["_after"] for a in e["articles"])
    return finalize(found)


AFTER_MARKET = re.compile(r"애프터\s?마켓|시간외|넥스트레이드|NXT")


# ---------------------------------------------------------------- 해외 주식
EN_VERB = r"(?:soar|surge|jump|skyrocket|rocket|spike|rall|climb|leap|zoom|explode|pop|shoot|fly|flie|rise|gain)\w*"
EN_RE = re.compile(
    r"(?P<subj>[A-Z][\w&.'’\- ]{1,50}?)\s*(?:\((?:NASDAQ|NYSE|NYSE ?American|AMEX|OTC|NasdaqGS|NasdaqCM)\s*[:\s]\s*(?P<tk>[A-Z.]{1,6})\))?\s*"
    r"(?:\((?P<tk2>[A-Z]{1,5})\)\s*)?(?:stock|shares|Stock|Shares)?\s*(?:is\s+|are\s+)?" + EN_VERB +
    r"\s+(?:over\s+|more than\s+|nearly\s+|almost\s+|by\s+|as much as\s+|up to\s+)?(?P<pct>\d{1,4}(?:\.\d+)?)%")
EN_STOP = {"stock", "stocks", "shares", "market", "dow", "nasdaq", "s&p", "s&p 500", "the", "why", "here", "crypto",
           "bitcoin", "penny", "penny stocks", "small caps", "small-cap", "index", "futures", "oil", "gold", "treasury"}
EN_BAD_SUBJ = re.compile(r"\b(revenue|sales|profit|earnings|income|ebitda|q[1-4]|quarter|index|market|futures|price|volume|bitcoin|ether|yields?)\b", re.I)
EN_FORECAST = re.compile(r"\b(could|may|might|can|would|will|set to|poised|targets?|forecasts?|predicts?|expected to|upside)\b", re.I)
NAME_NOISE = r"\b(stock|shares|common|ordinary|class [a-z]|inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|the|n\.?v|s\.?a|ag|se)\b"


def norm_subject(s):
    s = re.sub(r"^(why|how|what|stock market today|breaking|update|watch|premarket|midday|top)\b[:\s]*", "", s, flags=re.I)
    s = re.sub(r"\b(stock|shares|inc\.?|corp\.?|co\.?|ltd\.?|plc|holdings|group|the)\b", "", s, flags=re.I)
    return re.sub(r"\s{2,}", " ", s).strip(" -’'.,")


def norm_company(s):
    s = re.sub(r"[.,’'()]", " ", s.split(" - ")[0].lower())
    s = re.sub(NAME_NOISE, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_us_listings():
    """미국 상장 종목 목록 (나스닥이 공개하는 파일, 가입 불필요). {정리된 회사명: 티커}, {티커}"""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / "us_listings.json"
    if not (cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400):
        rows = []
        for url in ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"):
            for line in (fetch(url) or "").splitlines()[1:]:
                parts = line.split("|")
                if len(parts) > 3 and not line.startswith("File Creation"):
                    rows.append([parts[0], parts[1]])
        if rows:
            cache.write_text(json.dumps(rows), "utf-8")
    rows = json.loads(cache.read_text("utf-8")) if cache.exists() else []
    by_name = {}
    for sym, name in rows:
        if re.search(r"warrant|right|unit|preferred|depositary shares? representing|notes? due", name, re.I):
            continue
        by_name.setdefault(norm_company(name), sym)
    return by_name, {sym for sym, _ in rows}


def match_us(subj, ticker, listings):
    """기사 속 회사명이 미국 상장 종목인지 확인하고 티커를 돌려줌. 아니면 None."""
    by_name, symbols = listings
    if ticker and ticker in symbols:
        return ticker
    if re.fullmatch(r"[A-Z]{1,5}", subj) and subj in symbols:
        return subj
    key = norm_company(subj)
    if len(key) < 3:
        return None
    if key in by_name:
        return by_name[key]
    hits = [sym for name, sym in by_name.items() if name.startswith(key + " ")]
    return hits[0] if len(hits) == 1 else None


KO_NAME_PCT = re.compile(r"(?P<name>[가-힣A-Za-z][가-힣A-Za-z0-9&.]{1,20})\s*(?:\((?P<tk>[A-Z]{1,5})\))?\s*(?:주가\s*|주\s*)?"
                         r"(?P<pct>\d{1,4}(?:\.\d+)?)\s*%\s*(?:대\s*)?(?:급등|폭등|상승|↑|뛰|치솟|껑충|올라)")
KO_STOP = re.compile(r"^(다우|나스닥|S&P|지수|유가|브렌트유|국제유가|금리|국채|코스피|코스닥|증시|선물|달러|환율|매출|영업이익|순이익|실적|거래대금|거래량)")


def collect_us(now, n=1):
    tz = ny_tz(now)
    expected = last_weekday_closed_by(now.astimezone(tz), 16)
    sessions = trading_days(expected, us_closed, n)
    ny_4am = lambda d: datetime.combine(d, datetime.min.time(), tz).astimezone(KST) + timedelta(hours=4)
    queries = ["stock soars %", "shares surge %", "stock skyrockets", "stock jumps %", "shares soar %",
               "biggest stock movers today", "shares jump after", "stock soars after", "stock surges after",
               "shares skyrocket", "stocks making the biggest moves", "shares double"]
    ko_queries = ["美증시 특징주", "미국 특징주 급등", "뉴욕증시 특징주 폭등", "미국 주식 폭등 %"]
    listings = load_us_listings()
    results = {}
    for d in sessions:  # 그날 뉴욕 새벽 4시(프리마켓) ~ 다음 거래일 새벽 4시 보도만 그날 장으로 봄 (겹치지 않게)
        start, end = ny_4am(d), ny_4am(next_trading_day(d, us_closed))
        # 지수: 미국 장 마감(16:00) 뒤 ~ 다음 개장 전까지 나온 '뉴욕증시 마감' 기사
        close = datetime.combine(d, datetime.min.time(), tz).astimezone(KST) + timedelta(hours=16)
        idx_pool = article_pool([], ["뉴욕증시 마감 다우 나스닥", "뉴욕증시 다우 나스닥", "미국 증시 마감 나스닥", "뉴욕증시 다우존스", "다우지수 마감"], "ko",
                                close, close + timedelta(hours=17, minutes=30))
        en_fetch = lambda c=close: article_pool(  # 소수점 둘째 자리 값이 없을 때만 영어 마감 기사도 찾음
            [], ["Dow Nasdaq close stocks", "Dow Jones closes", "stock market today Dow Nasdaq",
                 "Dow Jones Index closes", "Dow Jones Industrial Average close of trade", "Nasdaq Composite closes"],
            "en", c - timedelta(minutes=30), c + timedelta(hours=10))
        indices = index_moves(idx_pool, ["다우", "나스닥"], en_fetch)
        missing = [n for n in ("다우", "나스닥") if n not in {i["name"] for i in indices}]
        if missing:
            for name in missing:
                # 숫자가 끝내 없으면 한국 기사의 방향 표현을 그대로 씀 (예: '소폭 상승')
                pat = ("다우(?:지수|존스)?" if name == "다우" else "나스닥(?:지수)?") + r"[은는도이가]?\s*(소폭|큰 폭)?\s*(상승|하락|올라|내려|강세|약세)"
                for it in idx_pool:
                    m = re.search(pat, it["title"])
                    if m:
                        down = bool(IDX_DOWN.search(m.group(2)))
                        indices.append({"name": name, "pct": -0.0001 if down else 0.0001,
                                        "text": f"{m.group(1) + ' ' if m.group(1) else ''}{'하락' if down else '상승'}",
                                        "source": it["source"], "link": it["link"]})
                        break
            indices.sort(key=lambda i: ("다우", "나스닥").index(i["name"]))
        results[d.isoformat()] = {"label": f"미국 {fmt_day(d)} 정규장 마감 후 보도 기준", "threshold": THRESHOLD,
                                  "indices": indices,
                                  "items": extract_us(article_pool(FEEDS_EN, queries, "en", start, end),
                                                      article_pool([], ko_queries, "ko", start, end), listings)}
    return results, closed_days(sessions[-1], now.astimezone(tz).date(), us_closed, "미국", "미국 ")


def extract_us(en_pool, ko_pool, listings):
    found = {}
    for it in en_pool:
        title = it["title"]
        if PERIOD_EN.search(title):  # 기간 누적 상승 기사 제외
            continue
        for seg in re.split(r"\s[-–|]\s|:\s|;\s|,\s(?=[A-Z])", title):
            m = EN_RE.search(seg)
            if not m or EN_FORECAST.search(seg[: m.end()]):  # '~할 수 있다' 같은 전망 제외
                continue
            pct = float(m.group("pct"))
            subj = norm_subject(m.group("subj"))
            if pct < THRESHOLD or not subj or subj.lower() in EN_STOP or len(subj) > 40 or EN_BAD_SUBJ.search(subj):
                continue
            ticker = match_us(subj, m.group("tk") or m.group("tk2"), listings)
            if not ticker:  # 미국 상장 종목 목록에 없으면(다른 나라 주식 등) 제외
                continue
            e = found.setdefault(ticker, {"name": subj, "sub": ticker, "pct": pct, "articles": []})
            e["pct"] = max(e["pct"], pct)
            e["articles"].append({**it, "_solo": True})

    # 한국어 해외증시 기사(예: '[美증시 특징주] 파시라 44% 폭등')
    for it in ko_pool:
        title = it["title"]
        if PERIOD_KO.search(title) or re.search(r"日증시|中증시|베트남|홍콩|코스피|코스닥|국내", title):
            continue
        if not re.search(r"美|미국|뉴욕|나스닥|월가|NYSE", title):  # 미국 증시 기사임이 분명한 것만
            continue
        for m in KO_NAME_PCT.finditer(title):
            name, pct = m.group("name"), float(m.group("pct"))
            if (pct < THRESHOLD or KO_STOP.search(name) or re.match(r"^(비트코인|이더리움)", name)
                    or re.search(r"(에|에서|으로|소식|충족|기대|전망|발표|하루|주가)$", name)):
                continue
            ticker = m.group("tk") if m.group("tk") in listings[1] else None
            if ticker and ticker in found:  # 티커가 같을 때만 영어 기사와 같은 종목으로 합침
                found[ticker]["sub"] = f"{found[ticker]['name']} · {ticker}"
                found[ticker]["name"] = name
                found[ticker]["articles"].append({**it, "_solo": True})
                continue
            # 상승률이 비슷한 영어 기사 종목이 있으면 같은 종목일 수 있어 중복 표시하지 않음
            if not ticker and any(abs(e["pct"] - pct) <= 1 for e in found.values() if e["sub"] != "미국 주식"):
                continue
            e = found.setdefault(ticker or name, {"name": name, "sub": ticker or "미국 주식", "pct": pct, "articles": []})
            e["articles"].append({**it, "_solo": True})
    return finalize(found)


# ---------------------------------------------------------------- 코인
CRYPTO_STOP = re.compile(r"코인데스크|지수|(만에|이후|이상|최대|에|에서|으로|소식)$|^(SPAC|ETF|주간|주식|코인|가상자산|가상화폐|암호화폐|알트코인|알트|밈코인|시장|시총|"
                         r"거래량|거래대금|김치프리미엄|업비트|빗썸|코인원|코빗|주가|국내|해외|최근|하루|일주일|전날|전일|대비|약|한때|장중)$")


def collect_crypto(now, n=1):
    """매일 12:00 기준, 직전 24시간 보도. (12시 전에 실행하면 어제 12시 기준)"""
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if now < noon:
        noon -= timedelta(days=1)
    results = {}
    for k in range(n):
        end = noon - timedelta(days=k)
        label = f"{fmt_day(end.date())} 12:00 기준 최근 24시간 보도"
        # BTC: 그날 12시까지 24시간 동안 보도된 비트코인 등락률 중 가장 최근 값
        idx_pool = article_pool([], ["비트코인 시세", "코인 가격 비트코인", "비트코인 %", "비트코인 상승", "비트코인 하락",
                                     "비트코인 시황", "가상자산 시세 비트코인", "비트코인 24시간 전보다"], "ko", end - timedelta(hours=24), end)
        results[end.date().isoformat()] = {"label": label, "threshold": THRESHOLD,
                                           "indices": index_moves(idx_pool, ["BTC"], limit=30.0, latest=True),
                                           "items": extract_crypto(end - timedelta(hours=24), end)}
    return results, None


def extract_crypto(start, end):
    queries = ["코인 급등 %", "가상자산 급등", "암호화폐 폭등 %", "알트코인 급등", "코인 시세 급등", "밈코인 폭등"]
    found = {}
    for it in article_pool(FEEDS_KO, queries, "ko", start, end):
        title = it["title"]
        if PERIOD_KO.search(title) or not re.search(r"코인|가상자산|가상화폐|암호화폐|토큰|업비트|빗썸|비트|이더|리플|솔라나|도지|크립토", title):
            continue
        for m in KO_NAME_PCT.finditer(title):
            name, pct = m.group("name"), float(m.group("pct"))
            if pct < THRESHOLD or pct > 1000 or CRYPTO_STOP.search(name) or KO_STOP.search(name):
                continue
            e = found.setdefault(name, {"name": name, "sub": m.group("tk") or "가상자산", "pct": pct, "articles": []})
            e["pct"] = max(e["pct"], pct)
            e["articles"].append({**it, "_solo": True})
    return finalize(found)


# ---------------------------------------------------------------- 정리 · 삭제 목록
def load_blocklist():
    if not BLOCKLIST.exists():
        return set()
    lines = (l.split("#")[0].strip() for l in BLOCKLIST.read_text("utf-8").splitlines())
    return {l.lower() for l in lines if l}


def blocked(e, blocklist):
    return any(x and x.lower() in blocklist for x in [e["name"]] + e.get("sub", "").split(" · "))


def finalize(found):
    out = []
    for e in found.values():
        arts, titles = [], set()
        for a in sorted(e.pop("articles"), key=lambda a: -(a["_ts"].timestamp() if a["_ts"] else 0)):
            if a["title"] not in titles:
                titles.add(a["title"])
                arts.append(a)
        # 오보 방지: 기사 2건 이상에서 확인되거나, 그 종목 하나만 다룬 기사가 있을 때만 표시
        if len(arts) < 2 and not any(a["_solo"] for a in arts):
            continue
        arts.sort(key=lambda a: not a["_solo"])  # 그 종목만 다룬 기사를 먼저
        e["newsCount"] = len(arts)
        e["articles"] = [{"title": a["title"], "link": a["link"], "source": a["source"],
                          "time": a["_ts"].isoformat() if a["_ts"] else None} for a in arts[:4]]
        out.append(e)
    out.sort(key=lambda e: (-e["pct"], -e["newsCount"]))
    return out


def apply_blocklist(items, blocklist):
    out = []
    for e in items:
        if blocked(e, blocklist):
            continue
        e["articles"] = [a for a in e["articles"] if a["link"].lower() not in blocklist]
        if e["articles"]:
            out.append(e)
    return out


# ---------------------------------------------------------------- 날짜별 기록 저장
# site/archive/index.json          : 시장별 날짜 목록 + 휴장 안내
# site/archive/<시장>/<날짜>.json   : 그날의 종목과 관련 기사
def read_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


# ---------------------------------------------------------------- 실시간 국내 종목 뉴스
# site/live.json : 최근 48시간 동안 국내 상장 종목 이름이 들어간 증시 기사 (최신순)
LIVE = ROOT / "site" / "live.json"
LIVE_HOURS = 48
LIVE_MAX = 400
# 증시 기사 판별: 주가 표현뿐 아니라, 주가가 오르기 시작할 때 먼저 나오는 회사 소식(임상 성공, 선정, 계약 등)도 포함
STOCK_CONTEXT = re.compile(r"주가|주식|증시|특징주|상한가|하한가|급등|급락|강세|약세|%|株|上|下|코스피|코스닥|종목|공시|매수|매도|"
                           r"시총|시가총액|목표가|실적|영업이익|수주|상장|유상증자|무상증자|자사주|배당|"
                           r"임상|성공|선정|계약|공급|승인|허가|특허|인수|합병|MOU|협약|투자 유치|국책과제|거래 재개|"
                           r"성과|부각|테마|방긋|흑자|적자|진출|입점|FDA")


# 알맹이 판별: 이유·사건이 담긴 제목은 통과, 등락·가격만 있는 시세 기사는 접어서 보여줌
# (worker/src/rules.js 에도 같은 규칙이 있음 — 바꾸면 함께 바꿔 주세요)
SUBSTANCE = re.compile(
    r"에\s|에[,…·]|에도|고도\s|불구|으로\s|로 인해|소식|기대|우려|영향|덕분|덕에|힘입|여파|탓|발표|공시|승인|허가|"
    r"계약|수주|인수|합병|매각|분할|실적|영업이익|매출|순이익|흑자|적자|어닝|출시|개발|선정|체결|협약|MOU|투자|유치|"
    r"증자|감자|자사주|배당|소각|소송|판결|무죄|규제|정책|정부|목표가|목표주가|리포트|증권가|전망|분석|왜|이유|비결|"
    r"불발|철회|취소|제재|조사|압수|협력|파트너|공급|납품|임상|FDA|신약|특허|기술|지분|매입|처분|취득|대출|담보|"
    r"거래정지|상장폐지|관리종목|상폐|실질심사|수요예측|공모|IPO|대표|회장|CEO|주주|환원|경영|노조|파업|사고|화재|리콜|"
    r"테마|수혜|관련주|AI|반도체|2차전지|바이오|원전|방산|로봇|효과|흥행|인기|돌풍|위기|논란")
# 이유 없이 시세만 전하는 틀에 박힌 형식
THIN = re.compile(r"공시 없이|기업주식정보|\d+주차|(첫째|둘째|셋째|넷째) 주|애프터마켓|프리마켓|개장 직후|장 초반|"
                  r"\d+만\s?원대\s*(급등|급락|상승|하락)|^\[(상한가|하한가) 종목\]")
THIN_SOURCES = {"TopStarNews", "톱스타뉴스"}  # 종목만 바꿔 같은 형식의 시세 기사를 반복해서 내는 곳


def has_substance(title, source="", stocks=()):
    """제목에 이유·사건이 담겨 있으면 True, 등락·가격만 있는 시세 기사면 False."""
    if source in THIN_SOURCES or THIN.search(title):
        return False
    text = re.sub(r"^\[[^\]]{1,15}\]\s*", "", title)
    for s in stocks:  # 종목명 속 단어('HLB바이오스텝'의 '바이오')로 판단하지 않게 종목명은 빼고 봄
        text = text.replace(s["name"] if isinstance(s, dict) else s, " ")
    return bool(SUBSTANCE.search(text))


# 상승 관련 판별: 실시간 탭에는 제목 내용이 주가 상승과 관련된 기사만 올림
# (worker/src/rules.js 에도 같은 규칙이 있음 — 바꾸면 함께 바꿔 주세요)
MOVE_UP = r"급등|폭등|상한가|강세|상승|오름|올라|오른|뛰|치솟|껑충|신고가|최고가|반등|↑|불기둥|들썩|랠리|훨훨|급반등|돌파|上|방긋|\+\d+(?:\.\d+)?%"
MOVE_DOWN = r"급락|폭락|하한가|약세|하락|내려|내린|뚝|추락|↓|신저가|빠진|빠져|밀려|후퇴|털썩|곤두박질|무너|下"
EVENT_UP = (r"수주|공급계약|계약 체결|승인|허가|흑자전환|흑자 전환|어닝 서프라이즈|호실적|최대 실적|사상 최대|역대 최대|"
            r"자사주 매입|자사주 소각|소각|목표가 상향|목표주가 상향|상향|선정|특허|임상 성공|주주환원|배당 확대|기대감|"
            r"수혜|러브콜|완판|유치|협약|MOU|파트너십|공동 개발|출시|저평가|성공|성과|거래 재개|부각|순매수|국책과제|입점|진출|"
            r"청신호|통과|가속|속도|재신청|재제출|순항|탄력|호평|문턱|뚫")
EVENT_DOWN = (r"불발|적자|거래정지|상장폐지|실질심사|소송|목표가 하향|목표주가 하향|하향|유상증자|가압류|우려|쇼크|부진|"
              r"철회|취소|리콜|제재|횡령|배임|압수수색|매도 폭탄|투매|손실|팔자|매도세|순매도|실패")
MOVE_RE = re.compile(f"(?P<up>{MOVE_UP})|(?P<down>{MOVE_DOWN})")
# 지수·시장 전체의 등락('코스피 2.6% 급락…')은 종목의 등락이 아니므로 그 구절은 빼고 봄
INDEX_PHRASE = re.compile(r"(코스피|코스닥|지수|증시|뉴욕증시|다우|나스닥|S&P|외인|기관|개인)[^…,·;!?\]]*")


def direction(title, stocks=()):
    """제목 내용이 주가 상승 쪽이면 'up', 하락 쪽이면 'down', 알 수 없으면 'none'."""
    text = INDEX_PHRASE.sub(" ", title)
    for s in stocks:
        text = text.replace(s["name"] if isinstance(s, dict) else s, " ")
    if re.search(r"지분율|주식 [\d만,]+주", text):  # 임원·대주주 지분 변동 공시의 ↑↓는 주가가 아님
        return "none"
    # '호실적에도', '100조 호실적도 무색'처럼 좋은 소식에도 오르지 못했다는 표현은 상승 근거로 보지 않음
    text = re.sub(rf"({EVENT_UP})[’'”\"]?\s*(에도|도\s*무색)", " ", text)
    moves = list(MOVE_RE.finditer(text))
    if moves:  # 등락 표현이 있으면 마지막 표현 기준 ('하락 출발 후 반등' → 상승)
        return "up" if moves[-1].group("up") else "down"
    up, down = len(re.findall(EVENT_UP, text)), len(re.findall(EVENT_DOWN, text))
    return "up" if up > down else "down" if down > up else "none"


def find_stocks(title, names, name_re):
    """제목에 들어간 국내 상장 종목 (단어 중간 매칭·그룹/테마 표현 제외)."""
    out = []
    for m in name_re.finditer(title):
        if not standalone(title, m):
            continue
        name = m.group(0)
        if name not in (s["name"] for s in out):
            out.append({"name": name, "code": names[name]["code"]})
    return out


def filter_live(items, blocklist):
    """삭제 목록 반영 + 같은 제목 중복 제거 (최신순)."""
    out, seen = [], set()
    for it in sorted(items, key=lambda x: x["time"], reverse=True):
        key = re.sub(r"\W", "", it["title"])[:60]
        if key in seen or it["link"].lower() in blocklist:
            continue
        it["stocks"] = [s for s in it["stocks"] if s["name"].lower() not in blocklist and s["code"].lower() not in blocklist]
        if it["stocks"]:
            seen.add(key)
            out.append(it)
    return out


def run_live(now):
    """오늘(한국 시간 0시~) 기사는 site/live.json에, 자정이 지난 날짜는 archive/live/<날짜>.json에 저장."""
    names = load_krx_names()
    name_re = re.compile("|".join(re.escape(n) for n in sorted((n for n in names if len(n) >= 2), key=len, reverse=True)))
    start = now - timedelta(hours=LIVE_HOURS)
    queries = ["특징주", "주가 급등", "주가 급락", "상한가", "공시 주가", "목표가 상향"]
    blocklist = load_blocklist()
    day_of = lambda it: datetime.fromisoformat(it["time"]).astimezone(KST).date().isoformat()

    # 이전에 모은 기사 (RSS는 최신 몇십 건만 보여줘서 이어 붙임)
    items = {it["link"]: it for it in read_json(LIVE, {}).get("items", [])}
    for k in range(1, LIVE_HOURS // 24 + 2):
        for it in read_json(ARCHIVE / "live" / f"{(now.date() - timedelta(days=k)).isoformat()}.json", {}).get("items", []):
            items.setdefault(it["link"], it)
    for it in article_pool(FEEDS_KO, queries, "ko", start, now + timedelta(minutes=10)):
        title = it["title"]
        if FOREIGN_KO.search(title) or not STOCK_CONTEXT.search(title) or it["link"] in items:
            continue
        stocks = find_stocks(title, names, name_re)
        if stocks:
            items[it["link"]] = {"title": title, "link": it["link"], "source": it["source"],
                                 "time": it["_ts"].isoformat(), "stocks": stocks}

    by_day = {}
    for it in filter_live(items.values(), blocklist):
        it["thin"] = not has_substance(it["title"], it["source"], it["stocks"])  # 시세만 전하는 기사는 접어서 표시
        it["dir"] = direction(it["title"], it["stocks"])  # 화면에는 상승 관련(up) 기사만 올림
        by_day.setdefault(day_of(it), []).append(it)
    today = now.date().isoformat()
    write_json(LIVE, {"date": today, "updatedAt": now.isoformat(), "items": by_day.get(today, [])[:LIVE_MAX]})
    print(f"실시간 {today}: {len(by_day.get(today, []))}건 ({LIVE.relative_to(ROOT)})")
    for day, day_items in by_day.items():  # 자정이 지난 날짜는 날짜별 기록으로 (바뀐 게 있을 때만 저장)
        if day >= today:
            continue
        path = ARCHIVE / "live" / f"{day}.json"
        old = read_json(path, {})
        sig = lambda i: (i["link"], i.get("thin"), i.get("dir"))
        if {sig(i) for i in old.get("items", [])} != {sig(i) for i in day_items}:
            write_json(path, {"label": f"{fmt_day(datetime.fromisoformat(day).date())} 국내 종목 뉴스",
                              "updatedAt": now.isoformat(), "items": day_items[:LIVE_MAX]})
            print(f"  live {day}: {len(day_items)}건 기록")


def write_stocks():
    """실시간 탭의 Cloudflare Worker가 쓰는 국내 종목 이름 목록 (site/stocks.json)."""
    names = load_krx_names()
    if names:
        (ROOT / "site" / "stocks.json").write_text(
            json.dumps({n: v["code"] for n, v in names.items()}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    # 삭제 요청 목록도 Worker가 읽을 수 있게 공개 (실시간 탭에서도 바로 숨김)
    write_json(ROOT / "site" / "blocklist.json", sorted(load_blocklist()))
    # 국내 증시 휴장일(평일 공휴일·거래소 휴장일): 실시간 서버와 화면이 장 시간을 판단할 때 씀
    today = datetime.now(KST).date()
    days = (today + timedelta(days=i) for i in range(-7, 400))
    write_json(ROOT / "site" / "holidays.json", [d.isoformat() for d in days if d.weekday() < 5 and kr_closed(d)])


def rebuild_index():
    index = read_json(ARCHIVE / "index.json", {})
    index["threshold"] = THRESHOLD
    index["markets"] = {}
    for key in ("korea", "us", "crypto", "live"):
        files = sorted((ARCHIVE / key).glob("*.json"), key=lambda p: p.stem, reverse=True)
        closed = {p.stem: d["closed"] for p in files if (d := read_json(p, {})).get("closed")}
        index["markets"][key] = {"dates": [p.stem for p in files], "closed": closed}
    write_json(ARCHIVE / "index.json", index)
    write_stocks()


def main():
    args = sys.argv[1:]
    if "--live" in args:
        run_live(datetime.now(KST))
        rebuild_index()
        return
    collectors = {"korea": collect_korea, "us": collect_us, "crypto": collect_crypto}
    days = int(args[args.index("--days") + 1]) if "--days" in args else 1  # 과거 며칠치를 함께 채울지
    blocklist = load_blocklist()

    if "--refilter" not in args:
        targets = [a for a in args if a in collectors] or list(collectors)
        now = datetime.now(KST)
        print(f"수집 시작 {now:%Y-%m-%d %H:%M} KST: {', '.join(targets)} (최근 {days}일)")
        for key in targets:
            try:
                results, closed = collectors[key](now, days)
            except Exception as e:  # 한 시장이 실패하면 그 시장은 기존 기록을 유지
                print(f"  ! {key} 실패: {e}", file=sys.stderr)
                continue
            for day, result in (closed or {}).items():  # 휴장일도 날짜 목록에 '휴장'으로 남김
                write_json(ARCHIVE / key / f"{day}.json", result)
                print(f"  {key} {day}: 휴장 ({result['closed']})")
            for day, result in results.items():
                path = ARCHIVE / key / f"{day}.json"
                old = read_json(path, None)
                if not result["items"] and old and old.get("items"):
                    continue  # 수집이 비정상적으로 비었을 때 기존 기록을 지우지 않음
                write_json(path, {**result, "items": apply_blocklist(result["items"], blocklist),
                                  "updatedAt": now.isoformat()})
                print(f"  {key} {day}: {len(result['items'])}개")
    else:  # 삭제 요청 반영: 지난 기록 전체에서 숨김
        for path in ARCHIVE.glob("*/*.json"):
            data = read_json(path, None)
            if data and "items" in data:
                live_like = path.parent.name == "live"
                data["items"] = (filter_live if live_like else apply_blocklist)(data["items"], blocklist)
                write_json(path, data)
        live = read_json(LIVE, None)
        if live:
            live["items"] = filter_live(live["items"], blocklist)
            write_json(LIVE, live)

    rebuild_index()
    print(f"저장: {ARCHIVE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()