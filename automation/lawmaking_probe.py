# -*- coding: utf-8 -*-
"""국민참여입법센터(입법예고) OPEN API 진단용 스크립트.

목적: "킥보드·개인형 이동장치·자전거·전기자전거 관련 입법예고가 뜨면 알림"이
      기술적으로 가능한지 확인한다. 즉 (1) 목록 조회 API가 실제로 존재하는가,
      (2) 무엇으로 필터/검색할 수 있는가, (3) 새 글을 식별할 키가 있는가.

성격: schedule_debug.py와 같은 진단 도구다. 상태 파일을 쓰지 않고, 슬랙도 보내지
      않는다. 읽고 출력만 한다. 손으로만 실행한다(lawmaking-probe.yml).

주의: lawmaking.go.kr 도메인은 개발 컨테이너의 egress 정책에서 막혀 있다.
      GitHub Actions 러너에서 실행하는 것을 전제로 한다.

OC(승인 아이디)는 LAWMAKING_OC 환경변수로 받는다. 이 저장소는 퍼블릭이므로
파일에 적지 않고, 출력할 때도 가린다.
"""
import os, re, sys, json, time, urllib.request, urllib.parse, urllib.error

OC = os.environ.get("LAWMAKING_OC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 20


def redact(text):
    """퍼블릭 저장소의 실행 로그에 OC가 그대로 남지 않게 가린다."""
    if OC and text:
        return text.replace(OC, "***OC***")
    return text


def fetch(url, label=""):
    """(status, content_type, body_text)를 돌려준다. 실패해도 예외를 올리지 않는다."""
    print("\n>>> GET %s  %s" % (redact(url), label))
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            body = raw.decode("utf-8", "replace")
            print("    HTTP %s | %s | %d bytes" % (resp.status, ctype, len(raw)))
            return resp.status, ctype, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        print("    HTTP %s (오류) | %d bytes" % (e.code, len(body)))
        if body:
            print("    본문 앞부분: %s" % redact(strip_tags(body))[:400])
        return e.code, "", body
    except Exception as e:
        print("    실패: %r" % (e,))
        return None, "", ""


TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
ANY_TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")


def strip_tags(html):
    t = TAG_RE.sub(" ", html)
    t = re.sub(r"</(tr|div|p|li|h[1-6]|table)>", "\n", t, flags=re.I)
    t = re.sub(r"</t[dh]>", " | ", t, flags=re.I)
    t = ANY_TAG_RE.sub("", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    lines = [WS_RE.sub(" ", ln).strip(" |") for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip())


def show(text, limit, label="본문"):
    text = redact(text)
    print("--- %s (%d자 중 %d자) ---" % (label, len(text), min(limit, len(text))))
    print(text[:limit])
    if len(text) > limit:
        print("... (이하 생략)")
    print("--- 끝 ---")


def step1_guide():
    """API 활용가이드 페이지를 그대로 읽어 온다. 문서보다 실측이 빠르지만,
    엔드포인트 이름만은 문서를 봐야 안다."""
    print("\n" + "=" * 70)
    print("STEP 1. API 활용가이드 페이지 수집")
    print("=" * 70)

    guide_urls = [
        "https://opinion.lawmaking.go.kr/api/apiGuideInfo",
        "https://opinion.lawmaking.go.kr/api/apiGuideList",
        "https://opinion.lawmaking.go.kr/api/apiGuide",
    ]
    hrefs = set()
    for u in guide_urls:
        st, ctype, body = fetch(u, "(활용가이드)")
        if st == 200 and body:
            show(strip_tags(body), 12000, "가이드 본문 %s" % u.rsplit("/", 1)[-1])
            for m in re.finditer(r'href=["\']([^"\']+)["\']', body):
                h = m.group(1)
                if "apiGuide" in h or "/rest/" in h or "api" in h.lower():
                    hrefs.add(h)
            # 페이지 안에 박혀 있는 rest URL 패턴도 긁는다
            for m in re.finditer(r'https?://[\w.\-]*lawmaking\.go\.kr[^\s"\'<>\\)]*', body):
                hrefs.add(m.group(0))
        time.sleep(0.4)

    if hrefs:
        print("\n가이드 페이지에서 발견한 링크/URL %d개:" % len(hrefs))
        for h in sorted(hrefs):
            print("   %s" % h)
    return hrefs


def follow_guide_links(hrefs, limit=8):
    """가이드 상세(엔드포인트별 파라미터 표)가 따로 있으면 그것도 읽는다."""
    print("\n" + "=" * 70)
    print("STEP 2. 가이드 상세 페이지 추적")
    print("=" * 70)
    seen = 0
    for h in sorted(hrefs):
        if seen >= limit:
            print("(상세 링크가 더 있으나 %d개에서 멈춤)" % limit)
            break
        if h.startswith("/"):
            h = "https://opinion.lawmaking.go.kr" + h
        if "lawmaking.go.kr" not in h:
            continue
        if "apiGuide" not in h:
            continue
        st, ctype, body = fetch(h, "(가이드 상세)")
        if st == 200 and body:
            show(strip_tags(body), 8000, "상세 %s" % h)
            seen += 1
        time.sleep(0.4)


def step3_candidates():
    """목록 조회 엔드포인트 후보를 실제로 찔러 본다.

    사용자가 알려준 상세 URL은
      https://www.lawmaking.go.kr/rest/ogLmPpMod/{ogLmPpSeq}/{mappingLbicId}/{announceType}
    로, 'ogLmPp' = 정부 입법예고, 'Mod'가 상세로 보인다. 목록 쪽 이름을
    문서에서 확정하기 전까지는 흔한 변형을 함께 던져 응답 형태로 판별한다.
    """
    print("\n" + "=" * 70)
    print("STEP 3. 목록 조회 엔드포인트 후보 실측")
    print("=" * 70)
    if not OC:
        print("LAWMAKING_OC 가 비어 있다 — 인증이 필요한 호출은 실패할 수 있다.")

    q = urllib.parse.quote
    base_rest = "https://www.lawmaking.go.kr/rest"
    candidates = []
    for path in ["lmPp", "ogLmPp", "ogLmPpList", "lmPpList", "ogLmPpMod"]:
        candidates.append("%s/%s?OC=%s&type=JSON&display=20&page=1" % (base_rest, path, OC))
    # 법제처 계열 OPEN API의 공통 형태(target 파라미터)도 확인
    for target in ["lmPp", "ogLmPp", "admPp"]:
        candidates.append("https://www.lawmaking.go.kr/DRF/lawSearch.do?OC=%s&target=%s&type=JSON&display=20" % (OC, target))
        candidates.append("https://www.law.go.kr/DRF/lawSearch.do?OC=%s&target=%s&type=JSON&display=20" % (OC, target))
    # 검색어가 먹는지: '킥보드'를 그대로 질의해 본다
    candidates.append("%s/lmPp?OC=%s&type=JSON&display=20&page=1&query=%s" % (base_rest, OC, q("킥보드")))
    candidates.append("https://www.law.go.kr/DRF/lawSearch.do?OC=%s&target=lmPp&type=JSON&display=20&query=%s" % (OC, q("킥보드")))

    for u in candidates:
        st, ctype, body = fetch(u, "(목록 후보)")
        if not body:
            continue
        head = body.lstrip()[:1]
        if head in "{[":
            try:
                data = json.loads(body)
                print("    JSON 파싱 성공. 최상위 키: %s" % list(data)[:10] if isinstance(data, dict) else "(배열)")
                show(json.dumps(data, ensure_ascii=False, indent=1), 4000, "JSON")
            except Exception as e:
                print("    JSON 파싱 실패(%s)" % e)
                show(body, 1500, "원문")
        else:
            show(strip_tags(body), 1200, "원문(태그 제거)")
        time.sleep(0.4)


def step4_detail_shape():
    """사용자가 준 상세 URL의 응답 형태를 본다. 값은 모르니 오류 형태만 확인해도
    파라미터 의미를 좁힐 수 있다."""
    print("\n" + "=" * 70)
    print("STEP 4. 상세 조회 URL 응답 형태")
    print("=" * 70)
    for u in [
        "https://www.lawmaking.go.kr/rest/ogLmPpMod/1/1/1?OC=%s&type=JSON" % OC,
        "https://www.lawmaking.go.kr/rest/ogLmPpMod/1/1/1",
    ]:
        st, ctype, body = fetch(u, "(상세 형태)")
        if body:
            show(strip_tags(body) if "<" in body[:200] else body, 1500, "응답")
        time.sleep(0.4)


def step5_html_list():
    """API가 막히더라도 공개 목록 화면이 있으면 최후 수단(HTML 파싱)이 된다.
    가능/불가능 판정을 위해 화면 존재 여부까지는 확인해 둔다."""
    print("\n" + "=" * 70)
    print("STEP 5. 공개 목록 화면(최후 수단) 존재 확인")
    print("=" * 70)
    for u in [
        "https://opinion.lawmaking.go.kr/gcom/nsmLmSts",
        "https://opinion.lawmaking.go.kr/gcom/ogLmPp",
        "https://opinion.lawmaking.go.kr/",
    ]:
        st, ctype, body = fetch(u, "(공개 화면)")
        if st == 200 and body:
            show(strip_tags(body), 2500, "화면 %s" % u)
        time.sleep(0.4)


def main():
    print("입법예고 API 진단 시작 (OC %s)" % ("설정됨" if OC else "없음"))
    hrefs = step1_guide()
    follow_guide_links(hrefs)
    step3_candidates()
    step4_detail_shape()
    step5_html_list()
    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
