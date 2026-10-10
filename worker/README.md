# 실시간 뉴스 서버 (Cloudflare 무료 플랜)

장중(평일 08:00~15:30)에 '실시간' 탭의 새 기사를 접속한 모든 사람에게 바로 보내는 서버입니다.
없어도 사이트는 동작하며, 그때는 GitHub 자동 수집분(장중 10분·장 마감 후 30분 간격)을 씁니다.

## 동작 방식

- 서버 하나(Durable Object)가 20초마다 언론사 RSS를 **한 번만** 확인하고, 새 기사를 WebSocket으로 모두에게 보냅니다.
- 보는 사람이 없거나 장이 끝나면 확인을 멈춥니다.
- 무료 한도(하루 10만 요청)에 잡히는 것: 기사 확인(장중 약 1,200번) + 사람마다 접속할 때 2번.
  1,000명이 하루 몇 번씩 접속해도 약 2만 번이라 넉넉합니다.
- 한도를 넘거나 서버에 문제가 생기면 화면이 알아서 GitHub 수집분으로 바꿔 보여 줍니다. (한도는 매일 오전 9시 초기화)

## 처음 한 번만 하는 설정 (약 10분)

1. https://dash.cloudflare.com 무료 가입
2. **계정 ID 복사**: 대시보드 오른쪽 아래(또는 Workers & Pages 화면)의 *Account ID*
3. **API 토큰 만들기**: 오른쪽 위 프로필 → *My Profile* → *API Tokens* → *Create Token*
   → *Edit Cloudflare Workers* 템플릿 선택 → *Continue to summary* → *Create Token* → 토큰 복사
4. GitHub 저장소 → *Settings* → *Secrets and variables* → *Actions* → *New repository secret* 로 두 개 추가
   - `CLOUDFLARE_API_TOKEN` = 3번 토큰
   - `CLOUDFLARE_ACCOUNT_ID` = 2번 계정 ID
5. 저장소 *Actions* 탭 → *worker* → *Run workflow* (이후에는 `worker/` 가 바뀔 때마다 자동 배포)
6. 배포가 끝나면 Cloudflare 대시보드 *Workers & Pages* 에서 `rise-live` 주소(예: `rise-live.아이디.workers.dev`)를 확인해
   `site/index.html` 의 `const LIVE_WS = "";` 따옴표 안에 `wss://rise-live.아이디.workers.dev/ws` 형태로 넣고 저장

확인: 브라우저에서 `https://rise-live.아이디.workers.dev/live` 를 열어 오늘 기사 목록(JSON)이 나오면 성공입니다.

## 파일

- `src/index.js` — 서버 (접속 관리, 20초마다 확인, 새 기사 전달)
- `src/rules.js` — 기사 판정 규칙 (collector/collect.py 와 같은 규칙. 한쪽을 바꾸면 다른 쪽도 함께)
- `wrangler.toml` — Cloudflare 설정
