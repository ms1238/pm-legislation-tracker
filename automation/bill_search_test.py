# -*- coding: utf-8 -*-
"""가짜 국회 API 로 신규 의안 탐지 로직을 검증한다.

만든 이유: 이 로직을 바꾼 2026-09-16 에 open.assembly.go.kr 이 응답하지 않아
(두 번의 진단 실행에서 16회 전부 타임아웃) 실물로 확인할 방법이 없었다.
네트워크를 쓰지 않으므로 아무 데서나 돌아간다.

    python automation/bill_search_test.py

확인하는 것:
  - 목록 쪽 순서가 최신순이든 역순이든 같은 결과를 내는가
  - 응답에 제안일이 없으면 다음 엔드포인트로 넘어가는가
  - 첫 엔드포인트가 죽어도 두 번째로 넘어가는가
  - 지정 법 개정안이라도 제안이유에 PM 얘기가 없으면 거르는가
  - skip(이미 거른 것) 목록과 seed(사람이 찍은 의안)가 먹는가
"""
import sys, os, json, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_check_updates as m

BILLS = []
def mk(no, name, date, proposer="홍길동의원 등 10인", cm="행정안전위원회"):
    return {"BILL_NO": no, "BILL_ID": "PRC_" + no, "BILL_NAME": name,
            "PROPOSE_DT": date, "PROPOSER": proposer, "COMMITTEE": cm}

# 최신순으로 만든다 (2026-09-15 가 가장 최근)
BILLS = [
    mk("2221403", "도로교통법 일부개정법률안", "2026-09-10"),
    mk("2221402", "도로교통법 일부개정법률안", "2026-09-10"),   # 음주운전 — PM 무관
    mk("2221300", "자전거 이용 활성화에 관한 법률 일부개정법률안", "2026-09-01"),
    mk("2221200", "소득세법 일부개정법률안", "2026-08-25"),      # 지정 법 아님
    mk("2219714", "도로교통법 일부개정법률안", "2026-07-02"),    # 이미 추적 중
    mk("2210000", "도로교통법 일부개정법률안", "2026-01-02"),    # 기간 밖
]
SUMMARY = {
    "2221403": "현행법은 개인형 이동장치의 보도 통행을 금지하고 있으나 단속이 어려운 실정이다. 이에 전동킥보드 주차구역 지정 근거를 마련하려는 것임.",
    "2221402": "음주운전 처벌을 강화하고 상습 위반자의 면허 취소 기준을 정비하려는 것임.",
    "2221300": "자전거등의 통행방법을 정비하고 공유 자전거 대여사업 신고 근거를 마련하려는 것임.",
}

MODE = {"order": "newest_first", "dates": True, "endpoint_ok": "nzmimeepazxkubdpn",
        "max_psize": 1000, "name_filter": "none"}
CALLS = []

def fake_api_get(url):
    CALLS.append(url)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    ep = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    if ep == "BPMBILLSUMMARY":
        no = q["BILL_NO"][0]
        if no not in SUMMARY:
            return {"RESULT": {"CODE": "INFO-200"}}
        return {"BPMBILLSUMMARY": [{}, {"row": [{"SUMMARY": SUMMARY[no]}]}]}
    if ep == "ALLBILL":
        no = q["BILL_NO"][0]
        row = next((b for b in BILLS if b["BILL_NO"] == no), None)
        if row is None:
            return {"RESULT": {"CODE": "INFO-200"}}
        r = dict(row); r["JRCMIT_CMMT_DT"] = "2026-09-11"; r["BILL_NM"] = row["BILL_NAME"]
        return {"ALLBILL": [{}, {"row": [r]}]}
    if ep == "VCONFBILLCONFLIST":
        return {"RESULT": {"CODE": "INFO-200"}}
    if ep in ("nzmimeepazxkubdpn", "TVBPMBILL11"):
        if q.get("BILL_NAME"):
            if MODE["name_filter"] == "none":         # 부분일치가 안 되는 경우
                return {"RESULT": {"CODE": "INFO-200"}}
            want = q["BILL_NAME"][0]
            hit = [b for b in BILLS if want in b["BILL_NAME"]]
            if MODE["name_filter"] == "partial_nodate":
                hit = [{k: v for k, v in b.items() if k != "PROPOSE_DT"} for b in hit]
            if not hit:
                return {"RESULT": {"CODE": "INFO-200"}}
            return {ep: [{"head": [{"list_total_count": len(hit)}]}, {"row": hit}]}
        if ep != MODE["endpoint_ok"]:
            return {"RESULT": {"CODE": "INFO-200"}}
        if int(q["pSize"][0]) > MODE["max_psize"]:   # 큰 쪽은 거부하는 날이 있다
            return {"RESULT": {"CODE": "INFO-300", "MESSAGE": "pSize 가 너무 큽니다"}}
        rows = list(BILLS)
        if MODE["order"] == "oldest_first":
            rows = rows[::-1]
        if not MODE["dates"]:
            rows = [{k: v for k, v in r.items() if k != "PROPOSE_DT"} for r in rows]
        psize, pindex = int(q["pSize"][0]), int(q["pIndex"][0])
        page = rows[(pindex - 1) * psize: pindex * psize]
        return {ep: [{"head": [{"list_total_count": len(rows)}]}, {"row": page}]}
    raise AssertionError("예상 못 한 호출: " + url)

