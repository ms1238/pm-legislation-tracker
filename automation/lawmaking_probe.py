# -*- coding: utf-8 -*-
"""입법예고 목록 조회 API의 범위·페이징·필터를 확정하는 진단 스크립트.

왜 필요한가:
  diff=0(진행중)으로 목록을 불렀더니 20건만 왔다. 사이트 화면은 같은 조건에서
  233건이라고 한다. 20건이면 최신 하루치 남짓이라, 하루 한 번 도는 감시로는
  평소엔 충분해도 첫 실행과 밀린 실행에서 구멍이 난다.

  문서의 요청변수에는 페이징이 없다(OC lsClsCd cptOfiOrgCd diff pntcNo pntcNo2
  stYdFmt edYdFmt lsNm). 그러니 둘 중 하나다.
    - 문서에 없는 페이징 파라미터가 먹는다 → 그걸 쓴다.
    - 안 먹는다 → 필터로 쪼개서 나눠 받는다(법령종류 6개, 예고일자 구간).

  겸사겸사 lsNm 필터가 API에서 실제로 먹는지도 못 박는다. 화면 쪽 제명 검색은
  결과 행을 못 뽑았지만, API에서 되면 지정 법 감시는 그쪽으로 하면 된다.

성격: 진단 도구다. 상태 파일을 쓰지 않고 슬랙도 보내지 않는다. 손으로만 실행한다.
주의: OC는 LAWMAKING_OC 환경변수로 받고 출력할 때 가린다(이 저장소는 퍼블릭이다).
"""
import os, re, sys, time, urllib.request, urllib.parse

OC = os.environ.get("LAWMAKING_OC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25
REST = "https://www.lawmaking.go.kr/rest/ogLmPpMod"

# 문서에 실린 법령종류 코드
LS_CLS = [("AA0101", "법률"), ("AA0102", "대통령령"), ("AA0103", "총리령"),
          ("AA0104", "부령"), ("AA0105", "대통령훈령"), ("AA0106", "국무총리훈령")]


def redact(t):
    return t.replace(OC, "***OC***") if (OC and t) else t


def fetch(url, tries=2):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == tries:
                print("    조회 실패 %s — %r" % (redact(url), e))
                return None
            time.sleep(2)
    return None


def seqs_of(xml):
    return re.findall(r"<ogLmPpSeq>(\d+)</ogLmPpSeq>", xml or "")


def names_of(xml):
    return [re.sub(r"\s+", " ", n).strip()
            for n in re.findall(r"<lsNm>(.*?)</lsNm>", xml or "", re.S)]


def ask(label, query):
    url = "%s.xml?OC=%s&%s" % (REST, urllib.parse.quote(OC), query)
    xml = fetch(url)
    if xml is None:
        return []
    s = seqs_of(xml)
    print("    %-34s → %3d건  %s" % (label, len(s),
                                     "min %s max %s" % (min(s), max(s)) if s else ""))
    return s


def step1_paging():
    """문서에 없는 페이징 파라미터가 먹는지 본다. 1쪽과 번호가 달라지면 먹는 것이다."""
    print("\n" + "=" * 70)
    print("STEP 1. 페이징 파라미터가 있는가")
    print("=" * 70)
    base = ask("기준(diff=0)", "diff=0")
    if not base:
        return
    for name in ["page", "pageIndex", "pageNo", "currentPageNo", "pageUnit",
                 "display", "numOfRows", "rows", "pageSize"]:
        val = "100" if name in ("display", "numOfRows", "rows", "pageSize", "pageUnit") else "2"
        got = ask("diff=0&%s=%s" % (name, val), "diff=0&%s=%s" % (name, val))
        if got and (len(got) != len(base) or set(got) - set(base)):
            print("        *** '%s' 가 먹는다 ***" % name)
        time.sleep(0.3)


def step2_partition():
    """페이징이 없으면 필터로 쪼개야 한다. 법령종류별로 나눠 받으면 몇 건이 되나."""
    print("\n" + "=" * 70)
    print("STEP 2. 법령종류로 쪼개기")
    print("=" * 70)
    total = set()
    for code, name in LS_CLS:
        got = ask("lsClsCd=%s (%s)" % (code, name), "diff=0&lsClsCd=%s" % code)
        total |= set(got)
        time.sleep(0.3)
    print("    쪼개서 모은 합계: %d건 (중복 제거)" % len(total))


def step3_dates():
    """예고일자 구간으로도 쪼갤 수 있는지. 형식은 문서상 YYYY.MM.DD 다."""
    print("\n" + "=" * 70)
    print("STEP 3. 예고일자 구간으로 쪼개기")
    print("=" * 70)
    for st, ed in [("2026.7.1.", "2026.7.31."), ("2026.8.1.", "2026.8.31.")]:
        ask("stYdFmt=%s edYdFmt=%s" % (st, ed),
            "diff=0&stYdFmt=%s&edYdFmt=%s" % (urllib.parse.quote(st), urllib.parse.quote(ed)))
        time.sleep(0.3)


def step4_lsnm():
    """지정 법 검색이 API에서 먹는지. 화면 쪽은 행을 못 뽑았다."""
    print("\n" + "=" * 70)
    print("STEP 4. lsNm(예고명) 필터")
    print("=" * 70)
    for kw in ["도로교통", "자전거", "주차장", "자동차관리", "개인정보", "킥보드"]:
        got = ask("lsNm=%s (진행중)" % kw, "diff=0&lsNm=%s" % urllib.parse.quote(kw))
        if not got:
            got = ask("lsNm=%s (상태무관)" % kw, "lsNm=%s" % urllib.parse.quote(kw))
        if got:
            xml = fetch("%s.xml?OC=%s&lsNm=%s" % (REST, urllib.parse.quote(OC),
                                                  urllib.parse.quote(kw)))
            for n in names_of(xml)[:5]:
                print("          · %s" % n[:70])
        time.sleep(0.3)


def main():
    if not OC:
        print("LAWMAKING_OC 가 없다. 종료.")
        return 1
    step1_paging()
    step2_partition()
    step3_dates()
    step4_lsnm()
    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
