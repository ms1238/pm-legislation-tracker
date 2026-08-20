# -*- coding: utf-8 -*-
"""국회 의안 제안이유·주요내용을 어디서 받는지 확인하는 진단 스크립트.

왜 필요한가:
  국회 입법예고 목록(nknalejkafmvgzmpt)에는 본문이 없어서 의안명으로만 거른다.
  그 결과 "도로교통법 일부개정법률안" 20건이 통째로 걸리는데, 대부분은 음주운전·
  신호위반처럼 PM과 무관하다. 제안이유를 읽을 수 있으면 정부 쪽과 같은 정밀도가
  되는데, BPMBILLSUMMARY 를 BILL_ID 로 부르니 전부 "데이터 없음"이 왔다.

  그래서 (1) 그 엔드포인트가 실제로 무엇을 받는지, (2) 필터 이름이 무엇인지,
  (3) 안 되면 대체 엔드포인트가 있는지를 실측한다.

성격: 진단 도구다. 상태 파일을 쓰지 않고 슬랙도 보내지 않는다. 손으로만 실행한다.
키는 ASSEMBLY_API_KEY 환경변수로 받고, 출력할 때 가린다(이 저장소는 퍼블릭이다).
"""
import json, os, sys, time, urllib.request, urllib.parse

KEY = os.environ.get("ASSEMBLY_API_KEY", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 20
PORTAL = "https://open.assembly.go.kr/portal/openapi/%s"

# 앞선 실행에서 실제로 걸린 도로교통법 개정안 하나. 이 의안의 제안이유를 찾는 게 목표다.
SAMPLE_BILL_ID = "PRC_S2R6L0K5J1S9P0R8Q5Z9Y0X8D5C0B9"
SAMPLE_BILL_NO = "2219720"


def redact(t):
    return t.replace(KEY, "***KEY***") if (KEY and t) else t


def ask(label, endpoint, extra=""):
    url = PORTAL % endpoint + "?KEY=%s&Type=json&pIndex=1&pSize=3%s" % (KEY, extra)
    print("\n>>> %s" % label)
    print("    %s" % redact(url))
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:
        print("    실패: %r" % (e,))
        return
    try:
        data = json.loads(raw)
    except Exception:
        print("    JSON 아님: %s" % redact(raw)[:300])
        return
    if "RESULT" in data:
        print("    %s" % data["RESULT"])
        return
    body = data.get(endpoint)
    if not body:
        print("    최상위 키: %s" % list(data))
        return
    try:
        total = body[0]["head"][0]["list_total_count"]
    except Exception:
        total = "?"
    try:
        rows = body[1]["row"]
    except Exception:
        rows = []
    print("    전체 %s건, 행 %d개" % (total, len(rows)))
    if not rows:
        return
    for k, v in rows[0].items():
        v = "" if v is None else str(v)
        print("      %-20s (%4d자) %s" % (k, len(v), v[:80].replace("\n", " ")))


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 가 없다. 종료.")
        return 1

    bid = urllib.parse.quote(SAMPLE_BILL_ID)
    ask("BPMBILLSUMMARY — 필터 없이(엔드포인트가 살아 있는지)", "BPMBILLSUMMARY")
    ask("BPMBILLSUMMARY — BILL_ID", "BPMBILLSUMMARY", "&BILL_ID=%s" % bid)
    ask("BPMBILLSUMMARY — BILL_NO", "BPMBILLSUMMARY", "&BILL_NO=%s" % SAMPLE_BILL_NO)
    time.sleep(0.3)

    # 대체 후보들. 의안 본문·제안이유를 주는 다른 이름이 있는지 본다.
    for ep in ["BILLINFOSUMMARY", "nzmimeepazxkubdpn", "TVBPMBILL11", "ALLBILL"]:
        ask("%s — BILL_NO" % ep, ep, "&BILL_NO=%s" % SAMPLE_BILL_NO)
        time.sleep(0.3)

    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
