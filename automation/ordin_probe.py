# -*- coding: utf-8 -*-
"""두 가지를 알아본다. 읽기만 하고 아무것도 저장하지 않는다.

 1. 401 이 간헐적인가. 같은 OC로 어떤 날은 되고 어떤 날은 안 된다. 한 번 걸러
    포기할 일인지, 기다렸다 다시 걸 일인지에 따라 고칠 곳이 다르다.
 2. 자치법규(지방) 입법예고를 여는 주소가 있는가. 안내 페이지를 못 읽어서
    이름을 짐작만 했고, 짐작한 여덟 개는 다 404였다.

개발 컨테이너에서는 lawmaking.go.kr 이 막혀 있어 러너에서만 돌아간다.
답을 얻으면 지울 파일이다.
"""
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REST = "https://www.lawmaking.go.kr/rest"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}


def oc():
    return os.environ.get("LAWMAKING_OC", "").strip()


def redact(t):
    return t.replace(oc(), "***OC***") if (oc() and t) else t


def log(m):
    print(m, flush=True)


def get(url, timeout=30):
    """(본문, 설명). 실패해도 왜 실패했는지는 남긴다."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), "HTTP %d" % r.status
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return None, "HTTP %d %s %s" % (e.code, e.reason, body[:120])
    except Exception as e:
        return None, repr(e)


def ret_msg(xml):
    m = re.search(r"<retMsg>([^<]*)</retMsg>", xml or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- 1) 401
def auth_pattern(times=6, gap=20):
    """같은 요청을 여러 번 건다. 다 401이면 설정, 섞이면 조임이다."""
    log("=" * 68)
    log("1) 401 이 계속되는가, 섞이는가 — 같은 요청 %d번" % times)
    log("=" * 68)
    if not oc():
        log("OC 없음 — 건너뛴다")
        return
    url = ("%s/ogLmPpMod.xml?OC=%s&diff=0&pageSize=1&pageIndex=1"
           % (REST, urllib.parse.quote(oc())))
    results = []
    for i in range(1, times + 1):
        t0 = time.time()
        body, how = get(url, timeout=40)
        took = time.time() - t0
        code = ret_msg(body) if body else None
        rows = len(re.findall(r"<ogLmPpSeq>", body or ""))
        verdict = ("retMsg=%s" % code) if code else ("정상 %d건" % rows if body else how)
        results.append(code or ("ok" if body else "err"))
        log("  %d회  %5.1f초  %s" % (i, took, verdict))
        if i < times:
            time.sleep(gap)
    ok = sum(1 for r in results if r == "ok")
    log("")
    log("  정상 %d / 401 %d / 기타 %d"
        % (ok, results.count("401"), len(results) - ok - results.count("401")))
    if ok and "401" in results:
        log("  => 섞인다. 설정 문제가 아니라 조임이다 — 기다렸다 다시 걸면 된다.")
    elif not ok:
        log("  => 전부 실패. 승인 아이디 상태를 사람이 확인해야 한다.")
    else:
        log("  => 전부 정상. 401 은 아까 그 순간만의 일이었다.")


# ------------------------------------------------------- 2) 자치법규 주소
GUIDES = [
    "https://opinion.lawmaking.go.kr/api/apiGuideInfo",
    "https://opinion.lawmaking.go.kr/api/apiGuide",
    "https://www.lawmaking.go.kr/api/apiGuideInfo",
    "https://opinion.lawmaking.go.kr/lmPp/nsmLmPpList",
    "https://opinion.lawmaking.go.kr/",
]


def read_guides():
    log("")
    log("=" * 68)
    log("2) 안내 페이지를 읽어 서비스 이름을 찾는다")
    log("=" * 68)
    found = set()
    for url in GUIDES:
        body, how = get(url, timeout=30)
        log("  %-52s %s" % (url.replace("https://", ""), how))
        if not body:
            continue
        names = set(re.findall(r"/rest/([A-Za-z0-9_]+)", body))
        if names:
            log("      /rest/ 이름: %s" % ", ".join(sorted(names)))
            found |= names
        for word in ("자치법규", "조례", "지방"):
            if word in body:
                log("      '%s' 이 페이지에 있다" % word)
        time.sleep(1)
    if found:
        log("")
        log("  모은 이름: %s" % ", ".join(sorted(found)))
    else:
        log("")
        log("  어느 페이지에서도 /rest/ 이름을 못 찾았다.")


CANDIDATES = [
    "ogLmPpMod",          # 아는 것 — 비교 기준
    "ordinLmPp", "ordinPp", "ogOrdinLmPp", "ordLmPpMod", "ordLmPp",
    "autoLmPpMod", "atrLmPpMod", "locLmPpMod", "ltcLmPpMod",
    "ogLmPpModOrd", "ogOrdLmPpMod", "ogAutoLmPpMod",
    "jaLmPpMod", "jchbLmPpMod", "ordinModPp",
]


def try_candidates():
    log("")
    log("=" * 68)
    log("3) 이름을 더 걸어 본다 (404=그런 서비스 없음)")
    log("=" * 68)
    if not oc():
        log("OC 없음 — 건너뛴다")
        return
    hits = []
    for name in CANDIDATES:
        url = ("%s/%s.xml?OC=%s&diff=0&pageSize=2&pageIndex=1"
               % (REST, name, urllib.parse.quote(oc())))
        body, how = get(url, timeout=30)
        code = ret_msg(body) if body else None
        if body and not code:
            log("  %-16s ★ 응답 있음 (길이 %d)" % (name, len(body)))
            log("       %s" % re.sub(r"\s+", " ", body[:200]))
            hits.append(name)
        else:
            log("  %-16s %s" % (name, ("retMsg=%s" % code) if code else how))
        time.sleep(1.5)
    log("")
    log("  살아 있는 이름: %s" % (", ".join(hits) if hits else "없음"))


def main():
    auth_pattern()
    read_guides()
    try_candidates()
    return 0


if __name__ == "__main__":
    sys.exit(main())
