# -*- coding: utf-8 -*-
"""국민참여입법센터(입법예고) 조회 경로 진단용 스크립트.

목적: "킥보드·개인형 이동장치·자전거·전기자전거 관련 입법예고가 뜨면 알림"을
      붙일 수 있는지, 붙인다면 어떤 경로로 붙일지 확정한다.

성격: schedule_debug.py와 같은 진단 도구다. 상태 파일을 쓰지 않고, 슬랙도 보내지
      않는다. 읽고 출력만 한다. 손으로만 실행한다.

주의: lawmaking.go.kr 도메인은 개발 컨테이너의 egress 정책에서 막혀 있다.
      GitHub Actions 러너에서 실행하는 것을 전제로 한다.

여기까지 확인한 것:
  - 상세 조회 API는 OC 인증으로 러너에서 그냥 통한다(등록 IP 검증 없음).
      https://www.lawmaking.go.kr/rest/ogLmPpMod/{ogLmPpSeq}/{mappingLbicId}/{announceType}.xml?OC=...
    잘못된 OC는 <result><retMsg>401</retMsg></result> 를 준다.
    응답 필드: ogLmPpSeq, lsNm, asndOfiNm, asndDptNm, lmTpNm, lsClsNm,
               stYd, edYd, telNo, faxNo, email, modDt, status, readCnt, lmPpCts.
    우리가 키워드를 걸 자리는 lmPpCts(예고 본문)다.
  - 목록 화면(opinion.lawmaking.go.kr/gcom/ogLmPp)은 서버가 그린 HTML이고,
    각 행은 /gcom/ogLmPp/{번호} 로 이어진다(예: 88178).
  - lsNm(제명) 검색은 GET으로 먹지만 제명만 본다.
    '자전거' 1건, '도로교통' 2건(종료포함 6건), '킥보드'·'이동장치' 0건 —
    즉 제명 검색만으로는 PM 관련 개정을 놓친다. 본문을 봐야 한다.

남은 미해결 하나: 목록의 번호(88178)와 API가 요구하는 ogLmPpSeq(28212) ·
mappingLbicId(2000000141134) 가 서로 다르다. 이 둘을 어디서 얻는지가
자동 감시의 마지막 조각이다. 이번 진단은 그걸 찾는다.

OC(승인 아이디)는 LAWMAKING_OC 환경변수로 받는다. 이 저장소는 퍼블릭이므로
파일에 적지 않고, 출력할 때도 가린다.
"""
import os, re, sys, time, urllib.request, urllib.parse, urllib.error

OC = os.environ.get("LAWMAKING_OC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25

LIST_URL = "https://opinion.lawmaking.go.kr/gcom/ogLmPp"
REST_BASE = "https://www.lawmaking.go.kr/rest/ogLmPpMod"

KEYWORDS = ["개인형 이동장치", "개인형이동장치", "전동킥보드", "킥보드",
            "자전거", "전기자전거", "퍼스널 모빌리티", "퍼스널모빌리티"]


def redact(text):
    if OC and text:
        return text.replace(OC, "***OC***")
    return text


def fetch(url, label="", tries=3):
    """러너에서 .go.kr 로 나가는 연결은 자주 끊긴다(한 실행은 국회 API까지 5/5
    타임아웃이었다). 한 번 실패했다고 '자료가 없다'로 읽으면 안 되므로 재시도한다."""
    print("\n>>> GET %s  %s" % (redact(url), label))
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                print("    HTTP %s | %s | %d bytes%s"
                      % (resp.status, resp.headers.get("Content-Type", ""), len(raw),
                         "" if attempt == 1 else " (%d번째 시도)" % attempt))
                return resp.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                b = e.read().decode("utf-8", "replace")
            except Exception:
                b = ""
            print("    HTTP %s (오류) | %d bytes" % (e.code, len(b)))
            return e.code, b
        except Exception as e:
            print("    시도 %d/%d 실패: %r" % (attempt, tries, e))
            if attempt < tries:
                time.sleep(2 * attempt)
    return None, ""


def strip_tags(html):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&middot;", "·"), ("&ldquo;", '"'), ("&rdquo;", '"')]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def show(text, limit, label="본문"):
    text = redact(text)
    print("--- %s (%d자 중 %d자) ---" % (label, len(text), min(limit, len(text))))
    print(text[:limit])
    if len(text) > limit:
        print("... (이하 생략)")
    print("--- 끝 ---")


def list_rows(query=""):
    """목록 화면에서 (번호, 제명, 부처) 를 뽑는다."""
    st, html = fetch(LIST_URL + query, "(목록)")
    if st != 200 or not html:
        return []
    rows = []
    body = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S | re.I)
    if not body:
        return []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", body.group(1), re.S | re.I):
        a = re.search(r'href="/gcom/ogLmPp/(\d+)"[^>]*title="([^"]*)"', tr)
        if not a:
            continue
        offices = re.findall(r"<p>([^<]+)</p>", tr)
        rows.append((a.group(1), a.group(2), offices[0].strip() if offices else ""))
    return rows


