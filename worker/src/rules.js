// 실시간 탭 기사 판정 규칙 (collector/collect.py 와 같은 규칙 — 바꾸면 양쪽을 함께 바꿔 주세요)

export const FEEDS = [  // collector/collect.py 의 FEEDS_KO (코인 전문지 제외)
  ["연합뉴스", "https://www.yna.co.kr/rss/news.xml"],
  ["연합뉴스", "https://www.yna.co.kr/rss/market.xml"],
  ["연합뉴스", "https://www.yna.co.kr/rss/economy.xml"],
  ["이데일리", "http://rss.edaily.co.kr/edaily_news.xml"],
  ["이데일리", "http://rss.edaily.co.kr/stock_news.xml"],
  ["아시아경제", "https://www.asiae.co.kr/rss/stock.htm"],
  ["뉴시스", "https://newsis.com/RSS/economy.xml"],
  ["이투데이", "https://rss.etoday.co.kr/eto/etoday_news_all.xml"],
  ["헤럴드경제", "https://biz.heraldcorp.com/rss/google/finance"],
  ["인포스탁데일리", "https://www.infostockdaily.co.kr/rss/allArticle.xml"],
  ["한국경제", "https://www.hankyung.com/feed/finance"],
  ["매일경제", "https://www.mk.co.kr/rss/50200011/"],
  ["조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/category/stock/?outputType=xml"],
  ["서울경제", "https://www.sedaily.com/rss/finance"],
];

export const KST_MS = 9 * 3600 * 1000;

// 증시 기사 판별: 주가 표현 + 주가가 오르기 시작할 때 먼저 나오는 회사 소식
export const STOCK_CONTEXT = new RegExp(
  "주가|주식|증시|특징주|상한가|하한가|급등|급락|강세|약세|%|株|上|下|코스피|코스닥|종목|공시|매수|매도|" +
  "시총|시가총액|목표가|실적|영업이익|수주|상장|유상증자|무상증자|자사주|배당|" +
  "임상|성공|선정|계약|공급|승인|허가|특허|인수|합병|MOU|협약|투자 유치|국책과제|거래 재개|" +
  "성과|부각|테마|방긋|흑자|적자|진출|입점|FDA");
// 해외 증시 기사 (국내 종목의 '美 진출' 같은 소식은 제외하지 않음)
export const FOREIGN_KO = new RegExp(
  "(베트남|중국증시|일본증시|홍콩증시|대만증시|호치민|인도증시|\\[중국|\\[베트남|\\[일본|\\[미국|\\[美|" +
  "美증시|美 증시|미국 증시|미국증시|미 증시|뉴욕증시|뉴욕 증시|뉴욕|나스닥|다우|S&P|월가|日증시|中증시|" +
  "개장전|미국 특징주|美 특징주)");

// 알맹이 판별: 이유 없이 시세만 전하는 기사는 접어서 보여줌
const SUBSTANCE = new RegExp(
  "에\\s|에[,…·]|에도|고도\\s|불구|으로\\s|로 인해|소식|기대|우려|영향|덕분|덕에|힘입|여파|탓|발표|공시|승인|허가|" +
  "계약|수주|인수|합병|매각|분할|실적|영업이익|매출|순이익|흑자|적자|어닝|출시|개발|선정|체결|협약|MOU|투자|유치|" +
  "증자|감자|자사주|배당|소각|소송|판결|무죄|규제|정책|정부|목표가|목표주가|리포트|증권가|전망|분석|왜|이유|비결|" +
  "불발|철회|취소|제재|조사|압수|협력|파트너|공급|납품|임상|FDA|신약|특허|기술|지분|매입|처분|취득|대출|담보|" +
  "거래정지|상장폐지|관리종목|상폐|실질심사|수요예측|공모|IPO|대표|회장|CEO|주주|환원|경영|노조|파업|사고|화재|리콜|" +
  "테마|수혜|관련주|AI|반도체|2차전지|바이오|원전|방산|로봇|효과|흥행|인기|돌풍|위기|논란");
