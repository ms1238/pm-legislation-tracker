# -*- coding: utf-8 -*-
"""가짜 응답으로 회의록 감시를 검증한다. 네트워크를 쓰지 않는다.

    python automation/meeting_watch_test.py

지키려는 것:
  1. 회의 기록이 등록된 날과 회의록 PDF 가 올라오는 날은 다르다. 등록만 된 회의는
     기억해 두었다가 PDF 가 올라온 실행에서 링크와 함께 한 번 알린다.
  2. 한 회의에 추적 의안이 여러 건 걸려도 알림은 한 덩어리다.
  3. 소위·국감 회의록 목록은 회의 날짜순이지 올라온 순서가 아니다. 날짜가 지난
     회의록이 뒤늦게 올라와도, PDF 를 한 번 못 읽어도 다음 실행에서 잡는다.
  4. 위원회·소위 일정 API 는 회의 전에 안건(ANGUN)을 준다. 앞으로 열릴 회의의 안건에
     추적 의안이나 PM 법안이 있으면 회의 전에 알리고, 같은 안건을 두 번 알리지 않는다.
  5. 슬랙은 한 덩어리에 3000자까지만 받는다. 길어지면 나눠 보낸다(통째로 거절당하면
     알림이 사라진다).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_check_updates as m

PDF_URL = "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=%s"
NOT_YET = "<script type='text/javascript'>alert('내려받을 원본 회의록 파일이 등록되지 않았습니다.'); window.close();</script>".encode("utf-8")
TEXT = "◯소위원장 정동만  의사일정 제25항\n◯복기왕 위원  개인형 이동장치 주차 문제는 지자체가 견인하고 있는데"

logs = []
m.log = logs.append
m.pdf_text = lambda data: TEXT           # pypdf 없이 돈다
m.time.sleep = lambda s: None


def conf_row(bill_id, conf_id, date, pdf_id, dgr="제1차"):
    return {"BILL_ID": bill_id, "BILL_NM": "25. 법률안", "CONF_KND": "상임위원회 회의록", "CONF_ID": conf_id,
            "ERACO": "제22대", "SESS": "제439회", "DGR": dgr, "CONF_DT": date + "        ", "DOWN_URL": PDF_URL % pdf_id}


SCHEDULE = [
    {"SCH_KIND": "위원회", "SCH_DT": "2026-11-11", "CMIT_NM": "국토교통위원회", "CONF_DIV": "국토법안심사소위원회",
     "CONF_SESS": "제439회국회(정기회)", "CONF_DGR": "제2차"},
    {"SCH_KIND": "위원회", "SCH_DT": "2026-11-11", "CMIT_NM": "국토교통위원회", "CONF_DIV": "교통법안심사소위원회",
     "CONF_SESS": "제439회국회(정기회)", "CONF_DGR": "제1차"},
]
by_bill = {}          # BILL_ID → 의안별 회의 목록
listing = {}          # 엔드포인트 → 회의록 목록
published = set()     # PDF 가 올라온 회의록 번호
broken = set()        # 받다가 끊기는 회의록 번호


def fake_api_get(url):
    if "VCONFBILLCONFLIST" in url:
        return {"VCONFBILLCONFLIST": [{}, {"row": by_bill[url.split("BILL_ID=")[1]]}]}
    if "ALLSCHEDULE" in url:
        return {"ALLSCHEDULE": [{}, {"row": [r for r in SCHEDULE if r["SCH_DT"] in url]}]}
    for ep, rows in listing.items():
        if ep in url:
            return {ep: [{}, {"row": rows}]}
    raise AssertionError(url)


def fake_download(url):
    pdf_id = url.split("id=")[1]
    if pdf_id in broken:
        raise IOError("connection reset")
    return b"%PDF-1.7 ..." if pdf_id in published else NOT_YET


m.api_get = fake_api_get
m.download = fake_download
m.now_kst = lambda: m.datetime(2026, 11, 12, 17, 0, tzinfo=m.KST)

# ---- 1. 등록만 된 회의 → PDF 가 올라오면 링크와 함께 알린다 ----
by_bill.update({"PRC_A": [conf_row("PRC_A", "N054233", "20260429", "56617", "제3차")],
                "PRC_B": [conf_row("PRC_B", "N054233", "20260429", "56617", "제3차")]})
snap = {"bills": {"DD20688": {"name": "개인형 이동장치의 안전 및 관리에 관한 법률안(대안)", "bill_id": "PRC_A", "seen_conf_ids": ["N054233"]},
                  "2216647": {"name": "개인형 이동장치의 안전 및 이용 활성화에 관한 법률안", "bill_id": "PRC_B", "seen_conf_ids": ["N054233"]}}}
published.add("56617")

by_bill["PRC_A"].append(conf_row("PRC_A", "N054769", "20261111", "57730"))
by_bill["PRC_B"].append(conf_row("PRC_B", "N054769", "20261111", "57730"))
ch = m.check_bill_meetings("KEY", snap)
print("--- 회의 기록이 새로 등록된 실행 ---")
print("  변경 %d건, 기다리는 회의록 %s" % (len(ch), sorted(snap["pending_minutes"])))
assert [c["type"] for c in ch] == ["bill_new_meeting"] * 2, ch
assert ch[0]["down_url"] == PDF_URL % "57730", ch[0]
assert ch[0]["where"] == "국토교통위원회 교통법안심사소위원회", ch[0]   # 그날 일정에서 찾은 회의 이름
assert sorted(snap["pending_minutes"]) == ["N054769"], snap["pending_minutes"]   # 오래전 회의는 기다리지 않는다
assert m.check_pending_minutes(snap) == []
assert "N054769" in snap["pending_minutes"]

block = "\n".join(m.change_lines(ch))
print(block)
assert block.count("회의 기록 등록") == 1, block                  # 의안 2건이어도 한 덩어리
assert "교통법안심사소위원회" in block and "2026-11-11" in block and "추적 의안 2건" in block, block
assert "회의록은 아직" in block and "pdf.do" not in block, block   # 안 열리는 링크는 주지 않는다

assert m.check_bill_meetings("KEY", snap) == []                    # 같은 회의를 두 번 알리지 않는다
published.add("57730")
ch = m.check_pending_minutes(snap)
print("--- 회의록 PDF 가 올라온 실행 ---")
assert len(ch) == 1 and ch[0]["type"] == "minutes_published", ch
assert ch[0]["bills"] == ["DD20688", "2216647"] and ch[0]["speakers"] == ["복기왕"], ch[0]
assert snap["pending_minutes"] == {}, snap["pending_minutes"]
block = "\n".join(m.change_lines(ch))
print(block)
assert "회의록 공개" in block and "<%s|" % (PDF_URL % "57730") in block and "교통법안심사소위원회" in block, block
assert m.check_pending_minutes(snap) == []                         # 한 번만 알린다

# 이 기능이 생기기 전에 이미 '등록'으로 알려 버린 최근 회의도 기다린다 (2026-11-11 교통소위가 그랬다)
old = {"bills": {"DD20688": {"name": "x", "bill_id": "PRC_A", "seen_conf_ids": ["N054233", "N054769"]}}}
published.discard("57730")
assert m.check_bill_meetings("KEY", old) == []
assert sorted(old["pending_minutes"]) == ["N054769"], old["pending_minutes"]

# 회의록 서버가 끊긴 실행은 '아직'도 '공개'도 아니다 — 남겨 두고 다음에 다시 본다
broken.add("57730")
assert m.check_pending_minutes(old) == [] and "N054769" in old["pending_minutes"]
broken.clear()

# ---- 2. 소위·국감 회의록 목록: 뒤늦게 올라온 것, 한 번 못 읽은 것 ----
def sub_row(conf_id, date, pdf_id):
    return {"CONF_ID": conf_id, "CONF_DT": date, "CMIT_NM": "국토교통위원회", "SB_CMIT_NM": "교통법안심사소위원회",
            "DOWN_URL": PDF_URL % pdf_id}

listing.update({"VCONFSUBCCONFLIST": [sub_row("N054260", "2026-05-12", "56691")],
                "VCONFAPIGCONFLIST": [sub_row("N054700", "2026-10-30", "57600")]})
published.update({"56691", "57600", "57001", "57730"})
scan = {"last_meeting_scan": "2026-10-30"}
ch, newest = m.check_new_meetings("KEY", scan)
assert ch == [] and newest == "2026-10-30", (ch, newest)           # 처음 켠 실행은 예전 것을 쏟아내지 않는다

listing["VCONFSUBCCONFLIST"].insert(0, sub_row("N054500", "2026-09-10", "57001"))   # 날짜는 지났는데 이제 올라왔다
listing["VCONFSUBCCONFLIST"].insert(0, sub_row("N054769", "2026-11-11", "57730"))
broken.add("57730")
scan["last_meeting_scan"] = newest
ch, newest = m.check_new_meetings("KEY", scan)
print("--- 소위 회의록 목록 ---")
print("  뒤늦게 올라온 회의록: %s" % [(c["conf_id"], c["date"]) for c in ch])
assert [c["conf_id"] for c in ch] == ["N054500"], ch
assert ch[0]["down_url"] == PDF_URL % "57001", ch[0]
assert "<%s|" % (PDF_URL % "57001") in "\n".join(m.change_lines(ch))

broken.clear()
scan["last_meeting_scan"] = newest
ch, newest = m.check_new_meetings("KEY", scan)
print("  한 번 못 읽은 회의록: %s" % [(c["conf_id"], c["date"]) for c in ch])
assert [c["conf_id"] for c in ch] == ["N054769"], ch
scan["last_meeting_scan"] = newest
assert m.check_new_meetings("KEY", scan)[0] == []

# ---- 3. 일정 알림: PM 법안을 심사하는 소위는 눈에 띄게 ----
lines = m.change_lines([{"type": "new_schedule", "date": "2026-11-11", "committee": "국토교통위원회",
                         "content": "제439회국회(정기회) 제1차 교통법안심사소위원회", "sess": "제439회국회(정기회)"},
                        {"type": "new_schedule", "date": "2026-11-11", "committee": "국토교통위원회",
                         "content": "제439회국회(정기회) 제2차 국토법안심사소위원회", "sess": "제439회국회(정기회)"}])
print("--- 일정 ---")
print("\n".join(lines))
assert "PM 법안 심사 소위" in lines[0] and "PM 법안 심사 소위" not in lines[1], lines

# ---- 4. 회의 전 안건 ----
def sched_row(date, title, degree, angun):
    return {"MEETING_DATE": date, "MEETING_TIME": "10:00", "SESS": "제439회국회(정기회)", "DEGREE": degree, "TITLE": title,
            "COMMITTEE_NAME": "국토교통위원회", "UNIT_CD": "100022", "ANGUN": angun,
            "LINK_URL2": "https://ltc.na.go.kr/cmmit/schl/cmitSchl/view.do?menuNo=2000048&cmtCnclDivCd=002&cmtSchSn=10121481"}

AIR = "1. [2208293] 항공안전법 일부개정법률안(정일영의원 대표발의)"
ALT = "25. 개인형 이동장치의 안전 및 관리에 관한 법률안(대안)(위원장)"            # 번호가 없는 대안 — 낱말로 잡는다
ROAD = "31. [2216647] 도로교통법 일부개정법률안(이연희의원 대표발의)"            # 낱말은 없지만 추적 의안 번호다
listing.update({
    "nrsldhjpaemrmolla": [sched_row("2026-11-18", "교통법안심사소위원회", 2, "<br>".join([AIR, ALT]) + "<br>"),
                          sched_row("2026-11-11", "교통법안심사소위원회", 1, "<br>".join([AIR, ALT, ROAD]))],   # 지난 회의
    "nttmdfdcaakvibdar": [sched_row("2026-11-19", "전체회의", 8, AIR + "<br>")],                             # 관련 안건 없음
})
ag = {"bills": {"2216647": {"name": "도로교통법 일부개정법률안"}, "DD20688": {"name": "대안"}}}
ch = m.check_agendas("KEY", ag)
print("--- 회의 전 안건 ---")
block = "\n".join(m.change_lines(ch))
print(block)
assert len(ch) == 1 and ch[0]["type"] == "agenda_hit" and ch[0]["date"] == "2026-11-18" and ch[0]["items"] == [ALT], ch
assert "안건 예고" in block and "교통법안심사소위원회" in block and "<https://ltc.na.go.kr/" in block, block
assert m.check_agendas("KEY", ag) == []                              # 같은 안건을 두 번 알리지 않는다
listing["nrsldhjpaemrmolla"][0]["ANGUN"] += ROAD                     # 안건이 추가됐다
ch = m.check_agendas("KEY", ag)
assert len(ch) == 1 and ch[0]["items"] == [ALT, ROAD] and ch[0]["new_items"] == [ROAD], ch
assert m.check_agendas("KEY", ag) == []

# ---- 5. 긴 알림은 나눠 보낸다 ----
long_text = "\n".join("• 줄 %03d %s" % (i, "가" * 60) for i in range(120))
blocks = m.text_blocks(long_text)
print("--- 긴 알림 ---")
print("  %d자 → %d덩어리 (가장 긴 것 %d자)" % (len(long_text), len(blocks), max(len(b["text"]["text"]) for b in blocks)))
assert len(blocks) > 1 and all(0 < len(b["text"]["text"]) <= 3000 for b in blocks), blocks
assert "\n".join(b["text"]["text"] for b in blocks) == long_text       # 잃어버리는 줄이 없다
assert len(m.text_blocks("짧은 알림")) == 1

print("\n전부 통과")
