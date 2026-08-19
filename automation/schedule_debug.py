# -*- coding: utf-8 -*-
"""국회 일정(ALLSCHEDULE) 조회 진단용 스크립트.

"일정이 없었다"와 "일정을 못 봤다"를 구분하려고 만들었다. 상태 파일도 쓰지 않고
슬랙도 보내지 않는다 — 읽고 출력만 한다. 손으로만 실행한다(schedule-debug.yml).

이 API의 성질(정렬·필터)은 문서보다 실측이 빨라서, 수집 방식을 바꿀 때마다
여기서 먼저 확인한다.
"""
import os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_check_updates as m


def main():
    key = os.environ.get("ASSEMBLY_API_KEY")
    if not key:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1

    today = m.now_kst().strftime("%Y-%m-%d")
    horizon = (m.now_kst() + m.timedelta(days=m.SCHEDULE_HORIZON_DAYS)).strftime("%Y-%m-%d")
    print("기준일 %s, 관심 구간 ~%s, 관심 위원회 %s"
          % (today, horizon, ", ".join(sorted(m.WATCHED_SCHEDULE_COMMITTEES))))

    print("\n=== 지금 방식(날짜별 조회) ===")
    rows, ok = m.fetch_schedule_rows(key)
    print("총 %d건 조회, 성공=%s" % (len(rows), ok))
    print("날짜별 건수: %s" % dict(sorted(Counter((r.get("SCH_DT") or "?") for r in rows).items())))
    print("SCH_KIND 분포: %s" % dict(Counter((r.get("SCH_KIND") or "(없음)") for r in rows)))

    print("\n관심 구간 일정 %d건 (추적/무시 판정):" % len(rows))
    for r in sorted(rows, key=lambda r: (r.get("SCH_DT") or "", r.get("CMIT_NM") or "")):
        watched = (r.get("SCH_KIND") == "본회의"
                   or (r.get("SCH_KIND") == "위원회"
                       and (r.get("CMIT_NM") or "").strip() in m.WATCHED_SCHEDULE_COMMITTEES))
        if not watched and r.get("SCH_KIND") != "위원회":
            continue  # 세미나·기자회견은 양이 많아 접는다
        print("  [%s] %s | SCH_KIND=%r | CMIT_NM=%r | %s"
              % ("추적" if watched else "무시", r.get("SCH_DT"), r.get("SCH_KIND"),
                 r.get("CMIT_NM"), r.get("SCH_CN")))

    # 요청 파라미터가 실제로 먹는지. CMIT_NM·UNIT_CD는 무시되고(전체 건수가 그대로),
    # SCH_DT·SCH_KIND만 걸린다 — 날짜별 조회로 간 근거다.
    print("\n=== 요청 파라미터 필터 지원 여부 ===")
    for label, extra in [("필터 없음", ""),
                         ("CMIT_NM=국토교통위원회", "&CMIT_NM=%EA%B5%AD%ED%86%A0%EA%B5%90%ED%86%B5%EC%9C%84%EC%9B%90%ED%9A%8C"),
                         ("SCH_KIND=위원회", "&SCH_KIND=%EC%9C%84%EC%9B%90%ED%9A%8C"),
                         ("SCH_DT=%s" % today, "&SCH_DT=%s" % today)]:
        u = ("https://open.assembly.go.kr/portal/openapi/ALLSCHEDULE"
             "?KEY=%s&Type=json&pIndex=1&pSize=5%s" % (key, extra))
        try:
            b = m.api_get(u)["ALLSCHEDULE"]
            r0 = b[1]["row"][0]
            print("  %-24s → 전체 %d건, 첫 행 %s | %s"
                  % (label, b[0]["head"][0]["list_total_count"], r0.get("SCH_DT"), r0.get("CMIT_NM")))
        except Exception as e:
            print("  %-24s → 실패/무응답: %s" % (label, e))
        m.time.sleep(0.3)

    print("\nAPI 조회 상태: %s" % m.api_health()[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