const THIN = /공시 없이|기업주식정보|\d+주차|(첫째|둘째|셋째|넷째) 주|애프터마켓|프리마켓|개장 직후|장 초반|\d+만\s?원대\s*(급등|급락|상승|하락)|^\[(상한가|하한가) 종목\]/;
const THIN_SOURCES = new Set(["TopStarNews", "톱스타뉴스"]);

export function hasSubstance(title, source, stocks) {
  if (THIN_SOURCES.has(source) || THIN.test(title)) return false;
  let text = title.replace(/^\[[^\]]{1,15}\]\s*/, "");
  for (const s of stocks) text = text.split(s.name).join(" ");  // 종목명 속 단어로 판단하지 않게
  return SUBSTANCE.test(text);
}

// 상승 관련 판별 — 화면에는 'up' 기사만 올림
const MOVE_UP = "급등|폭등|상한가|강세|상승|오름|올라|오른|뛰|치솟|껑충|신고가|최고가|반등|↑|불기둥|들썩|랠리|훨훨|급반등|돌파|上|방긋|\\+\\d+(?:\\.\\d+)?%";
const MOVE_DOWN = "급락|폭락|하한가|약세|하락|내려|내린|뚝|추락|↓|신저가|빠진|빠져|밀려|후퇴|털썩|곤두박질|무너|下";
const EVENT_UP = "수주|공급계약|계약 체결|승인|허가|흑자전환|흑자 전환|어닝 서프라이즈|호실적|최대 실적|사상 최대|역대 최대|" +
  "자사주 매입|자사주 소각|소각|목표가 상향|목표주가 상향|상향|선정|특허|임상 성공|주주환원|배당 확대|기대감|" +
  "수혜|러브콜|완판|유치|협약|MOU|파트너십|공동 개발|출시|저평가|성공|성과|거래 재개|부각|순매수|국책과제|입점|진출|" +
  "청신호|통과|가속|속도|재신청|재제출|순항|탄력|호평|문턱|뚫";
const EVENT_DOWN = "불발|적자|거래정지|상장폐지|실질심사|소송|목표가 하향|목표주가 하향|하향|유상증자|가압류|우려|쇼크|부진|" +
  "철회|취소|리콜|제재|횡령|배임|압수수색|매도 폭탄|투매|손실|팔자|매도세|순매도|실패";
const MOVE_RE = new RegExp(`(${MOVE_UP})|(${MOVE_DOWN})`, "g");
const EVENT_UP_RE = new RegExp(EVENT_UP, "g");
const EVENT_DOWN_RE = new RegExp(EVENT_DOWN, "g");
const INDEX_PHRASE = /(코스피|코스닥|지수|증시|뉴욕증시|다우|나스닥|S&P|외인|기관|개인)[^…,·;!?\]]*/g;
const CONTRAST = new RegExp(`(${EVENT_UP})[’'”"]?\\s*(에도|도\\s*무색)`, "g");

export function direction(title, stocks) {
  let text = title.replace(INDEX_PHRASE, " ");  // 지수·시장 전체의 등락은 종목의 등락이 아님
  for (const s of stocks) text = text.split(s.name).join(" ");
  if (/지분율|주식 [\d만,]+주/.test(text)) return "none";  // 지분 변동 공시의 ↑↓는 주가가 아님
  text = text.replace(CONTRAST, " ");  // '호실적에도' → 상승 근거 아님
  const moves = [...text.matchAll(MOVE_RE)];
  if (moves.length) return moves[moves.length - 1][1] ? "up" : "down";
  const up = (text.match(EVENT_UP_RE) || []).length;
  const down = (text.match(EVENT_DOWN_RE) || []).length;
  return up > down ? "up" : down > up ? "down" : "none";
}

