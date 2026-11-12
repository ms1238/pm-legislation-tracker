# -*- coding: utf-8 -*-
"""회의가 열리기 전에 안건(의사일정)을 볼 수 있는 곳이 있는지 알아본다. 진단 전용.

지금 쓰는 ALLSCHEDULE 은 회의 이름·날짜·시각만 주고 안건이 없다. 그래서 일정 알림은
며칠 전에 나가도 "PM 법안이 올라간다"는 말은 회의록이 등록된 뒤에야 할 수 있었다.
위원회 일정 API 가 회의별 안내 페이지 주소를 주는지, 그 페이지에 안건이나 의사일정
파일이 붙어 있는지 본다. 상태 파일도 쓰지 않고 슬랙도 보내지 않는다.

    python automation/agenda_probe.py [YYYY-MM-DD]

서비스 이름은 기억에 의존한 후보다. 틀리면 API 가 '없는 서비스'라고 답하고, 그 답을
그대로 찍는다.
"""
import html, json, os, re, sys, urllib.parse, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import cloud_check_updates as m

KEY = os.environ.get("ASSEMBLY_API_KEY", "").strip()
API = "https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=1&pSize=%d%s"
UNIT = "&UNIT_CD=100022"   # 제22대
CANDIDATES = [
    ("nrsldhjpaemrmolla", "위원회별 소위원회 일정(후보)"),
    ("nttmdfdcaakvibdar", "위원회별 전체회의 일정(후보)"),
    ("napvpafracrdkxmoq", "위원회별 공청회 일정(후보)"),
    ("nekcaiymatialqlxr", "본회의 일정(후보)"),
]


def call(service, size, extra):
    try:
        data = m.api_get(API % (service, KEY, size, extra))
    except Exception as e:
        return None, "요청 실패: %r" % e
    body = data.get(service)
    if body is None:
        return None, json.dumps(data, ensure_ascii=False)[:200]
    return body[1]["row"], body[0]["head"][0]["list_total_count"]


def text_of(page):
    page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", page))).strip()


def show_page(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype, blob = resp.headers.get("Content-Type"), resp.read()
    except Exception as e:
        print("    못 열었다: %r" % e)
        return
    page = blob.decode("utf-8", "replace")
    text = text_of(page)
    print("    %s, %d바이트, 글자 %d자" % (ctype, len(blob), len(text)))
    for word in ("안건", "의사일정", "법률안", "개인형"):
        at = [x.start() for x in re.finditer(word, text)]
        print("    '%s' %d번%s" % (word, len(at), (" — …%s…" % text[max(0, at[0] - 60):at[0] + 240]) if at else ""))
    links = sorted({h for h in re.findall(r'''(?i)(?:href|src|onclick)\s*=\s*["']([^"']{4,300})''', page)
                    if re.search(r"(?i)down|file|attach|\.hwp|\.pdf|fn_|javascript", h)})
    print("    파일·스크립트 링크 %d개: %s" % (len(links), links[:12]))


def main():
    if not KEY:
        print("ASSEMBLY_API_KEY 환경변수가 없다")
        return 1
    day = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else m.now_kst().strftime("%Y-%m-%d")
    call("ALLSCHEDULE", 1, "&SCH_DT=%s" % day)   # 연결 확인(성공 한 번을 먼저 센다)

    for service, label in CANDIDATES:
        print("\n==== %s — %s ====" % (service, label))
        for extra in (UNIT, ""):
            rows, info = call(service, 100, extra)
            if rows is None:
                print("  %s → %s" % (extra or "(조건 없음)", info))
                continue
            print("  %s → 전체 %s건, 받은 %d건. 첫 행:" % (extra or "(조건 없음)", info, len(rows)))
            for k, v in rows[0].items():
                print("    %-18s %s" % (k, str(v)[:150]))
            ours = [r for r in rows if "국토교통" in json.dumps(r, ensure_ascii=False)]
            near = [r for r in ours if day.replace("-", "") in re.sub(r"\D", "", json.dumps(r, ensure_ascii=False))]
            print("  국토교통위 %d건, 그중 %s 것 %d건" % (len(ours), day, len(near)))
            for r in (near or ours)[:3]:
                print("  -- " + json.dumps(r, ensure_ascii=False)[:400])
                for v in r.values():
                    if isinstance(v, str) and v.startswith("http"):
                        print("  → %s" % v)
                        show_page(v)
            break

    print("\n==== 안건 감시(일일 체크와 같은 함수)를 돌려 본다 — 상태 파일·슬랙 없이 ====")
    with open(os.path.join(BASE_DIR, "snapshot.json"), encoding="utf-8") as f:
        snapshot = json.load(f)
    real_now = m.now_kst
    y, mo, d = (int(x) for x in day.split("-"))
    for label, clock in (("지금 기준", real_now), ("%s 아침이었다면" % day, lambda: m.datetime(y, mo, d, 8, 0, tzinfo=m.KST))):
        m.now_kst = clock
        snap = dict(snapshot, seen_agenda={})
        changes = m.check_agendas(KEY, snap)
        print("[%s] 알릴 회의 %d건" % (label, len(changes)))
        print("\n".join(m.change_lines(changes)) or "  (없음)")
    m.now_kst = real_now

    print("\nAPI 조회 상태: %s" % m.api_health()[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
