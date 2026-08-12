# -*- coding: utf-8 -*-
"""
임시 진단용 스크립트. 열린국회정보 Open API의 특정 엔드포인트가
어떤 데이터를 주는지(필드명·샘플 행) 확인만 한다. 상태 파일은 건드리지 않는다.

이 컨테이너에서는 open.assembly.go.kr 접근이 egress 정책으로 막혀 있어서,
GitHub Actions 러너에서 대신 실행해 응답 구조를 확인하는 용도.
"""
import json, os, sys, urllib.request, urllib.parse

PROBE_IDS = [
    "nrkqqbvfanfybishu",
    "nkimylolanvseqagq",
]

KEY = os.environ.get("ASSEMBLY_API_KEY")


def call(endpoint, params=None):
    q = {"KEY": KEY, "Type": "json", "pIndex": 1, "pSize": 5}
    if params:
        q.update(params)
    url = "https://open.assembly.go.kr/portal/openapi/%s?%s" % (endpoint, urllib.parse.urlencode(q))
    safe_url = url.replace(KEY, "***KEY***")
    print("\n>>> GET %s" % safe_url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print("    요청 실패: %s" % e)
        return None
    try:
        return json.loads(raw)
    except Exception:
        print("    JSON 파싱 실패. 원문 앞 500자:")
        print(raw[:500])
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
        head, rows = None, None
        for part in val:
            if isinstance(part, dict) and "head" in part:
                head = part["head"]
            if isinstance(part, dict) and "row" in part:
                rows = part["row"]
        print("    서비스명(key): %s" % key)
        if head:
            for h in head:
                if "list_total_count" in h:
                    print("    전체 건수: %s" % h["list_total_count"])
                if "RESULT" in h:
                    print("    RESULT: %s" % json.dumps(h["RESULT"], ensure_ascii=False))
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

    for eid in PROBE_IDS:
        print("\n" + "=" * 70)
        print("엔드포인트: %s" % eid)
        print("=" * 70)

        rows = describe(call(eid))

        # 22대 국회로 좁혀서 한 번 더 (파라미터명은 API마다 달라서 후보를 순차 시도)
        for pname, pval in [("ERACO", "제22대"), ("UNIT_CD", "100022"), ("DAE_NUM", "22")]:
            data = call(eid, {pname: pval, "pSize": 100})
            r2 = describe(data)
            if r2:
                # 위원회명으로 보이는 필드에서 '소위'가 들어간 값이 있는지 확인
                hits = set()
                for row in r2:
                    for k, v in row.items():
                        if isinstance(v, str) and "소위" in v:
                            hits.add("%s=%s" % (k, v))
                print("    [소위 포함 값 %d종] %s" % (len(hits), sorted(hits)[:20]))
                break


if __name__ == "__main__":
    main()
