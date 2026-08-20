# -*- coding: utf-8 -*-
"""국민참여입법센터(입법예고) 조회 경로 진단용 스크립트.

목적: "킥보드·개인형 이동장치·자전거·전기자전거 관련 입법예고가 뜨면 알림"이
      기술적으로 가능한지 확인한다.

성격: schedule_debug.py와 같은 진단 도구다. 상태 파일을 쓰지 않고, 슬랙도 보내지
      않는다. 읽고 출력만 한다. 손으로만 실행한다.

주의: lawmaking.go.kr 도메인은 개발 컨테이너의 egress 정책에서 막혀 있다.
      GitHub Actions 러너에서 실행하는 것을 전제로 한다.

지금까지 확인한 것:
  - 상세 조회 API 주소는 아래 형태다(사용자 제공):
      https://www.lawmaking.go.kr/rest/ogLmPpMod/{ogLmPpSeq}/{mappingLbicId}/{announceType}.xml?OC=...
    본문은 출력변수 lmPpCts 에 들어간다. 우리가 키워드를 걸 자리가 여기다.
  - 목록은 서버가 그린 화면(opinion.lawmaking.go.kr/gcom/ogLmPp)으로 통째로 온다.
    폼 필드 중 lsNm(법령 제명)은 GET 파라미터로 그대로 먹는다(자전거 → 1건).
  - 다만 목록에서 ogLmPpSeq·mappingLbicId 를 어떻게 얻는지가 아직 미해결이다.
    이 둘이 없으면 상세 API를 부를 수 없다.

그래서 이번엔 세 가지를 본다:
  1. 사용자가 준 예제 URL을 OC를 바꿔 그대로 호출 — 인증이 통하는지, lmPpCts가
     실제로 오는지, 응답 필드가 무엇인지.
  2. 목록 화면 HTML 원문에서 상세로 넘어가는 링크의 실제 모양 — seq·id가 어디에
     실려 있는지.
  3. lsNm 검색이 관심 키워드에 대해 무엇을 돌려주는지.

OC(승인 아이디)는 LAWMAKING_OC 환경변수로 받는다. 이 저장소는 퍼블릭이므로
파일에 적지 않고, 출력할 때도 가린다.
"""
import os, re, sys, time, urllib.request, urllib.parse, urllib.error