CALLS_LOG = []
_log = m.log
def spy_log(msg):
    CALLS_LOG.append(msg)
    _log(msg)
m.log = spy_log
m.api_get = fake_api_get
m.time.sleep = lambda *_: None
m.BILL_SWEEP_PAGE_SIZES = [2]      # 쪽이 여러 개가 되도록 작게
m.BILL_SWEEP_MAX_PAGES = 10

KNOWN = ["2219714"]

def run(label):
    CALLS[:] = []
    CALLS_LOG[:] = []
    found, rejected = m.search_new_bills("KEY", known=KNOWN, skip=[])
    print("\n--- %s ---" % label)
    for no, f in sorted(found.items()):
        print("  적중 %s | %s | %s | %s" % (no, f["name"][:20], f["tier"], f["why"]))
        if f["excerpt"]:
            print("        > %s" % f["excerpt"][:80])
    print("  거름:", rejected)
    return found, rejected

f1, r1 = run("최신이 앞쪽")
assert set(f1) == {"2221403", "2221300"}, f1
assert r1 == ["2221402"], r1
assert f1["2221403"]["tier"] == "direct"
assert f1["2221300"]["tier"] == "near"

MODE["order"] = "oldest_first"
f2, r2 = run("최신이 뒤쪽 (순서 반대)")
assert set(f2) == set(f1), (f2, f1)

MODE["order"] = "newest_first"
MODE["dates"] = False
f3, r3 = run("제안일 필드가 없을 때")
assert f3 == {} and r3 == [], (f3, r3)
MODE["dates"] = True

MODE["endpoint_ok"] = "TVBPMBILL11"
f4, r4 = run("첫 엔드포인트가 죽었을 때 (두 번째로 넘어감)")
assert set(f4) == set(f1), f4

MODE["endpoint_ok"] = "nzmimeepazxkubdpn"
MODE["max_psize"] = 2
m.BILL_SWEEP_PAGE_SIZES = [1000, 2]     # 큰 쪽이 거부당하면 작은 쪽으로 물러선다
f5, r5 = run("큰 쪽 크기를 거부당했을 때")
assert set(f5) == set(f1), f5
m.BILL_SWEEP_PAGE_SIZES = [2]
MODE["max_psize"] = 1000

# 이름 검색이 되는 경우 — 훑기 없이 이름으로 끝나야 한다
MODE["name_filter"] = "partial"
m.BILL_NAME_PAGE_SIZE = 2
f6, r6 = run("이름 검색이 부분일치로 동작할 때")
assert set(f6) == set(f1), f6
assert not any("훑기" in c for c in CALLS_LOG), CALLS_LOG

# 이름 검색은 되는데 제안일이 없는 경우 — 훑기로 물러서야 한다
MODE["name_filter"] = "partial_nodate"
f7, r7 = run("이름 검색에 제안일이 없을 때 (훑기로 물러섬)")
assert set(f7) == set(f1), f7
MODE["name_filter"] = "none"

# skip 목록이 먹는지
found5, rej5 = m.search_new_bills("KEY", known=KNOWN, skip=["2221402"])
assert rej5 == [], rej5
assert "2221402" not in found5

# 지정 의안(seed)
out = {}
resolved = m.resolve_seed_bills("KEY", ["2221403"], set(), out)
print("\n--- seed ---")
print("  resolved:", resolved, "| 받아온 것:", {k: (v["name"], v["tier"]) for k, v in out.items()})
assert resolved == ["2221403"] and out["2221403"]["tier"] == "seed"
assert out["2221403"]["proposer"] == "홍길동의원 등 10인"

resolved_missing = m.resolve_seed_bills("KEY", ["9999999"], set(), {})
assert resolved_missing == [], resolved_missing   # 못 받으면 seeds 에 남는다

print("\n전부 통과")
