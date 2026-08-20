# -*- coding: utf-8 -*-
"""국회 입법예고 API 진단 스크립트.

무엇을 확인하는가:
  국민참여입법센터(정부 입법예고)와 달리, 국회 입법예고는 국회 Open API 쪽에
  있다. 엔드포인트는 nknalejkafmvgzmpt.

  정부 쪽에서 배운 것이 그대로 걸린다 — 의안명만으로는 PM 관련 개정을 못 잡는다.
  '개인형 이동장치'는 보통 본문(제안이유·주요내용)에만 나온다. 그래서 이 API가
  본문에 해당하는 필드를 주는지가 갈림길이다.
      준다  → 정부 쪽과 같은 방식(본문 키워드 판정)을 그대로 쓴다.
      안 준다 → 의안명 매칭 + 의안 상세를 따로 받아오는 경로가 필요하다.

  겸사겸사 요청 파라미터가 실제로 먹는지도 본다(국회 API는 문서에 적힌 필터가
  무시되는 일이 있다 — ALLSCHEDULE의 CMIT_NM이 그랬다).

성격: 진단 도구다. 상태 파일을 쓰지 않고 슬랙도 보내지 않는다. 손으로만 실행한다.
키는 ASSEMBLY_API_KEY 환경변수로 받고, 출력할 때 가린다(이 저장소는 퍼블릭이다).
"""
import json, os, re, sys, time, urllib.request, urllib.parse

KEY = os.environ.get("ASSEMBLY_API_KEY", "").strip()
ENDPOINT = "https://open.assembly.go.kr/portal/openapi/nknalejkafmvgzmpt"
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 20

KEYWORDS = ["개인형 이동장치", "개인형이동장치", "전동킥보드", "킥보드",
            "자전거", "전기자전거", "퍼스널 모빌리티", "퍼스널모빌리티"]


def redact(t):
    return t.replace(KEY, "***KEY***") if (KEY and t) else t


def fetch(url, tries=3):
    print("\n>>> GET %s" % redact(url))
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                print("    HTTP %s | %d bytes" % (resp.status, len(raw)))
                return raw
        except Exception as e:
            print("    시도 %d/%d 실패: %r" % (attempt, tries, e))
            if attempt < tries:
                time.sleep(2 * attempt)
    return None


def unwrap(raw):
    """국회 Open API의 [head, row] 껍데기를 벗긴다. (전체건수, 행목록)."""
    if not raw:
        return None, []
    try:
        data = json.loads(raw)
    except Exception:
        print("    JSON 아님 — 앞부분: %s" % redact(raw)[:400])
        return None, []
    if "RESULT" in data:
        print("    응답 코드: %s" % data["RESULT"])
        return None, []
    body = data.get("nknalejkafmvgzmpt")
    if not body:
        print("    예상한 키가 없다. 최상위 키: %s" % list(data))
        return None, []
    total = None
    try:
        total = body[0]["head"][0]["list_total_count"]
    except Exception:
        pass
    rows = []
    try:
        rows = body[1]["row"]
    except Exception:
        pass
    return total, rows


def step1_shape():
    """응답 필드를 있는 그대로 본다 — 본문에 해당하는 게 있는지가 핵심이다."""
    print("\n" + "=" * 70)
    print("STEP 1. 응답 필드 확인")
    print("=" * 70)
    total, rows = unwrap(fetch("%s?KEY=%s&Type=json&pIndex=1&pSize=5" % (ENDPOINT, KEY)))
    print("    전체 %s건, 이번에 받은 행 %d개" % (total, len(rows)))
    if not rows:
        return
    print("\n    필드 목록(%d개):" % len(rows[0]))
    for k, v in rows[0].items():
        v = "" if v is None else str(v)
        print("      %-22s %s%s" % (k, v[:90].replace("\n", " "), "…" if len(v) > 90 else ""))
    long_fields = [k for k, v in rows[0].items() if v and len(str(v)) > 200]
    print("\n    200자 넘는 필드(본문 후보): %s" % (long_fields or "(없음)"))

    print("\n    최근 5건:")
    for r in rows:
        print("      · %s | %s | %s" % (r.get("BILL_NO") or r.get("BILL_ID"),
                                        r.get("BILL_NAME"), r.get("PROPOSER")))


def step2_filters():
    """요청 파라미터가 실제로 먹는지. 안 먹으면 전부 받아서 우리가 걸러야 한다."""
    print("\n" + "=" * 70)
    print("STEP 2. 요청 파라미터가 먹는가")
    print("=" * 70)
    base = "%s?KEY=%s&Type=json&pIndex=1&pSize=5" % (ENDPOINT, KEY)
    trials = [("필터 없음", "")]
    for name in ["BILL_NAME", "COMMITTEE", "AGE"]:
        val = "개인형" if name == "BILL_NAME" else ("국토교통위원회" if name == "COMMITTEE" else "22")
        trials.append(("%s=%s" % (name, val), "&%s=%s" % (name, urllib.parse.quote(val))))
    for label, extra in trials:
        total, rows = unwrap(fetch(base + extra, tries=2))
        first = rows[0].get("BILL_NAME") if rows else "-"
        print("    %-26s → 전체 %s건, 첫 행 %s" % (label, total, first))
        time.sleep(0.3)


def step3_keyword_scan():
    """지금 걸려 있는 예고 중 관심 키워드가 있는지, 무엇으로 걸리는지 본다."""
    print("\n" + "=" * 70)
    print("STEP 3. 관심 키워드 훑기(받아서 우리가 판정)")
    print("=" * 70)
    total, rows = unwrap(fetch("%s?KEY=%s&Type=json&pIndex=1&pSize=100" % (ENDPOINT, KEY)))
    print("    전체 %s건 중 %d건을 받았다" % (total, len(rows)))
    hits = 0
    for r in rows:
        blob = " ".join(str(v) for v in r.values() if v)
        found = [k for k in KEYWORDS if k in blob]
        if not found:
            continue
        hits += 1
        where = [k for k, v in r.items() if v and any(f in str(v) for f in found)]
        print("      · %s | %s\n        적중 %s (필드: %s)"
              % (r.get("BILL_NO") or r.get("BILL_ID"), r.get("BILL_NAME"),
                 ", ".join(found), ", ".join(where)))
    print("    적중 %d건" % hits)


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 가 없다. 종료.")
        return 1
    step1_shape()
    step2_filters()
    step3_keyword_scan()
    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