OC = os.environ.get("LAWMAKING_OC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25

LIST_URL = "https://opinion.lawmaking.go.kr/gcom/ogLmPp"
REST_BASE = "https://www.lawmaking.go.kr/rest/ogLmPpMod"

# 문서에 실린 예제 건. 값이 살아 있는지와 무관하게 응답 형태는 확인할 수 있다.
SAMPLE = ("28212", "2000000141134", "TYPE5")

KEYWORDS = ["개인형 이동장치", "개인형이동장치", "전동킥보드", "킥보드",
            "자전거", "전기자전거", "퍼스널 모빌리티", "퍼스널모빌리티"]


def redact(text):
    """퍼블릭 저장소의 실행 로그에 OC가 그대로 남지 않게 가린다."""
    if OC and text:
        return text.replace(OC, "***OC***")
    return text


def fetch(url, label=""):
    print("\n>>> GET %s  %s" % (redact(url), label))
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            print("    HTTP %s | %s | %d bytes"
                  % (resp.status, resp.headers.get("Content-Type", ""), len(raw)))
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            b = e.read().decode("utf-8", "replace")
        except Exception:
            b = ""
        print("    HTTP %s (오류) | %d bytes" % (e.code, len(b)))
        return e.code, b
    except Exception as e:
        print("    실패: %r" % (e,))
        return None, ""


ANY_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = ANY_TAG_RE.sub(" ", t)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def show(text, limit, label="본문"):
    text = redact(text)
    print("--- %s (%d자 중 %d자) ---" % (label, len(text), min(limit, len(text))))
    print(text[:limit])
    if len(text) > limit:
        print("... (이하 생략)")
    print("--- 끝 ---")


def step1_sample_detail():
    """예제 URL을 그대로 호출해 인증과 응답 필드를 확인한다."""
    print("\n" + "=" * 70)
    print("STEP 1. 상세 조회 API — 문서 예제 그대로 호출")
    print("=" * 70)
    if not OC:
        print("LAWMAKING_OC 가 비어 있다. 인증 결과는 판정할 수 없다.")

    seq, lbic, atype = SAMPLE
    for oc_label, oc_val in [("내 OC", OC), ("test", "test")]:
        if not oc_val:
            continue
        url = "%s/%s/%s/%s.xml?OC=%s" % (REST_BASE, seq, lbic, atype, oc_val)
        st, body = fetch(url, "(%s / XML)" % oc_label)
        if not body:
            continue
        tags = []
        for t in re.findall(r"<([A-Za-z_][\w.\-]*)[ >]", body):
            if t not in tags:
                tags.append(t)
        print("    응답에 등장한 태그(%d종): %s" % (len(tags), ", ".join(tags[:60])))
        m = re.search(r"<lmPpCts>(.*?)</lmPpCts>", body, re.S)
        if m:
            cts = strip_tags(m.group(1))
            print("    lmPpCts 발견 — 길이 %d자" % len(cts))
            show(cts, 1200, "lmPpCts 앞부분")
            print("    키워드 적중: %s"
                  % (", ".join(k for k in KEYWORDS if k in cts) or "없음"))
        else:
            print("    lmPpCts 없음 — 응답 앞부분을 그대로 보인다")
            show(body, 1500, "응답 원문")
        time.sleep(0.4)


def step2_list_links():
    """목록 화면 HTML에서 상세로 넘어가는 실제 링크 모양을 찾는다."""
    print("\n" + "=" * 70)
    print("STEP 2. 목록 화면에서 ogLmPpSeq / mappingLbicId 찾기")
    print("=" * 70)
    st, html = fetch(LIST_URL, "(부처 입법예고 목록)")
    if st != 200 or not html:
        return

    for name in ["ogLmPpSeq", "mappingLbicId", "announceType", "lbicId", "ppSeq"]:
        hits = [m.start() for m in re.finditer(name, html)]
        print("    '%s' 등장 %d회" % (name, len(hits)))
        for pos in hits[:2]:
            show(html[max(0, pos - 220): pos + 220], 460, "'%s' 주변 원문" % name)

    # 목록 표의 첫 행 원문을 그대로 본다 — 링크가 어떤 모양인지 눈으로 확인.
    body = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S | re.I)
    if body:
        rows = re.findall(r"<tr[^>]*>.*?</tr>", body.group(1), re.S | re.I)
        print("\n    tbody에서 찾은 행 %d개. 첫 행 원문:" % len(rows))
        if rows:
            show(rows[0], 2500, "첫 행 HTML")
    else:
        print("\n    tbody를 못 찾았다. a 태그 중 상세로 보이는 것들:")
        for a in re.findall(r"<a\b[^>]*>", html)[:40]:
            if any(k in a for k in ("ogLmPp", "Info", "detail", "Detail", "View")):
                print("      %s" % a[:220])


def step3_keyword_search():
    """lsNm(제명) 검색이 관심 키워드에 무엇을 돌려주는지."""
    print("\n" + "=" * 70)
    print("STEP 3. 제명(lsNm) 검색 결과")
    print("=" * 70)
    for kw in ["자전거", "도로교통", "킥보드", "이동장치"]:
        for extra in ["", "&finishIncludeYn=Y"]:
            u = "%s?lsNm=%s%s" % (LIST_URL, urllib.parse.quote(kw), extra)
            st, html = fetch(u, "(lsNm=%s%s)" % (kw, " 종료포함" if extra else ""))
            if st == 200 and html:
                txt = strip_tags(html)
                m = re.search(r"전체\s*([\d,]+)\s*건", txt)
                print("    → 총 %s건" % (m.group(1) if m else "?"))
                names = re.findall(r"([가-힣A-Za-z0-9·ㆍ\s]{4,60}(?:일부개정령안|제정안|전부개정령안|일부개정법률안))", txt)
                for n in list(dict.fromkeys(names))[:8]:
                    print("       · %s" % n.strip())
            time.sleep(0.4)


def main():
    print("입법예고 조회 경로 진단 시작 (OC %s)" % ("설정됨" if OC else "없음"))
    step1_sample_detail()
    step2_list_links()
    step3_keyword_search()
    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
