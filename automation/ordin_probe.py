# -*- coding: utf-8 -*-
"""401 이 러너 IP를 타는지 보고, 자치법규 입법예고 주소를 찾는다.

한 job 안에서 20초 간격으로 여섯 번을 걸었더니 전부 401이었다. 그런데 어제는
같은 OC로 성공했다. job 하나는 러너 하나이고 러너 하나는 나가는 IP 하나이므로,
"어떤 IP는 되고 어떤 IP는 안 된다"면 두 관찰이 같이 설명된다. law.go.kr 이
IP 등록을 요구하는 것과 같은 부류다.

그래서 이 파일은 나가는 IP를 먼저 찍는다. 이걸 여러 번 돌려 IP와 결과를
나란히 놓으면 가설이 맞는지 한눈에 보인다.

읽기만 한다. 러너에서만 돌아간다. 답을 얻으면 지울 파일이다.
"""
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REST = "https://www.lawmaking.go.kr/rest"
GUIDE = "https://opinion.lawmaking.go.kr/api/apiGuideInfo"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}


def oc():
    return os.environ.get("LAWMAKING_OC", "").strip()


def log(m):
    print(m, flush=True)


def get(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), "HTTP %d" % r.status
    except urllib.error.HTTPError as e:
        return None, "HTTP %d %s" % (e.code, e.reason)
    except Exception as e:
        return None, repr(e)


def ret_msg(xml):
    m = re.search(r"<retMsg>([^<]*)</retMsg>", xml or "")
    return m.group(1) if m else None


def egress_ip():
    """이 러너가 바깥으로 나갈 때 쓰는 주소."""
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com",
                "https://ifconfig.me/ip"):
        body, how = get(url, timeout=15)
        if body and re.match(r"^\d+\.\d+\.\d+\.\d+\s*$", body):
            return body.strip()
    return "(못 알아냄)"


def one_call():
    url = ("%s/ogLmPpMod.xml?OC=%s&diff=0&pageSize=1&pageIndex=1"
           % (REST, urllib.parse.quote(oc())))
    t0 = time.time()
    body, how = get(url, timeout=40)
    took = time.time() - t0
    code = ret_msg(body) if body else None
    if code:
        return "retMsg=%s" % code, took
    if body:
        return "정상 %d건" % len(re.findall(r"<ogLmPpSeq>", body)), took
    return how, took


def dump_guide():
    """안내 페이지가 서비스 목록을 어떻게 담고 있는지 본다.

    /rest/ 가 본문에 없었다. 목록이 자바스크립트로 채워지거나 상세 페이지로
    링크만 걸려 있을 수 있어, 이번엔 실제 구조를 보고 판단한다.
    """
    log("")
    log("=" * 66)
    log("안내 페이지 구조")
    log("=" * 66)
    body, how = get(GUIDE, timeout=30)
    log("  %s  (길이 %s)" % (how, len(body) if body else "-"))
    if not body:
        return

    links = re.findall(r'href=["\']([^"\']+)["\']', body)
    inner = sorted({l for l in links
                    if not l.startswith(("http", "#", "javascript", "mailto"))})
    log("  내부 링크 %d개 (앞 25개):" % len(inner))
    for l in inner[:25]:
        log("     %s" % l)

    # 목록을 눌렀을 때 부르는 주소가 onclick/data-*/script 안에 있을 수 있다.
    calls = sorted(set(re.findall(r'(?:fn\w+|goDetail|apiDetail)\([^)]*\)', body)))
    if calls:
        log("  스크립트 호출 %d개 (앞 15개):" % len(calls))
        for c in calls[:15]:
            log("     %s" % c[:100])

    ids = sorted(set(re.findall(r'(?:apiId|svcId|serviceId|guideId)["\']?\s*[:=]\s*["\']?(\w+)', body)))
    if ids:
        log("  서비스 식별자로 보이는 값: %s" % ", ".join(ids[:30]))

    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    for word in ("자치법규", "조례", "입법예고", "국회"):
        spots = [m.start() for m in re.finditer(word, text)][:3]
        if spots:
            log("  '%s' %d곳:" % (word, len(re.findall(word, text))))
            for p in spots:
                log("     …%s…" % text[max(0, p - 60):p + 60].strip())


def main():
    log("=" * 66)
    log("나가는 IP : %s" % egress_ip())
    verdict, took = one_call() if oc() else ("OC 없음", 0)
    log("목록 한 건: %s  (%.1f초)" % (verdict, took))
    log("=" * 66)
    dump_guide()
    return 0


if __name__ == "__main__":
    sys.exit(main())
