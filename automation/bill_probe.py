# -*- coding: utf-8 -*-
"""신규 의안 탐지 진단용 스크립트.

"도로교통법 개정안이 발의됐는데 트래커가 왜 못 봤나"를 가리려고 만들었다.
상태 파일도 쓰지 않고 슬랙도 보내지 않는다 — 읽고 출력만 한다.
손으로만 실행한다(schedule-debug.yml 의 bills 모드).

세 가지를 확인한다.
  1) TVBPMBILL11 의 BILL_NAME 필터가 부분일치인가 — 지금 신규 탐지는 이걸 전제로 한다.
     (국회 입법예고 API 는 부분일치가 안 된다는 게 실측으로 확인돼 있다. 같은 성질이면
      '개인형 이동' 검색은 늘 0건이고, 신규 탐지는 처음부터 동작한 적이 없다는 뜻이다.)
  2) 최근 발의된 도로교통법 개정안이 무엇인가, 그 중 snapshot 에 없는 건 무엇인가.
  3) 그 놓친 의안의 제안이유·주요내용에 PM 관심어가 있는가 — 즉 "봤어야 할 건"인가.
"""
import json, os, sys, time, urllib.parse, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(BASE_DIR, "snapshot.json")

KEY = os.environ.get("ASSEMBLY_API_KEY", "").strip()
AGE = "22"
PM_TERMS = ["개인형 이동", "개인형이동", "전동킥보드", "킥보드", "퍼스널 모빌리티",
            "퍼스널모빌리티", "자전거등", "전동이륜평행차", "개인형 이동장치"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rows_of(data, name):
    """열린국회정보 응답에서 row 목록과 총건수를 꺼낸다. 없으면 (None, 사유)."""
    if "RESULT" in data:
        r = data["RESULT"]
        return None, "%s %s" % (r.get("CODE"), r.get("MESSAGE"))
    try:
        head = data[name][0]["head"]
        total = head[0]["list_total_count"]
        return data[name][1]["row"], total
    except Exception:
        return None, "응답 구조를 못 읽음: %s" % json.dumps(data, ensure_ascii=False)[:200]


def probe_filter(endpoint, bill_name):
    url = ("https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=1&pSize=5&AGE=%s"
           % (endpoint, KEY, AGE))
    if bill_name:
        url += "&BILL_NAME=" + urllib.parse.quote(bill_name)
    try:
        data = get(url)
    except Exception as e:
        print("   %-14s → 호출 실패: %s" % (bill_name or "(필터없음)", e))
        return None
    rows, total = rows_of(data, endpoint)
    if rows is None:
        print("   %-14s → %s" % (bill_name or "(필터없음)", total))
        return []
    print("   %-14s → 총 %s건. 예: %s" % (bill_name or "(필터없음)", total,
                                        " / ".join(r.get("BILL_NAME", "?") for r in rows[:3])))
    return rows


def print_summaries(bill_nos):
    """의안 제안이유·주요내용 전문을 찍는다. 페이지에 올릴 한 줄 요약을 지어내지
    않고 원문을 보고 쓰려고 만들었다."""
    for no in bill_nos:
        url = ("https://open.assembly.go.kr/portal/openapi/BPMBILLSUMMARY?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_NO=%s"
               % (KEY, urllib.parse.quote(str(no))))
        try:
            data = get(url)
        except Exception as e:
            print("[%s] 호출 실패: %s" % (no, e))
            continue
        rows, why = rows_of(data, "BPMBILLSUMMARY")
        if rows is None:
            print("[%s] 요약 없음 (%s)" % (no, why))
            continue
        text = " ".join(str(r.get("SUMMARY") or "") for r in rows)
        print("\n[%s] %d자\n%s" % (no, len(text), " ".join(text.split())))
        time.sleep(0.4)
    return 0


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--summary":
        if not KEY:
            print("ASSEMBLY_API_KEY 환경변수가 없다")
            return 1
        return print_summaries(sys.argv[2].split(","))
    if not KEY:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1

    print("=== 1. BILL_NAME 필터가 부분일치인가 (TVBPMBILL11) ===")
    print("   지금 신규 탐지가 쓰는 검색어는 '개인형 이동', '퍼스널모빌리티', '전동킥보드', '킥라니' 다.")
    for kw in ["", "개인형 이동", "개인형", "도로교통법", "도로교통법 일부개정법률안", "전동킥보드"]:
        probe_filter("TVBPMBILL11", kw)
        time.sleep(0.4)

    print("\n=== 2. 최근 발의된 도로교통법 개정안 ===")
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        snapshot = json.load(f)
    known = set(str(b) for b in snapshot["known_bill_nos"])

    # 의원 발의 법률안 목록. 제안일자가 있어서 '최근 발의'를 볼 수 있다.
    candidates = []
    for endpoint in ["nzmimeepazxkubdpn", "TVBPMBILL11"]:
        url = ("https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=1&pSize=100&AGE=%s&BILL_NAME=%s"
               % (endpoint, KEY, AGE, urllib.parse.quote("도로교통법")))
        try:
            data = get(url)
        except Exception as e:
            print("   %s 호출 실패: %s" % (endpoint, e))
            continue
        rows, total = rows_of(data, endpoint)
        if rows is None:
            print("   %s → %s" % (endpoint, total))
            continue
        print("   %s → 총 %s건 (받은 건 %d건). 응답 필드: %s"
              % (endpoint, total, len(rows), ", ".join(sorted(rows[0].keys()))))
        candidates = rows
        break

    if not candidates:
        print("   도로교통법 목록을 못 받았다 — 아래 단계는 건너뛴다.")
        return 0

    def dt(r):
        return r.get("PROPOSE_DT") or r.get("PPSL_DT") or ""
    candidates.sort(key=dt, reverse=True)
    print("\n   최근 제안 20건 (★ = snapshot 에 없는 것):")
    missing = []
    for r in candidates[:20]:
        no = str(r.get("BILL_NO") or r.get("BILL_NO_"))
        mark = "  " if no in known else "★ "
        print("   %s%s %s | %s | %s" % (mark, no, dt(r), r.get("PROPOSER", "")[:24],
                                        (r.get("BILL_NAME") or "")[:30]))
        if no not in known:
            missing.append(r)

    print("\n=== 3. snapshot 에 없는 건의 제안이유에 PM 관심어가 있나 ===")
    if not missing:
        print("   최근 20건 중 빠진 건 없다.")
    for r in missing[:12]:
        no = str(r.get("BILL_NO"))
        url = ("https://open.assembly.go.kr/portal/openapi/BPMBILLSUMMARY?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_NO=%s"
               % (KEY, urllib.parse.quote(no)))
        try:
            data = get(url)
        except Exception as e:
            print("   %s → 요약 호출 실패: %s" % (no, e))
            continue
        rows, why = rows_of(data, "BPMBILLSUMMARY")
        if rows is None:
            print("   %s → 요약 없음 (%s)" % (no, why))
            continue
        text = " ".join(str(x.get("SUMMARY") or "") for x in rows)
        hits = [t for t in PM_TERMS if t in text]
        if hits:
            i = text.find(hits[0])
            print("   %s → ★PM 관련★ %s | …%s…" % (no, ", ".join(hits),
                                                   text[max(0, i - 60):i + 100].replace("\n", " ")))
        else:
            print("   %s → PM 관심어 없음 | %s…" % (no, text[:90].replace("\n", " ")))
        time.sleep(0.4)

    print("\n=== 4. 지금 탐지 로직이 무엇을 알릴지 (실제 호출, 저장·알림 없음) ===")
    sys.path.insert(0, BASE_DIR)
    import cloud_check_updates as m
    try:
        found, rejected = m.search_new_bills(KEY, known=snapshot["known_bill_nos"],
                                             skip=snapshot.get("not_pm_bill_nos", []))
        m.resolve_seed_bills(KEY, snapshot.get("seed_bill_nos", []),
                             set(str(k) for k in snapshot["known_bill_nos"]), found)
    except SystemExit:
        # 연결이 아예 안 되면 탐지 쪽이 그 자리에서 끝낸다(FAILFAST). 진단은 거기서
        # 죽지 않고 그렇게 됐다는 사실만 남긴다.
        print("   국회 API 연결이 안 돼 탐지가 즉시 종료됐다(FAILFAST).")
        return 0
    if not found:
        print("   알릴 것 없음 (새로 걸린 의안 0건)")
    for no, f in sorted(found.items()):
        print("   [%s] %s | %s %s\n        %s" % (no, f["name"], f.get("proposer", ""),
                                                  f.get("date", ""), f.get("why", "")))
        if f.get("excerpt"):
            print("        > %s" % f["excerpt"])
    print("   제안이유에 PM 얘기가 없어 거른 것 %d건" % len(rejected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
