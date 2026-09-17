# -*- coding: utf-8 -*-
"""추적 중인 의안들의 현재 동향을 한 번에 훑어 본다.

일일 체크는 "지난 실행과 달라진 것"만 알린다. 이 스크립트는 그와 달리 지금
상태를 통째로 보여준다 — "요즘 뭐 움직이는 거 있나"를 손으로 물어볼 때 쓴다.
상태 파일도 쓰지 않고 슬랙도 보내지 않는다.

    python automation/trend_probe.py

보는 것:
  1. 추적 의안 47건의 진행단계 (snapshot 에 적힌 것과 달라졌는지)
  2. 새로 상정된 회의 (snapshot 이 기억하는 회의 목록과 비교)
  3. 앞으로 14일 행안위·국토위·법사위·본회의 일정
  4. 검토보고서를 API 나 의안정보시스템에서 볼 수 있는지 (지금은 아예 안 보고 있다)
"""
import json, os, re, sys, time, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import cloud_check_updates as m

KEY = os.environ.get("ASSEMBLY_API_KEY", "").strip()
LIKMS = "https://likms.assembly.go.kr/bill/billDetail.do?billId=%s"
# 의안정보시스템 상세 페이지에서 찾을 말. 검토보고서는 소관위 심사 단계에서
# 수석전문위원이 쓰는 문서라, 이게 떴다는 건 심사가 실제로 굴러간다는 뜻이다.
DOC_TERMS = ["검토보고서", "검토보고", "심사보고서", "비용추계"]


def section(title):
    print("\n" + "=" * 4 + " " + title + " " + "=" * 4)


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as e:
        return None


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1
    with open(os.path.join(BASE_DIR, "snapshot.json"), encoding="utf-8") as f:
        snapshot = json.load(f)
    bills = snapshot["bills"]

    section("1. 추적 의안 진행단계 (%d건)" % len(bills))
    moved, same, unknown = [], 0, []
    tally = {}
    for no, info in sorted(bills.items()):
        now = m.get_stage(KEY, no)
        time.sleep(0.2)
        if now is None:
            unknown.append(no)
            continue
        tally[now] = tally.get(now, 0) + 1
        if now != info["stage"]:
            moved.append((no, info["name"], info["stage"], now))
        else:
            same += 1
    if moved:
        print("  ** 단계가 달라진 의안 %d건 **" % len(moved))
        for no, name, old, new in moved:
            print("   [%s] %s\n        %s  →  %s" % (no, name, old, new))
    else:
        print("  단계가 달라진 의안 없음")
    print("  그대로 %d건, 조회 실패 %d건" % (same, len(unknown)))
    print("  현재 분포:")
    for st, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print("    %-34s %d건" % (st, n))

    section("2. 새로 상정된 회의")
    hits = 0
    for no, info in sorted(bills.items()):
        bill_id = info.get("bill_id")
        if not bill_id:
            continue
        url = ("https://open.assembly.go.kr/portal/openapi/VCONFBILLCONFLIST?KEY=%s&Type=json&pIndex=1&pSize=30&BILL_ID=%s"
               % (KEY, bill_id))
        try:
            data = m.api_get(url)
            rows = data.get("VCONFBILLCONFLIST", [None, None])[1]["row"]
        except Exception:
            continue
        time.sleep(0.2)
        seen = set(info.get("seen_conf_ids", []))
        for r in rows:
            cid = r.get("CONF_ID")
            if cid and cid not in seen:
                hits += 1
                print("   [%s] %s\n        %s %s %s (%s)"
                      % (no, info["name"], r.get("CONF_KND", ""), r.get("SESS", ""),
                         r.get("DGR", ""), (r.get("CONF_DT") or "").strip()))
    if not hits:
        print("   새로 상정된 회의 없음")

    section("3. 앞으로 %d일 일정" % m.SCHEDULE_HORIZON_DAYS)
    rows, ok = m.fetch_schedule_rows(KEY)
    if not ok:
        print("   일정 조회가 온전하지 않았다 — 아래가 전부가 아닐 수 있다")
    today = m.now_kst().strftime("%Y-%m-%d")
    got = 0
    for r in sorted(rows, key=lambda r: (r.get("SCH_DT") or "")):
        # check_schedule 과 같은 기준으로 고른다 — 본회의는 CMIT_NM 이 비어 있어서
        # 위원회 이름만으로 거르면 통째로 빠진다.
        kind = r.get("SCH_KIND")
        cmit = (r.get("CMIT_NM") or "").strip()
        dt = (r.get("SCH_DT") or "").strip()
        if not dt or dt < today:
            continue
        if not (kind == "본회의" or (kind == "위원회" and cmit in m.WATCHED_SCHEDULE_COMMITTEES)):
            continue
        got += 1
        print("   %s  %-12s %s" % (dt, cmit or "본회의", (r.get("SCH_CN") or "").strip()[:60]))
    if not got:
        print("   관심 위원회 일정 없음")

    section("4. 검토보고서를 볼 수 있나")
    print("   지금 트래커는 검토보고서를 아예 안 본다. 볼 수 있는지 확인한다.")
    # 심사가 굴러가는 의안(소관위 심사중)부터 본다 — 검토보고서가 있다면 여기 있다.
    live = [(no, i) for no, i in sorted(bills.items()) if "심사중" in i["stage"]][:5]
    for no, info in live:
        html = fetch_page(LIKMS % info["bill_id"])
        if html is None:
            print("   [%s] 의안정보시스템 페이지를 못 받았다" % no)
            continue
        found = [t for t in DOC_TERMS if t in html]
        print("   [%s] %s\n        페이지 %d자, 걸린 문서어: %s"
              % (no, info["name"][:28], len(html), ", ".join(found) or "없음"))
        if found:
            i = html.find(found[0])
            around = re.sub(r"<[^>]+>", " ", html[max(0, i - 300):i + 300])
            print("        …%s…" % " ".join(around.split())[:220])
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
