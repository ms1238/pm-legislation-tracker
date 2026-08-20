# -*- coding: utf-8 -*-
"""국민참여입법센터(입법예고) 조회 경로 진단용 스크립트.

목적: "킥보드·개인형 이동장치·자전거·전기자전거 관련 입법예고가 뜨면 알림"이
      기술적으로 가능한지 확인한다. 즉 (1) 목록을 어떤 경로로 읽을 수 있는가,
      (2) 무엇으로 걸러낼 수 있는가, (3) 새 글을 식별할 키가 있는가.

성격: schedule_debug.py와 같은 진단 도구다. 상태 파일을 쓰지 않고, 슬랙도 보내지
      않는다. 읽고 출력만 한다. 손으로만 실행한다.

주의: lawmaking.go.kr 도메인은 개발 컨테이너의 egress 정책에서 막혀 있다.
      GitHub Actions 러너에서 실행하는 것을 전제로 한다.

1차 진단에서 확인한 것:
  - opinion.lawmaking.go.kr/api/apiGuideInfo 는 HTTP 200에 본문 0바이트다
    (문서가 화면 스크립트로 그려지는 듯). 문서로는 스펙을 못 얻는다.
  - www.lawmaking.go.kr/rest/... 는 짐작한 이름 전부 404였고, 200이 온 하나도
    JSON이 아니라 공통 HTML 껍데기였다. 엔드포인트 이름은 추측으로 못 맞춘다.
  - law.go.kr 계열은 OC가 있어도 "서버장비 IP·도메인 등록" 검증을 건다.
  - 반면 공개 목록 화면(opinion.lawmaking.go.kr/gcom/ogLmPp)은 서버가 그린
    HTML로 233건이 그대로 들어 있었다.

그래서 2차는 세 가지를 본다:
  A. 목록 화면 HTML에서 진짜 식별자(ogLmPpSeq 등)를 뽑아, 사용자가 알려준
     REST 상세 URL에 실제 값을 넣어 호출한다 — 응답이 JSON인지, OC/IP 검증을
     거는지 확인.
  B. 목록 화면의 검색 파라미터(제명·기간·부처)가 GET으로 먹는지 — '킥보드',
     '개인형 이동장치'로 실제 검색해 본다.
  C. 상세 화면 본문에 키워드가 실리는지 — 제명만으로는 PM 관련 개정을 놓친다.

OC(승인 아이디)는 LAWMAKING_OC 환경변수로 받는다. 이 저장소는 퍼블릭이므로
파일에 적지 않고, 출력할 때도 가린다.
"""
import os, re, sys, json, time, urllib.request, urllib.parse, urllib.error

OC = os.environ.get("LAWMAKING_OC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25

LIST_URL = "https://opinion.lawmaking.go.kr/gcom/ogLmPp"

# 이 트래커가 찾는 말들. 제명에만 걸면 "도로교통법 시행령 일부개정령안"처럼
# 본문에서야 정체가 드러나는 건을 놓친다.
KEYWORDS = ["개인형 이동장치", "개인형이동장치", "전동킥보드", "킥보드",
            "자전거", "전기자전거", "퍼스널 모빌리티", "퍼스널모빌리티", "이동장치"]


def redact(text):
    """퍼블릭 저장소의 실행 로그에 OC가 그대로 남지 않게 가린다."""
    if OC and text:
        return text.replace(OC, "***OC***")
    return text


def fetch(url, label="", data=None):
    """(status, content_type, body_text)를 돌려준다. 실패해도 예외를 올리지 않는다."""
    print("\n>>> %s %s  %s" % ("POST" if data else "GET", redact(url), label))
    body_bytes = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body_bytes, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            print("    HTTP %s | %s | %d bytes" % (resp.status, ctype, len(raw)))
            return resp.status, ctype, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            b = e.read().decode("utf-8", "replace")
        except Exception:
            b = ""
        print("    HTTP %s (오류) | %d bytes" % (e.code, len(b)))
        return e.code, "", b
    except Exception as e:
        print("    실패: %r" % (e,))
        return None, "", ""


TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
ANY_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html):
    t = TAG_RE.sub(" ", html)
    t = re.sub(r"</(tr|div|p|li|h[1-6]|table)>", "\n", t, flags=re.I)
    t = re.sub(r"</t[dh]>", " | ", t, flags=re.I)
    t = ANY_TAG_RE.sub("", t)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        t = t.replace(a, b)
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" |") for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip())


def show(text, limit, label="본문"):
    text = redact(text)
    print("--- %s (%d자 중 %d자) ---" % (label, len(text), min(limit, len(text))))
    print(text[:limit])
    if len(text) > limit:
        print("... (이하 생략)")
    print("--- 끝 ---")