def step1_detail_page(no, title):
    """상세 화면 HTML에서 ogLmPpSeq / mappingLbicId / announceType 을 찾는다."""
    print("\n" + "-" * 70)
    print("상세 화면 %s — %s" % (no, title))
    print("-" * 70)
    st, html = fetch("https://opinion.lawmaking.go.kr/gcom/ogLmPp/%s" % no, "(상세 화면)")
    if st != 200 or not html:
        return None

    found = {}
    for name in ["ogLmPpSeq", "mappingLbicId", "announceType", "lbicId", "lsiSeq", "TYPE"]:
        hits = [m.start() for m in re.finditer(name, html)]
        print("    '%s' 등장 %d회" % (name, len(hits)))
        for pos in hits[:2]:
            show(html[max(0, pos - 200): pos + 200], 420, "'%s' 주변" % name)
        v = re.search(r"%s['\"]?\s*[:=]\s*['\"]?(\w+)" % name, html)
        if v:
            found[name] = v.group(1)

    # 긴 숫자(mappingLbicId 후보)가 화면 어딘가에 박혀 있을 수 있다.
    longs = sorted(set(re.findall(r"\b(\d{11,14})\b", html)))
    print("    11~14자리 숫자 후보: %s" % (longs[:10] or "(없음)"))

    txt = strip_tags(html)
    hits = [k for k in KEYWORDS if k in txt]
    print("    상세 화면 텍스트 %d자, 키워드 적중: %s" % (len(txt), ", ".join(hits) or "없음"))
    print("    발견한 식별자: %s" % (found or "(없음)"))
    return found, longs


def step2_rest(seq, lbic, atype="TYPE5"):
    """찾은 식별자로 실제 API를 부른다."""
    url = "%s/%s/%s/%s.xml?OC=%s" % (REST_BASE, seq, lbic, atype, OC)
    st, body = fetch(url, "(REST 상세)")
    if not body:
        return
    m = re.search(r"<lmPpCts>(.*?)</lmPpCts>", body, re.S)
    nm = re.search(r"<lsNm>(.*?)</lsNm>", body, re.S)
    print("    lsNm: %s" % (strip_tags(nm.group(1)) if nm else "(없음)"))
    if m:
        cts = strip_tags(m.group(1))
        print("    lmPpCts %d자, 키워드 적중: %s"
              % (len(cts), ", ".join(k for k in KEYWORDS if k in cts) or "없음"))
        show(cts, 600, "lmPpCts 앞부분")
    else:
        show(body, 500, "응답")


def main():
    print("입법예고 조회 경로 진단 시작 (OC %s)" % ("설정됨" if OC else "없음"))

    print("\n" + "=" * 70)
    print("STEP 1. 목록 훑기")
    print("=" * 70)
    rows = list_rows()
    print("    목록 행 %d개. 앞 5개:" % len(rows))
    for r in rows[:5]:
        print("      %s | %s | %s" % r)

    bike = list_rows("?lsNm=%s" % urllib.parse.quote("자전거"))
    print("\n    '자전거' 검색 행 %d개:" % len(bike))
    for r in bike:
        print("      %s | %s | %s" % r)

    print("\n" + "=" * 70)
    print("STEP 2. 상세 화면에서 API 식별자 찾기")
    print("=" * 70)
    targets = (bike[:1] + rows[:1])
    for no, title, office in targets:
        res = step1_detail_page(no, title)
        if not res:
            continue
        found, longs = res
        # 화면에서 찾은 값으로, 안 되면 목록 번호를 seq로 놓고 시도한다.
        seq = found.get("ogLmPpSeq", no)
        for lbic in ([found["mappingLbicId"]] if "mappingLbicId" in found else longs[:2] or ["0"]):
            for atype in ["TYPE5"]:
                step2_rest(seq, lbic, atype)
                time.sleep(0.3)

    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
