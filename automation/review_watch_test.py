# -*- coding: utf-8 -*-
"""가짜 HTML 로 검토보고서 파싱을 검증한다. 네트워크를 쓰지 않는다.

    python automation/review_watch_test.py

의안정보시스템 페이지 구조는 우리 것이 아니라 언제든 바뀐다. 바뀌어서 링크를 못
뽑게 되면 이 검증이 먼저 울어야 한다 — 실제로 깨졌을 때 조용히 '검토보고서 0건'을
보고하는 게 제일 나쁜 결과다.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_watch as rw

# 1) 평범한 표 한 행 — 파일 서버 링크에 문서 이름이 붙어 있다.
PLAIN = """
<table class="tbl"><tbody>
  <tr>
    <td>검토보고서</td>
    <td><a href="/filegate/servlet/FileGate?bookId=ABC-123&type=1">도로교통법 일부개정법률안 검토보고서.pdf</a></td>
    <td>2026-09-10</td>
  </tr>
</tbody></table>
"""

# 2) 링크가 자바스크립트 호출로 들어 있는 꼴.
JSCALL = """
<div class="doc">
  <span>비용추계서</span>
  <a href="#" onclick="openBillFile('/filegate/servlet/FileGate?bookId=XYZ-9','비용추계서');return false;">내려받기</a>
</div>
"""

# 3) 아직 문서가 없는 의안 — 말 자체가 안 나온다.
EMPTY = "<html><body><h2>의안원문</h2><p>제안이유 및 주요내용 …</p></body></html>"

# 4) 말은 있는데 링크가 없다 = 파싱이 깨졌거나 구조가 바뀐 것. '없음'으로 보면 안 된다.
BROKEN = "<html><body><ul><li>검토보고서</li></ul><a href='/bill/billList.do'>목록</a></body></html>"


def one(html, label):
    docs, failed = rw.parse_documents(html)
    print("\n--- %s ---" % label)
    print("  파싱실패=%s, 문서 %d건" % (failed, len(docs)))
    for d in docs:
        print("   %s | %s" % (d["term"], d["url"]))
    return docs, failed

docs, failed = one(PLAIN, "표 한 행")
assert not failed and len(docs) == 1, (docs, failed)
assert docs[0]["url"] == "https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=ABC-123&type=1", docs
assert "검토보고서" in docs[0]["title"], docs

docs, failed = one(JSCALL, "자바스크립트 호출")
assert not failed and len(docs) == 1, (docs, failed)
assert docs[0]["url"].endswith("bookId=XYZ-9"), docs

docs, failed = one(EMPTY, "아직 문서 없음")
assert docs == [] and failed is False, (docs, failed)

docs, failed = one(BROKEN, "말은 있는데 링크가 없다")
assert docs == [] and failed is True, (docs, failed)

# --- check_reports ---
SNAP = {"bills": {
    "2221403": {"name": "도로교통법 일부개정법률안", "stage": "소관위 심사중", "bill_id": "PRC_A"},
    "2207879": {"name": "도로교통법 일부개정법률안", "stage": "본회의 대안반영폐기 · 2025-03-13", "bill_id": "PRC_B"},
    "2219714": {"name": "도로교통법 일부개정법률안", "stage": "소관위 접수", "bill_id": "PRC_C"},
}}
PAGES = {"PRC_A": PLAIN, "PRC_C": EMPTY}
logs = []

def fake_fetch(bill_id):
    return PAGES.get(bill_id)     # 없는 것은 None = 못 읽음

changes, checked, unread = rw.check_reports(SNAP, fetch=fake_fetch, log=logs.append)
print("\n--- check_reports (처음) ---")
print("  본 의안 %d건, 새 문서 %d건, 못 읽음 %s" % (checked, len(changes), unread))
for c in changes:
    print("   [%s] %s" % (c["bill_no"], c["url"]))
assert checked == 2, checked                     # 폐기된 2207879 은 건너뛴다
assert len(changes) == 1 and changes[0]["bill_no"] == "2221403", changes
assert unread == [], unread

changes2, _, _ = rw.check_reports(SNAP, fetch=fake_fetch, log=logs.append)
print("\n--- check_reports (다시) ---")
print("  새 문서 %d건 (같은 문서를 두 번 알리지 않는다)" % len(changes2))
assert changes2 == [], changes2

# 파싱이 깨진 페이지는 '없음'이 아니라 '못 읽음'으로 들어가고 로그를 남긴다
SNAP2 = {"bills": {"2221403": {"name": "x", "stage": "소관위 심사중", "bill_id": "PRC_A"}}}
logs2 = []
ch, _, un = rw.check_reports(SNAP2, fetch=lambda b: BROKEN, log=logs2.append)
print("\n--- 파싱이 깨졌을 때 ---")
print("  changes=%d, 못 읽음=%s" % (len(ch), un))
print("  경고: %s" % (logs2[0] if logs2 else "(없음)"))
assert ch == [] and un == ["2221403"], (ch, un)
assert logs2 and "못 뽑았다" in logs2[0], logs2

# 페이지를 아예 못 받은 경우도 '없음'이 아니다
ch, _, un = rw.check_reports({"bills": {"1": {"stage": "소관위 심사중", "bill_id": "Z"}}},
                             fetch=lambda b: None, log=logs2.append)
assert ch == [] and un == ["1"], (ch, un)

print("\n전부 통과")
