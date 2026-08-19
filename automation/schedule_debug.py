# -*- coding: utf-8 -*-
"""국회 일정(ALLSCHEDULE) 조회 진단용 스크립트.

"일정이 없었다"와 "일정을 못 봤다"를 구분하려고 만들었다. 상태 파일도 쓰지 않고
슬랙도 보내지 않는다 — 읽고 출력만 한다. 손으로만 실행한다(schedule-debug.yml).

옛 방식(1쪽 300건)과 새 방식(끝까지 페이징)이 각각 무엇을 보는지 나란히 찍어서,
특정 위원회 일정이 안 잡힌 게 페이징 때문인지, 필터 때문인지, 애초에 API에
그 일정이 없어서인지 가른다.
"""
import os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_check_updates as m


def in_window(dt, today, horizon):
    return dt and today <= dt <= horizon


def main():
    key = os.environ.get("ASSEMBLY_API_KEY")
    if not key:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1

    today = m.now_kst().strftime("%Y-%m-%d")
    horizon = (m.now_kst() + m.timedelta(days=m.SCHEDULE_HORIZON_DAYS)).strftime("%Y-%m-%d")
    print("기준일 %s, 관심 구간 ~%s, 관심 위원회 %s"
          % (today, horizon, ", ".join(sorted(m.WATCHED_SCHEDULE_COMMITTEES))))

    # --- 옛 방식: 1쪽 300건만 ---
    print("\n=== 옛 방식(pIndex=1&pSize=300) ===")
    url = ("https://open.assembly.go.kr/portal/openapi/ALLSCHEDULE"
           "?KEY=%s&Type=json&pIndex=1&pSize=300" % key)
    try:
        body = m.api_get(url)["ALLSCHEDULE"]
        old_rows = body[1]["row"]
        total = body[0]["head"][0]["list_total_count"]
    except Exception as e:
        print("조회 실패: %s" % e)
        old_rows, total = [], None
    dates = sorted(r.get("SCH_DT") or "" for r in old_rows)
    print("전체 %s건 중 %d건을 봤다. 이 쪽의 날짜 범위: %s ~ %s"
          % (total, len(old_rows), dates[0] if dates else "-", dates[-1] if dates else "-"))
    print("이 쪽에 들어온 관심 구간 일정: %d건"
          % sum(1 for r in old_rows if in_window((r.get("SCH_DT") or "").strip(), today, horizon)))

    # --- 새 방식: 끝까지 페이징 ---
    print("\n=== 새 방식(끝까지 페이징) ===")
    rows, complete = m.fetch_schedule_rows(key)
    print("총 %d건 조회, 끝까지 읽음=%s" % (len(rows), complete))
    print("SCH_KIND 분포: %s" % dict(Counter((r.get("SCH_KIND") or "(없음)") for r in rows)))

    window = [r for r in rows if in_window((r.get("SCH_DT") or "").strip(), today, horizon)]
    print("\n관심 구간(%s~%s) 전체 일정 %d건:" % (today, horizon, len(window)))
    for r in sorted(window, key=lambda r: (r.get("SCH_DT") or "", r.get("CMIT_NM") or "")):
        # 같은 내용의 행이 여러 개일 수 있어 값이 아니라 객체로 위치를 찾는다.
        page = next(i for i, x in enumerate(rows) if x is r) // m.SCHEDULE_PAGE_SIZE + 1
        watched = (r.get("SCH_KIND") == "본회의"
                   or (r.get("SCH_KIND") == "위원회"
                       and (r.get("CMIT_NM") or "").strip() in m.WATCHED_SCHEDULE_COMMITTEES))
        print("  [%s] %s | SCH_KIND=%r | CMIT_NM=%r | %s | %d쪽"
              % ("추적" if watched else "무시", r.get("SCH_DT"), r.get("SCH_KIND"),
                 r.get("CMIT_NM"), r.get("SCH_CN"), page))

    # 이름이 조금이라도 다르면 필터에서 조용히 빠지므로, 구간 밖까지 훑어서
    # 실제로 어떤 이름으로 들어오는지 본다.
    print("\nCMIT_NM에 '국토'가 들어간 행의 이름 분포(전 구간):")
    print("  %s" % dict(Counter((r.get("CMIT_NM") or "")
                                for r in rows if "국토" in (r.get("CMIT_NM") or ""))))
    print("\n'국토' 관련 행 중 오늘 이후 것:")
    for r in sorted((r for r in rows if "국토" in (r.get("CMIT_NM") or "")
                     and (r.get("SCH_DT") or "") >= today),
                    key=lambda r: r.get("SCH_DT") or ""):
        print("  %s | SCH_KIND=%r | CMIT_NM=%r | %s"
              % (r.get("SCH_DT"), r.get("SCH_KIND"), r.get("CMIT_NM"), r.get("SCH_CN")))


    # --- 쪽마다 어떤 날짜가 들어오는지: 정렬 규칙 추정 ---
    print("\n=== 쪽별 날짜 분포(pSize=300) ===")
    for page in (1, 2, 3, 5, 10):
        u = ("https://open.assembly.go.kr/portal/openapi/ALLSCHEDULE"
             "?KEY=%s&Type=json&pIndex=%d&pSize=300" % (key, page))
        try:
            rs = m.api_get(u)["ALLSCHEDULE"][1]["row"]
        except Exception as e:
            print("  %2d쪽: 조회 실패 %s" % (page, e))
            continue
        m.time.sleep(0.5)
        ds = sorted((r.get("SCH_DT") or "") for r in rs)
        fut = sum(1 for d in ds if d >= today)
        print("  %2d쪽: %d건 | 날짜 %s ~ %s | 오늘 이후 %d건 | 첫 행 SCH_DT=%s"
              % (page, len(rs), ds[0], ds[-1], fut, rs[0].get("SCH_DT")))

    # --- 요청 파라미터로 좁힐 수 있는지 ---
    print("\n=== 요청 파라미터 필터 지원 여부 ===")
    for label, extra in [("필터 없음", ""),
                         ("CMIT_NM=국토교통위원회", "&CMIT_NM=%EA%B5%AD%ED%86%A0%EA%B5%90%ED%86%B5%EC%9C%84%EC%9B%90%ED%9A%8C"),
                         ("SCH_KIND=위원회", "&SCH_KIND=%EC%9C%84%EC%9B%90%ED%9A%8C"),
                         ("SCH_DT=2026-08-19", "&SCH_DT=2026-08-19"),
                         ("UNIT_CD=100022", "&UNIT_CD=100022")]:
        u = ("https://open.assembly.go.kr/portal/openapi/ALLSCHEDULE"
             "?KEY=%s&Type=json&pIndex=1&pSize=5%s" % (key, extra))
        try:
            b = m.api_get(u)["ALLSCHEDULE"]
            n = b[0]["head"][0]["list_total_count"]
            r0 = b[1]["row"][0]
            print("  %-24s → 전체 %d건, 첫 행: %s | %s | %s"
                  % (label, n, r0.get("SCH_DT"), r0.get("CMIT_NM"), (r0.get("SCH_CN") or "")[:30]))
        except Exception as e:
            print("  %-24s → 실패/무응답: %s" % (label, e))
        m.time.sleep(0.5)

    ratio, summary = m.api_health()
    print("\nAPI 조회 상태: %s" % summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
