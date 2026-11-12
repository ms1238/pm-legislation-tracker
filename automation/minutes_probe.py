# -*- coding: utf-8 -*-
"""회의록·상정·일정 API가 실제로 무엇을 주는지 본다. 진단 전용.

상태 파일도 쓰지 않고 슬랙도 보내지 않는다 — 읽고 출력만 한다(schedule-debug.yml).

    python automation/minutes_probe.py [YYYY-MM-DD]

보는 것:
  1. 소위 회의록 목록(VCONFSUBCCONFLIST) — 행에 어떤 칸이 있는지(링크 칸), 날짜순으로 오는지
  2. 추적 의안의 상정 회의(VCONFBILLCONFLIST) — 최근 회의 행의 칸 전부
  3. 그날 일정(ALLSCHEDULE) — 국토위 행의 칸 전부(안건·링크 칸이 있는지)
  4. 2번에서 본 회의의 DOWN_URL 이 실제로 PDF 를 주는지(안 올라온 회의록은 무엇을 주는지)
"""
import json, os, sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import cloud_check_updates as m

KEY = os.environ.get("ASSEMBLY_API_KEY", "").strip()
API = "https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=1&pSize=%d%s"


def rows_of(endpoint, size, extra=""):
    data = m.api_get(API % (endpoint, KEY, size, extra))
    body = data.get(endpoint)
    if body is None:
        print("  응답에 %s 없음: %s" % (endpoint, json.dumps(data, ensure_ascii=False)[:300]))
        return 0, []
    return body[0]["head"][0]["list_total_count"], body[1]["row"]


def dump(row):
    for k, v in row.items():
        print("    %-16s %s" % (k, str(v)[:160]))


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1
    day = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else m.now_kst().strftime("%Y-%m-%d")

    print("==== 1. 소위 회의록 목록 (앞 50건) ====")
    total, rows = rows_of("VCONFSUBCCONFLIST", 50)
    print("전체 %d건, 받은 %d건" % (total, len(rows)))
    if rows:
        print("첫 행의 칸:")
        dump(rows[0])
        dates = [r.get("CONF_DT") or "" for r in rows]
        print("날짜 내림차순으로 오는가: %s (처음 %s, 끝 %s)" % (dates == sorted(dates, reverse=True), dates[0], dates[-1]))
        for r in rows:
            print("  %s | %s | %s | %s" % (r.get("CONF_DT"), r.get("CMIT_NM"), r.get("SB_CMIT_NM"), r.get("CONF_ID")))

    print("\n==== 2. 추적 의안의 상정 회의 ====")
    with open(os.path.join(BASE_DIR, "snapshot.json"), encoding="utf-8") as f:
        bills = json.load(f)["bills"]
    newest, older = None, []
    for no in ("DD20688", "2216647"):
        bill_id = (bills.get(no) or {}).get("bill_id")
        if not bill_id:
            print("  %s: snapshot 에 bill_id 없음" % no)
            continue
        total, rows = rows_of("VCONFBILLCONFLIST", 30, "&BILL_ID=%s" % bill_id)
        print("[%s] 회의 %d건" % (no, total))
        for r in sorted(rows, key=lambda r: (r.get("CONF_DT") or ""), reverse=True)[:2]:
            dump(r)
            print("    --")
            if r.get("DOWN_URL"):
                older.append(r)
                if newest is None or (r.get("CONF_DT") or "") > (newest.get("CONF_DT") or ""):
                    newest = r

    print("\n==== 3. %s 일정 중 국토교통위원회 ====" % day)
    total, rows = rows_of("ALLSCHEDULE", 100, "&SCH_DT=%s" % day)
    print("그날 전체 %d건" % total)
    for r in rows:
        if "국토교통" in (r.get("CMIT_NM") or ""):
            dump(r)
            print("    --")

    print("\n==== 4. 회의록 주소가 실제로 주는 것 (최근 회의 / 이전 회의) ====")
    import urllib.request
    for r in [newest] + [x for x in older if x is not newest][:1]:
        if not r:
            continue
        print("%s %s %s — %s" % (r.get("SESS"), r.get("DGR"), (r.get("CONF_DT") or "").strip(), r["DOWN_URL"]))
        try:
            req = urllib.request.Request(r["DOWN_URL"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                ctype, blob = resp.headers.get("Content-Type"), resp.read()
            print("  Content-Type %s, %d바이트, PDF 인가: %s" % (ctype, len(blob), blob[:5] == b"%PDF-"))
            if blob[:5] != b"%PDF-":
                print("  본문: %s" % blob[:400].decode("utf-8", "replace").replace("\n", " "))
        except Exception as e:
            print("  못 받았다: %r" % e)

    print("\nAPI 조회 상태: %s" % m.api_health()[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
