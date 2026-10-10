// 앱처럼 설치했을 때 쓰는 오프라인 대비 (서비스 워커)
// 항상 최신 자료를 먼저 받고, 인터넷이 끊기면 마지막으로 받아 둔 화면·자료를 보여줌
const CACHE = "rise-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/apple-touch-icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;  // 기사 링크·실시간 서버는 그대로
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(url.pathname, copy));  // 같은 파일은 주소 뒤 ?t=… 와 상관없이 하나로 저장
        }
        return res;
      })
      .catch(() => caches.match(url.pathname).then(r => r || caches.match("./")))
  );
});