def step_a_ids():
    """목록 화면에서 상세로 넘어가는 링크·파라미터를 뽑는다."""
    print("\n" + "=" * 70)
    print("STEP A. 목록 화면에서 상세 식별자 추출")
    print("=" * 70)
    st, ctype, html = fetch(LIST_URL, "(부처 입법예고 목록)")
    if st != 200 or not html:
        return None, []

    total = re.search(r"전체\s*([\d,]+)\s*건", strip_tags(html))
    print("    목록이 밝힌 총 건수: %s" % (total.group(1) if total else "(못 찾음)"))

    # 상세 이동은 대개 자바스크립트 함수 호출이다. 인자 묶음을 통째로 본다.
    calls = re.findall(r"(?:onclick|href)\s*=\s*[\"']\s*(?:javascript:)?([A-Za-z_]\w*)\(([^)]*)\)", html)
    seen = {}
    for fn, args in calls:
        seen.setdefault(fn, []).append(args.strip())
    print("\n    화면에서 쓰는 이동 함수(상위 12개):")
    for fn, argl in sorted(seen.items(), key=lambda kv: -len(kv[1]))[:12]:
        print("      %-28s %d회, 예: %s" % (fn, len(argl), argl[0][:120]))

    # 폼 필드 이름 — 검색 파라미터 후보다.
    fields = sorted(set(re.findall(r'<(?:input|select)[^>]*\bname=["\']([^"\']+)["\']', html)))
    print("\n    목록 화면 폼 필드(%d개): %s" % (len(fields), ", ".join(fields)))

    forms = re.findall(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>', html, re.I)
    print("    form action: %s" % (", ".join(sorted(set(forms))) or "(없음)"))

    # 숫자 식별자 후보
    seqs = re.findall(r"ogLmPpSeq['\"]?\s*[:=]\s*['\"]?(\d+)", html)
    seqs += re.findall(r"lmPpSeq['\"]?\s*[:=]\s*['\"]?(\d+)", html)
    print("    본문에 박힌 seq 후보: %s" % (sorted(set(seqs))[:10] or "(없음)"))
    return html, sorted(set(seqs))


def step_b_search():
    """목록 화면 검색이 GET 파라미터로 먹는지 — 이게 되면 알림은 사실상 끝이다."""
    print("\n" + "=" * 70)
    print("STEP B. 목록 검색 파라미터 실측")
    print("=" * 70)
    q = urllib.parse.quote
    trials = [
        ("제명검색 없음(기준)", ""),
        ("lmttNm=자전거", "?lmttNm=%s" % q("자전거")),
        ("searchNm=자전거", "?searchNm=%s" % q("자전거")),
        ("lsNm=자전거", "?lsNm=%s" % q("자전거")),
        ("searchWord=자전거", "?searchWord=%s" % q("자전거")),
        ("srchWrd=자전거", "?srchWrd=%s" % q("자전거")),
    ]
    for label, qs in trials:
        st, ctype, html = fetch(LIST_URL + qs, "(%s)" % label)
        if st == 200 and html:
            txt = strip_tags(html)
            m = re.search(r"전체\s*([\d,]+)\s*건", txt)
            hits = [k for k in KEYWORDS if k in txt]
            print("    → 총 %s건 | 화면에 보이는 키워드: %s"
                  % (m.group(1) if m else "?", ", ".join(hits) or "없음"))
        time.sleep(0.4)


def step_c_detail(seqs):
    """사용자가 알려준 REST 상세 URL에 실제 값을 넣어 본다."""
    print("\n" + "=" * 70)
    print("STEP C. REST 상세 URL 실측")
    print("=" * 70)
    if not OC:
        print("LAWMAKING_OC 가 비어 있다 — 인증이 필요한 호출은 실패할 수 있다.")
    if not seqs:
        print("목록에서 seq를 못 뽑았다. 알려진 형태만 확인한다.")
    for seq in (seqs or [])[:3]:
        for tail in ["/0/1", "/1/1", ""]:
            u = "https://www.lawmaking.go.kr/rest/ogLmPpMod/%s%s" % (seq, tail)
            for suffix in ["?OC=%s&type=JSON" % OC, ""]:
                st, ctype, body = fetch(u + suffix, "(상세)")
                if st == 200 and body:
                    if body.lstrip()[:1] in "{[":
                        show(body, 2500, "JSON 응답")
                    else:
                        show(strip_tags(body), 800, "HTML 응답")
                time.sleep(0.3)


def step_d_guide():
    """API 활용가이드를 다른 방법으로 얻어 본다(POST / 파라미터 / 안내 메뉴)."""
    print("\n" + "=" * 70)
    print("STEP D. API 활용가이드 재시도")
    print("=" * 70)
    st, ctype, body = fetch("https://opinion.lawmaking.go.kr/api/apiGuideInfo",
                            "(POST 시도)", data={"apiSeq": "1"})
    if body:
        show(strip_tags(body) if "<" in body[:200] else body, 3000, "POST 응답")

    for u in ["https://opinion.lawmaking.go.kr/api/apiGuideInfo?apiSeq=1",
              "https://opinion.lawmaking.go.kr/gcom/lmInfoOpen",
              "https://opinion.lawmaking.go.kr/gcom/infoOpenUse"]:
        st, ctype, body = fetch(u, "(가이드 후보)")
        if st == 200 and body:
            show(strip_tags(body), 2500, u)
        time.sleep(0.3)


def main():
    print("입법예고 조회 경로 진단 시작 (OC %s)" % ("설정됨" if OC else "없음"))
    html, seqs = step_a_ids()
    step_b_search()
    step_c_detail(seqs)
    step_d_guide()
    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
