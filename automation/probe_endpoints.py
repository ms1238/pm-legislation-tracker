# -*- coding: utf-8 -*-
"""
임시 진단용 스크립트. 열린국회정보 Open API의 특정 엔드포인트가
어떤 데이터를 주는지(필드명·샘플 행) 확인만 한다. 상태 파일은 건드리지 않는다.

이 컨테이너에서는 open.assembly.go.kr 접근이 egress 정책으로 막혀 있어서,
GitHub Actions 러너에서 대신 실행해 응답 구조를 확인하는 용도.

CONTROL 엔드포인트(이미 운영 중인 ALLSCHEDULE)를 먼저 호출해서,
실패가 "호스트 전체 문제"인지 "해당 ID만의 문제"인지 구분한다.
"""
import json, os, sys, time, urllib.request, urllib.parse

CONTROL_ID = "ALLSCHEDULE"          # 이미 daily-check에서 잘 쓰고 있는 엔드포인트
PROBE_IDS = ["nrkqqbvfanfybishu", "nkimylolanvseqagq"]

KEY = os.environ.get("ASSEMBLY_API_KEY")
TIMEOUT = 60
RETRIES = 3


def call(endpoint, params=None):
    q = {"KEY": KEY, "Type": "json", "pIndex": 1, "pSize": 5}
    if params:
        q.update(params)
    url = "https://open.assembly.go.kr/portal/openapi/%s?%s" % (endpoint, urllib.parse.urlencode(q))
    print("\n>>> GET %s" % url.replace(KEY, "***KEY***"))
    for attempt in range(1, RETRIES + 1):
        started = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            print("    시도 %d: HTTP 200, %.1fs, %d bytes" % (attempt, time.time() - started, len(raw)))
        except Exception as e:
            print("    시도 %d: 실패 (%.1fs) %s" % (attempt, time.time() - started, e))
            continue
        try:
            return json.loads(raw)
        except Exception:
            print("    JSON 파싱 실패. 원문 앞 600자:")
            print("    " + raw[:600].replace("\n", "\n    "))
            return None
    return None


def describe(data):
    """응답에서 head(결과코드)와 row(데이터)를 찾아 구조를 출력."""
    if data is None:
        return None
    if "RESULT" in data:
        print("    RESULT: %s" % json.dumps(data["RESULT"], ensure_ascii=False))
        return None
    for key, val in data.items():
        if not isinstance(val, list):
            continue
        rows = None
        for part in val:
            if not isinstance(part, dict):
                continue
            for h in part.get("head", []):
                if "list_total_count" in h:
                    print("    전체 건수: %s" % h["list_total_count"])
                if "RESULT" in h:
                    print("    RESULT: %s" % json.dumps(h["RESULT"], ensure_ascii=False))
            if "row" in part:
                rows = part["row"]
        print("    서비스명(key): %s" % key)
        if rows:
            print("    필드: %s" % ", ".join(rows[0].keys()))
            for i, r in enumerate(rows[:3]):
                print("    --- row %d ---" % i)
                print("    " + json.dumps(r, ensure_ascii=False, indent=2).replace("\n", "\n    "))
        else:
            print("    row 없음")
        return rows
    print("    예상 밖 응답 형태. 앞 800자:")
    print(json.dumps(data, ensure_ascii=False)[:800])
    return None


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 없음")
        sys.exit(1)

    print("=" * 70)
    print("[대조군] %s — 여기서 실패하면 호스트/키 문제, 성공하면 프로브 ID 문제" % CONTROL_ID)
    print("=" * 70)
    control_ok = describe(call(CONTROL_ID)) is not None

    for eid in PROBE_IDS:
        print("\n" + "=" * 70)
        print("엔드포인트: %s" % eid)
        print("=" * 70)

        rows = describe(call(eid))
        if rows is None:
            continue

        # 22대로 좁혀서 한 번 더 (파라미터명은 API마다 달라 후보를 순차 시도)
        for pname, pval in [("ERACO", "제22대"), ("UNIT_CD", "100022"), ("DAE_NUM", "22")]:
            r2 = describe(call(eid, {pname: pval, "pSize": 100}))
            if not r2:
                continue
            hits = set()
            for row in r2:
                for k, v in row.items():
                    if isinstance(v, str) and "소위" in v:
                        hits.add("%s=%s" % (k, v))
            print("    [소위 포함 값 %d종] %s" % (len(hits), sorted(hits)[:20]))
            break

    print("\n대조군 성공 여부: %s" % control_ok)


if __name__ == "__main__":
    main()
