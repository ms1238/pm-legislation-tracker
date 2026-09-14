# -*- coding: utf-8 -*-
"""자치법규(지방) 입법예고 API가 있는지, 있다면 어떤 주소인지 알아본다.

개발 컨테이너에서는 lawmaking.go.kr 이 막혀 있어 러너에서만 돌릴 수 있다.
한 번 답을 얻으면 지울 파일이다 — lawmaking_probe.py 가 그랬던 것처럼.

읽기만 한다. 아무것도 저장하지 않는다.
"""
import os
import re
import sys
import time
import urllib.parse
import urllib.request

GUIDE = "https://opinion.lawmaking.go.kr/api/apiGuideInfo"
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}


def oc():
    return os.environ.get("LAWMAKING_OC", "").strip()


def redact(t):
    return t.replace(oc(), "***OC***") if (oc() and t) else t


def log(msg):
    print(msg, flush=True)


def get(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        log("  실패 %s — %r" % (redact(url), e))
        return None


def strip_tags(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def read_guide():
    """안내 페이지가 어떤 서비스를 열어 두었는지 본다."""
    log("=" * 70)
    log("1) API 안내 페이지에서 서비스 목록을 찾는다")
    log("=" * 70)
    html = get(GUIDE)
    if not html:
        log("안내 페이지를 못 읽었다.")
        return

    # /rest/... 로 시작하는 주소가 본문이든 링크든 다 모은다.
    paths = sorted(set(re.findall(r"/rest/([A-Za-z0-9_]+)", html)))
    log("발견된 /rest/ 엔드포인트 %d개:" % len(paths))
    for p in paths:
        log("   /rest/%s" % p)

    text = strip_tags(html)
    log("")
    log("'자치법규'가 나오는 자리:")
    hits = 0
    for m in re.finditer("자치법규", text):
        log("   …%s…" % text[max(0, m.start() - 90):m.start() + 90].strip())
        hits += 1
        if hits >= 6:
            break
    if not hits:
        log("   (안내 페이지 본문에 '자치법규'라는 말이 없다)")

    log("")
    log("'입법예고'가 나오는 자리:")
    hits = 0
    for m in re.finditer("입법예고", text):
        log("   …%s…" % text[max(0, m.start() - 80):m.start() + 80].strip())
        hits += 1
        if hits >= 6:
            break


def try_endpoints():
    """이름을 짐작해 몇 개 걸어 본다. 안내 페이지가 답을 주면 이건 참고용이다."""
    log("")
    log("=" * 70)
    log("2) 있을 법한 주소를 걸어 본다")
    log("=" * 70)
    if not oc():
        log("OC 가 없어 건너뛴다.")
        return
    base = "https://www.lawmaking.go.kr/rest/%s.xml?OC=%s&diff=0&pageSize=3&pageIndex=1"
    names = [
        "ogLmPpMod",        # 아는 것 — 비교 기준
        "ordinLmPpMod",
        "ogOrdinPpMod",
        "ordinPpMod",
        "ogJchLmPpMod",
        "jchLmPpMod",
        "lmPpModOrdin",
        "ogLmPpModOrdin",
    ]
    for name in names:
        url = base % (name, urllib.parse.quote(oc()))
        t0 = time.time()
        body = get(url, timeout=40)
        took = time.time() - t0
        if body is None:
            log("  %-16s 응답 없음 (%.0f초)" % (name, took))
        else:
            head = re.sub(r"\s+", " ", body[:160])
            n = len(re.findall(r"<(?:ogLmPpSeq|lmPpSeq|seq)>", body))
            log("  %-16s %6.1f초  길이%-7d 항목%-3d  %s"
                % (name, took, len(body), n, head[:90]))
        time.sleep(1.5)


def main():
    if not oc():
        log("LAWMAKING_OC 가 없다 — 안내 페이지만 읽는다.")
    read_guide()
    try_endpoints()
    return 0


if __name__ == "__main__":
    sys.exit(main())