// ---------------------------------------------------------------- 종목명 찾기
const WORD = /[가-힣A-Za-z0-9]/;
// 조사 뒤에 글자가 바로 이어지면 조사가 아님 ('SK에코플랜트'의 '에')
const PARTICLE = /^((은|는|이|가|을|를|의|도|에|와|과|로|으로|만)(?![가-힣A-Za-z0-9])|株|주가|우\b)/;
const GROUP = /^\s*(그룹|관련주|계열|테마)/;

// names: {종목명: 종목코드}, block: 삭제 요청 목록
export function makeLists(names, block = []) {
  const sorted = Object.keys(names).filter(n => n.length >= 2).sort((a, b) => b.length - a.length);
  const nameRe = new RegExp(sorted.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  return { names, nameRe, block: new Set(block.map(x => String(x).toLowerCase())) };
}

export function findStocks(title, { names, nameRe, block }) {
  const out = [];
  for (const m of title.matchAll(nameRe)) {
    const before = m.index > 0 ? title[m.index - 1] : " ";
    const after = title.slice(m.index + m[0].length, m.index + m[0].length + 4);
    if (WORD.test(before) || (WORD.test(after[0] || "") && !PARTICLE.test(after)) || GROUP.test(after)) continue;
    const name = m[0], code = names[name];
    if (block.has(name.toLowerCase()) || block.has(code) || out.some(s => s.name === name)) continue;
    out.push({ name, code });
  }
  return out;
}

// ---------------------------------------------------------------- RSS
const unescapeXml = s => s.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'")
  .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n)).replace(/&amp;/g, "&");
const tag = (block, name) => {
  const m = block.match(new RegExp(`<${name}[^>]*>([\\s\\S]*?)</${name}>`));
  return m ? unescapeXml(m[1]).trim() : "";
};

function parseTime(s) {
  if (!s) return null;
  // 시간대 표기가 없으면('2026-10-10 13:54:00') 한국 시간으로 봄
  const naive = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$/.test(s.trim());
  const t = Date.parse(naive ? s.trim().replace(" ", "T") + "+09:00" : s);
  return isNaN(t) ? null : t;
}

// since(ms) 이후 기사만, 최신 기사부터 읽다가 오래된 기사가 나오면 멈춤 (계산량 절약)
export function parseRss(xml, source, since = 0) {
  const out = [];
  let old = 0;
  for (const m of xml.matchAll(/<item[\s>]([\s\S]*?)<\/item>/g)) {
    const t = parseTime(tag(m[1], "pubDate") || tag(m[1], "dc:date"));
    if (!t) continue;
    if (t < since) { if (++old >= 5) break; continue; }  // 오래된 기사가 5개 연달아 나오면 그만 읽음
    old = 0;
    const title = tag(m[1], "title").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
    const link = tag(m[1], "link") || tag(m[1], "guid");
    if (title && link) out.push({ title, link, source, t });
  }
  return out;
}

// 기사 하나를 실시간 탭 기준으로 판정. 국내 종목 기사가 아니면 null
export function judge(it, lists) {
  if (FOREIGN_KO.test(it.title) || !STOCK_CONTEXT.test(it.title) || lists.block.has(it.link.toLowerCase())) return null;
  const stocks = findStocks(it.title, lists);
  if (!stocks.length) return null;
  return { title: it.title, link: it.link, source: it.source, time: new Date(it.t).toISOString(), stocks,
           thin: !hasSubstance(it.title, it.source, stocks), dir: direction(it.title, stocks) };
}

// ---------------------------------------------------------------- 장 시간
export const kstDate = ms => new Date(ms + KST_MS).toISOString().slice(0, 10);

// 국내 정규장(넥스트레이드 프리마켓 포함 08:00~15:30)이 열려 있는지. closed: 휴장일 목록 ["2026-10-09", ...]
export function marketOpen(ms, closed) {
  const k = new Date(ms + KST_MS);
  const day = k.getUTCDay(), min = k.getUTCHours() * 60 + k.getUTCMinutes();
  if (day === 0 || day === 6 || closed.includes(kstDate(ms))) return false;
  return min >= 8 * 60 && min < 15 * 60 + 30;
}
