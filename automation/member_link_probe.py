# -*- coding: utf-8 -*-
"""의원 카드에 걸 국회 홈페이지 주소를 찾는다.

국회 개인 페이지는 https://www.assembly.go.kr/members/22nd/<영문이름> 꼴이다
(정동만 → JEONGDONGMAN, 2026-11-05 실측 200). 그런데 의원 목록 페이지가 404라
이름-슬러그 쌍을 긁어올 데가 없고, 로마자 표기는 사람마다 달라서(이 → LEE/YI/RHEE,
박 → PARK/BAK) 추측하면 깨진 링크가 된다.

그래서 ALLNAMEMBER 가 주는 영문명으로 주소를 만든 뒤 하나씩 눌러 본다. 200 인 것만
쓴다. 추측은 하지 않는다 — 안 되면 '확인 불가'로 남긴다.

    python automation/member_link_probe.py            # index.html 의 카드 이름 전부
    python automation/member_link_probe.py 정동만,김미애  # 지정한 이름만
"""
import json, os, re, sys, time, urllib.parse, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMBER_PAGE = "https://www.assembly.go.kr/members/22nd/%s"
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}


def card_names():
    """index.html 의 member-name 중 사람 이름만. 기관 카드는 '(...)' 가 붙어 있다."""
    h = open(os.path.join(BASE_DIR, os.pardir, "index.html"), encoding="utf-8").read()
    return [n for n in re.findall(r'<span class="member-name">([^<]+)</span>', h) if "(" not in n]


def eng_names(key, name):
    """ALLNAMEMBER 가 주는 영문명 후보. 어느 필드에 들어 있는지 몰라 전부 긁는다."""
    url = ("https://open.assembly.go.kr/portal/openapi/ALLNAMEMBER?KEY=%s&Type=json&pIndex=1&pSize=5&NAAS_NM=%s"
           % (key, urllib.parse.quote(name)))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = data.get("ALLNAMEMBER", [None, None])[1]["row"]
    if not rows:
        return None, []
    # 동명이인이 있다. 22대 행을 먼저 보되 거기서 안 되면 나머지 행의 영문명도 쓴다 —
    # 김희정·박성민이 한 행만 보고 실패했다(2026-11-05).
    rows = sorted(rows, key=lambda r: "22" not in (r.get("GTELT_ERACO") or ""))
    out, seen = [], set()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, str) and re.fullmatch(r"[A-Za-z][A-Za-z ,.\-']{3,}", v.strip()):
                val = v.strip()
                if val not in seen:
                    seen.add(val)
                    out.append((k, val))
    return rows[0], out


def slug(eng):
    """국회 주소 꼴로 바꾼다 — 대문자, 공백·쉼표·하이픈·마침표·따옴표 제거."""
    return re.sub(r"[^A-Z]", "", eng.upper())


def status(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return str(e)[:40]


def main(argv):
    key = os.environ.get("ASSEMBLY_API_KEY", "").strip()
    if not key:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1
    names = argv[1].split(",") if len(argv) > 1 else card_names()
    print("확인할 이름 %d명" % len(names))

    first = True
    found, missing = {}, []
    for name in names:
        try:
            row, engs = eng_names(key, name)
        except Exception as e:
            print("%-5s API 실패: %s" % (name, e))
            missing.append(name)
            continue
        if first and row:
            print("\n[ALLNAMEMBER 응답 필드] %s\n" % ", ".join(sorted(row)))
            first = False
        if not engs:
            print("%-5s 영문명으로 보이는 필드 없음" % name)
            missing.append(name)
            continue
        hit = None
        for field, eng in engs:
            sl = slug(eng)
            if not sl:
                continue
            st = status(MEMBER_PAGE % sl)
            time.sleep(0.3)
            if st == 200:
                hit = (field, eng, sl)
                break
        if hit:
            found[name] = hit[2]
            print("%-5s → %-22s (%s=%s)" % (name, hit[2], hit[0], hit[1]))
        else:
            print("%-5s 200 인 주소 없음. 시도: %s" % (name, ", ".join(slug(e) for _, e in engs)))
            missing.append(name)

    print("\n=== 결과 ===")
    print("확인 %d명 / 확인 불가 %d명" % (len(found), len(missing)))
    if missing:
        print("확인 불가:", ", ".join(missing))
    print("\n--- 붙여 쓸 매핑 (JSON) ---")
    print(json.dumps(found, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
