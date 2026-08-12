# -*- coding: utf-8 -*-
"""
페이지(index.html)에 표기된 의원별 소속 정당을 국회 Open API와 전수 대조한다.

트래커는 그동안 정당 변동을 추적하지 않아서(스냅샷이 위원회만 저장) 탈당·입당·
제명이 일어나도 페이지 표기가 갱신되지 않았다. 이 스크립트는 그렇게 누적된
불일치를 한 번에 찾아내기 위한 점검 도구다.

개발 컨테이너에서는 open.assembly.go.kr 접근이 egress 정책으로 막혀 있으므로
GitHub Actions 러너에서 실행한다. 페이지를 고치지는 않고 보고만 한다.
"""
import json, os, re, sys, time, urllib.parse, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(os.path.dirname(BASE_DIR), "index.html")

KEY = os.environ.get("ASSEMBLY_API_KEY")
TIMEOUT = 30
RETRIES = 3

# 정당명이 담긴 필드는 API 문서마다 표기가 달라 후보를 순서대로 시도한다.
PARTY_FIELDS = ["PLPT_NM", "POLY_NM", "PARTY_NM", "POLY_NM_LIST"]

# 페이지에서 정당을 나타내는 칩 클래스 → 표기 문자열은 칩 안의 텍스트를 그대로 쓴다.
PARTY_CHIP_RE = re.compile(
    r'<span class="member-name">([^<]+)</span>\s*'
    r'(?:<span class="chip chair">[^<]*</span>\s*)?'
    r'<span class="chip (?:dem|ppp|jp|meta)"[^>]*>([^<]+)</span>'
)
COMPACT_RE = re.compile(
    r'<span class="cname">([^<]+)</span><span class="cmeta">([^<·]+)·'
)


def api_get(url):
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == RETRIES:
                print("    조회 실패(%d회): %s" % (RETRIES, e))
                return None
            time.sleep(2 * attempt)
    return None


def current_party(row):
    """PLPT_NM 등은 '더불어민주당/무소속'처럼 이력이 슬래시로 이어진다 — 마지막이 현재."""
    for f in PARTY_FIELDS:
        raw = row.get(f)
        if raw:
            return raw.split("/")[-1].strip(), f
    return "", None


def lookup(name):
    enc = urllib.parse.quote(name)
    url = ("https://open.assembly.go.kr/portal/openapi/ALLNAMEMBER"
           "?KEY=%s&Type=json&pIndex=1&pSize=10&NAAS_NM=%s" % (KEY, enc))
    data = api_get(url)
    if not data:
        return None
    try:
        rows = data["ALLNAMEMBER"][1]["row"]
    except Exception:
        return None
    for r in rows:
        if "22" in (r.get("GTELT_ERACO") or ""):
            return r
    return rows[0] if rows else None


def page_members():
    """①~⑤번(현직 의원) 구간에서 이름 → 페이지 표기 정당을 뽑는다. ⑥⑦번은 기관·전직이라 제외."""
    h = open(INDEX_PATH, encoding="utf-8").read()
    start = h.index("<!-- CATEGORY 1 -->")
    end = h.index("<!-- CATEGORY 6 -->")
    scope = h[start:end]
    found = {}
    for pattern in (PARTY_CHIP_RE, COMPACT_RE):
        for name, party in pattern.findall(scope):
            name = name.strip()
            # 여러 명을 한 카드에 묶은 합본 항목(예: "박성민 · 정준호 · …")은 건너뛴다.
            # 구성원 각자는 별도 카드로도 실려 있어 누락되지 않는다.
            if not re.fullmatch(r"[가-힣]{2,4}", name):
                continue
            found.setdefault(name, party.strip())
    return found


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 없음")
        sys.exit(1)

    members = page_members()
    print("페이지에서 추출한 현직 의원 %d명\n" % len(members))

    # 슬래시 이력의 정렬 방향을 판정하기 위한 대조군.
    # 윤종오·황운하는 소수정당, 장경태는 탈당 이력이 있어 이력이 여러 개일 것으로 예상된다.
    print("[원본 값 표본]")
    for name in ("장경태", "윤종오", "황운하", "정동만"):
        row = lookup(name)
        if row:
            print("  %-5s PLPT_NM=%r" % (name, row.get("PLPT_NM")))
        else:
            print("  %-5s 조회 실패" % name)
    print()

    mismatches, unresolved, field_seen = [], [], set()
    for i, (name, shown) in enumerate(sorted(members.items()), 1):
        row = lookup(name)
        if row is None:
            unresolved.append((name, shown, "API 조회 실패/결과 없음"))
            print("%3d. %-5s 페이지=%-8s → 조회 실패" % (i, name, shown))
            continue
        actual, field = current_party(row)
        if field:
            field_seen.add(field)
        if not actual:
            unresolved.append((name, shown, "응답에 정당 필드 없음: %s" % sorted(row.keys())))
            print("%3d. %-5s 페이지=%-8s → 정당 필드 없음" % (i, name, shown))
        elif actual != shown:
            mismatches.append((name, shown, actual))
            print("%3d. %-5s 페이지=%-8s API=%-8s  ← 불일치" % (i, name, shown, actual))
            # 슬래시 이력의 순서(오래된순/최신순)를 눈으로 확인하기 위해 원본을 그대로 찍는다.
            print("       원본 %s = %r" % (field, row.get(field)))
            print("       GTELT_ERACO=%r  ELECD_NM=%r" % (row.get("GTELT_ERACO"), row.get("ELECD_NM")))
        else:
            print("%3d. %-5s %s" % (i, name, actual))

    print("\n" + "=" * 60)
    print("사용한 정당 필드: %s" % (sorted(field_seen) or "(없음)"))
    print("불일치 %d건 / 확인불가 %d건 / 전체 %d명" % (len(mismatches), len(unresolved), len(members)))
    if mismatches:
        print("\n[정정 필요]")
        for name, shown, actual in mismatches:
            print("  %s: %s → %s" % (name, shown, actual))
    if unresolved:
        print("\n[확인 불가]")
        for name, shown, why in unresolved:
            print("  %s (페이지=%s): %s" % (name, shown, why))


if __name__ == "__main__":
    main()
