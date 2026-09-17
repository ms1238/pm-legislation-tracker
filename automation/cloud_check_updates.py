# -*- coding: utf-8 -*-
"""
PM(개인형 이동수단) 법안 트래커 - 클라우드(Anthropic Routine)용 일일 변경 감지 스크립트.

로컬 버전(check_updates.py)과 로직은 동일하지만:
  - 비밀값(국회 Open API 키, 슬랙 웹훅 URL)은 파일이 아니라 환경변수로 받는다
    (이 저장소는 퍼블릭이라 비밀값을 파일에 커밋하면 안 됨):
      ASSEMBLY_API_KEY, SLACK_WEBHOOK_URL
  - 상태 파일(snapshot.json, member_snapshot.json, pending_updates.json)은
    이 스크립트와 같은 automation/ 폴더에 있고, 실행 후 git으로 커밋·푸시하는 건
    이 스크립트를 호출하는 쪽(루틴 프롬프트)의 책임이다.
  - 이 스크립트는 페이지(index.html) 내용을 직접 수정하지 않는다 — 감지+알림+상태갱신까지만.
    실제 편집은 여전히 사람이 "PM 트래커 업데이트 반영해줘"라고 요청했을 때 처리한다.
"""
import json, os, sys, re, io, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

# 이 트래커가 다루는 시간은 전부 한국 국회 일정이라 KST가 기준이다.
# GitHub 러너의 로컬 시간은 UTC이므로 datetime.now()를 그대로 쓰면
# "오늘 일정" 판정이 국회 API가 주는 KST 날짜와 어긋날 수 있다
# (UTC 15:00 이후 = KST 다음날). 실행이 밀리면 실제로 발생한다.
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)

WATCHED_SCHEDULE_COMMITTEES = {"국토교통위원회", "행정안전위원회", "법제사법위원회"}
SCHEDULE_HORIZON_DAYS = 14
# ALLSCHEDULE은 전체가 9만 건이 넘고 SCH_DT 내림차순으로 내려온다. 쪽 단위로 긁으면
# 먼 미래 일정이 등록될수록 관심 구간이 밀려나므로, 날짜로 직접 묻는다(하루 20건 안팎).
SCHEDULE_DAY_PAGE_SIZE = 100
SCHEDULE_DAY_DELAY_SEC = 0.3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(BASE_DIR, "snapshot.json")
MEMBER_SNAPSHOT_PATH = os.path.join(BASE_DIR, "member_snapshot.json")
PENDING_PATH = os.path.join(BASE_DIR, "pending_updates.json")

ARTIFACT_URL = "https://claude.ai/code/artifact/80fc7afc-0941-46aa-bd9f-b89e687e3c08"
GITHUB_PAGES_URL = "https://ms1238.github.io/pm-legislation-tracker/"

MEETING_KEYWORDS = ["개인형 이동", "이동장치", "이동수단", "전동킥보드", "킥라니", "퍼스널모빌리티", "퍼스널 모빌리티"]
TITLE_SUFFIX = r"(위원장|위원|장관|차관|청장|차장|처장|실장|국장|과장|원장|총장|본부장|대표|진술인|증인|참고인)"
NAME_RE = re.compile(r'^([가-힣]{2,4})\s+(.*)', re.DOTALL)


def log(msg):
    print("[%s] %s" % (now_kst().strftime("%Y-%m-%d %H:%M:%S"), msg))

# 국회 API 호출 성패 집계. 호출부가 예외를 전부 삼키기 때문에(그래야 한 건 실패가
# 전체를 멈추지 않는다) 여기서 세어두지 않으면 "API가 죽어서 아무것도 못 봤다"와
# "볼 게 없었다"를 구분할 방법이 없다.
API_ATTEMPTS = 0
API_FAILURES = 0

# 이 비율 이상 실패하면 그 실행은 신뢰할 수 없다고 보고 상태를 갱신하지 않는다.
FAILURE_ABORT_RATIO = 0.5

# 러너에서 국회 API 연결이 통째로 막히는 날이 있다(2026-08-18 저녁, 2026-08-21 오전).
# 그때도 전 구간을 끝까지 도느라 호출 하나에 15초씩 163번을 버려서 실행이 41분 38초
# 걸렸다 — 결과는 어차피 "상태 갱신 안 함"으로 같다. 한 건도 성공하지 못한 채
# 이만큼 실패했으면 연결이 문제인 게 분명하니 그 자리에서 끝낸다.
FAILFAST_AFTER = 12
API_SUCCESSES = 0


def api_get(url):
    global API_ATTEMPTS, API_FAILURES, API_SUCCESSES
    API_ATTEMPTS += 1
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        API_FAILURES += 1
        if API_SUCCESSES == 0 and API_FAILURES >= FAILFAST_AFTER:
            abort_no_connection()
        raise
    API_SUCCESSES += 1
    return data


def abort_no_connection():
    """연결 자체가 안 되는 실행을 즉시 끝낸다.

    호출부가 예외를 전부 삼키므로(그래야 한 건 실패가 전체를 멈추지 않는다) 보통
    예외로는 여기서 빠져나갈 수 없다. SystemExit은 Exception이 아니라서
    그 handler들을 그대로 통과한다.
    """
    log("!!! 국회 API 조회가 %d회 연속 실패 — 한 건도 성공하지 못했습니다." % API_FAILURES)
    log("!!! 연결 문제로 보고 즉시 종료합니다(상태 파일은 갱신하지 않습니다).")
    log("!!! (슬랙 알림은 보내지 않습니다. Actions 실행이 실패로 표시됩니다.)")
    print("API_FAILED=true")
    print("HAS_CHANGES=false")
    raise SystemExit(1)


def api_health():
    """(실패율, 요약문자열)을 돌려준다. 호출이 아예 없었으면 실패율 0으로 본다."""
    if API_ATTEMPTS == 0:
        return 0.0, "API 호출 없음"
    ratio = API_FAILURES / API_ATTEMPTS
    return ratio, "%d건 중 %d건 실패 (%.0f%%)" % (API_ATTEMPTS, API_FAILURES, ratio * 100)

def bill_detail(key, bill_no):
    """ALLBILL 한 건. 못 받으면 None — "정보가 없다"와 구분해야 하므로 빈 dict 가 아니다."""
    url = "https://open.assembly.go.kr/portal/openapi/ALLBILL?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_NO=%s" % (key, bill_no)
    try:
        data = api_get(url)
        return data.get("ALLBILL", [None, None])[1]["row"][0]
    except Exception:
        return None


def stage_from_row(row):
    if row.get("PROM_DT"):
        return "공포 · " + row["PROM_DT"]
    if row.get("GVRN_TRSF_DT"):
        return "정부이송 · " + row["GVRN_TRSF_DT"]
    if row.get("RGS_RSLN_DT"):
        return "본회의 " + (row.get("RGS_CONF_RSLT") or "처리") + " · " + row["RGS_RSLN_DT"]
    if row.get("LAW_PROC_DT"):
        return "법사위 " + (row.get("LAW_PROC_RSLT") or "처리") + " · " + row["LAW_PROC_DT"]
    if row.get("LAW_PRSNT_DT"):
        return "법사위 계류 · " + row["LAW_PRSNT_DT"]
    if row.get("JRCMIT_PROC_DT"):
        return "소관위 " + (row.get("JRCMIT_PROC_RSLT") or "처리")
    if row.get("JRCMIT_PRSNT_DT"):
        return "소관위 심사중"
    if row.get("JRCMIT_CMMT_DT"):
        return "소관위 접수"
    return "정보없음"


def get_stage(key, bill_no):
    row = bill_detail(key, bill_no)
    return None if row is None else stage_from_row(row)


# --- 신규 의안 탐지 ---------------------------------------------------------
#
# 예전에는 의안명에 PM 검색어가 들어간 것만 찾았다. 그래서 도로교통법 개정안은
# 한 건도 못 잡았다 — 제명이 늘 "도로교통법 일부개정법률안"이라 검색어가 한 글자도
# 안 들어가고, PM 얘기는 제안이유·주요내용에 있기 때문이다. 2026-08-13~09-16
# 34일 동안 자동으로 추가된 의안이 0건이었던 이유가 이것이다. 그 사이에도 도로교통법
# 개정안은 계속 발의됐고(2221403 이 그 예다), 지금 snapshot 에 있는 도로교통법
# 27건은 전부 2026-08-21 에 사람이 한 번에 넣은 것이다.
#
# 이제 notice_watch.py 가 국회 입법예고에 쓰는 방식을 그대로 쓴다.
#   1) 의안명에 PM 말이 직접 들어가면 그대로 적중(제정안 같은 것들)
#   2) 지정 법(PM 규제가 실리는 법)의 개정안이면 후보로 잡고, 제안이유·주요내용을
#      받아 PM 관심어가 있는 것만 남긴다
#
# 후보는 지정 법 이름으로 직접 물어서 모은다. TVBPMBILL11 의 BILL_NAME 필터는
# 부분일치가 맞다 — 2026-09-16 실측으로 '도로교통법' 157건, '개인형 이동' 13건이
# 왔다(국회 입법예고 API 는 부분일치가 안 되는데, 이쪽은 다르다).
#
# 다만 그 성질에 매달지는 않는다. 응답에 제안일이 없거나 이름 검색이 빈손이면
# '최근 발의분 훑기'로 물러선다 — 쪽을 걸어가며 받아 로컬에서 거르는 방식이라
# 필터가 어떻게 동작하든 상관없다.

BILL_NAME_KEYWORDS = ["개인형 이동", "퍼스널모빌리티", "전동킥보드", "킥라니"]

# PM 규제가 실리는 법. 의안명 앞부분과 맞춰 보는 용도라 법 제명을 그대로 적는다.
BILL_WATCH_LAWS = [
    "도로교통법",                    # '자전거등'(자전거+개인형 이동장치) 정의와 통행·주차·제재
    "자전거 이용 활성화에 관한 법률",   # 공공·공유 자전거 근거법
    "도로법",
    "주차장법",
    "자동차관리법",
    "교통약자의 이동편의 증진법",
    "개인형 이동수단",                # PM 기본법 계열 제정안
]

# 제안이유·주요내용에서 찾을 말. 앞은 PM 고유어, 뒤는 인접어다. 인접어만 걸린 건도
# 알리되 꼬리표를 달아 구분한다 — 거르는 건 사람이 한다(notice_watch 와 같은 원칙).
BILL_PM_TERMS_DIRECT = ["개인형 이동장치", "개인형이동장치", "개인형 이동수단",
                        "개인형이동수단", "전동킥보드", "킥보드", "전동이륜평행차",
                        "퍼스널 모빌리티", "퍼스널모빌리티", "킥라니"]
BILL_PM_TERMS_NEAR = ["자전거등", "전기자전거", "자전거 대여", "대여사업", "대여업",
                      "공유 모빌리티", "공유모빌리티", "인명보호", "안전모"]

# 훑을 기간. 하루 두 번 도니까 짧아도 되지만, 실행이 며칠 연속 실패해도 메워지도록
# 넉넉히 둔다 — 2026-09 기준 실행 실패율이 절반 가까웠다.
BILL_SWEEP_DAYS = 60
# 쪽 크기는 큰 것부터 시도한다. 1000이 이 API 의 상한이지만 거부하는 날이 있어
# (입법예고 쪽에서 쪽 크기 때문에 막힌 전례가 있다) 작은 것으로 물러설 길을 둔다.
BILL_SWEEP_PAGE_SIZES = [1000, 100]
BILL_SWEEP_MAX_PAGES = 6
# 제안이유 조회는 후보 한 건당 한 번이다. 처음 도는 실행은 밀린 게 많아 수십 건이
# 될 수 있어 상한을 둔다. 못 본 건 다음 실행이 이어서 본다(훑기 구간이 60일이라 남는다).
BILL_SUMMARY_BUDGET = 40

# 의안 목록 엔드포인트. 앞의 것이 제안일을 주지 않거나 응답하지 않으면 뒤를 쓴다.
BILL_LIST_ENDPOINTS = ["nzmimeepazxkubdpn", "TVBPMBILL11"]


def _first(row, *names):
    """API 마다 같은 뜻의 필드 이름이 다르다. 먼저 값이 있는 것을 쓴다."""
    for n in names:
        v = row.get(n)
        if v:
            return str(v).strip()
    return ""


def bill_fields(row):
    return {
        "bill_no": _first(row, "BILL_NO"),
        "bill_id": _first(row, "BILL_ID"),
        "name": _first(row, "BILL_NAME", "BILL_NM"),
        "proposer": _first(row, "PROPOSER", "PPSR_NM", "RST_PROPOSER", "PPSR"),
        "date": _first(row, "PROPOSE_DT", "PPSL_DT", "PROPOSE_DATE")[:10],
        "committee": _first(row, "COMMITTEE", "CURR_COMMITTEE", "JRCMIT_NM"),
    }


def fetch_bill_page(key, endpoint, age, pindex, psize):
    """의안 목록 한 쪽. (행 목록, 총건수)."""
    url = ("https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=%d&pSize=%d&AGE=%s"
           % (endpoint, key, pindex, psize, age))
    data = api_get(url)          # 실패하면 예외 — 호출부가 센다
    if "RESULT" in data:         # INFO-200 = 더 없음
        return [], 0
    try:
        total = int(data[endpoint][0]["head"][0]["list_total_count"])
        rows = data[endpoint][1]["row"]
    except Exception:
        return [], 0
    return rows, total


# 지정 법 하나가 한 대(代) 국회에서 나올 수 있는 개정안 수. 2026-09 기준 22대
# 도로교통법이 157건이라 4쪽(400건)이면 넉넉하다.
BILL_NAME_MAX_PAGES = 4
BILL_NAME_PAGE_SIZE = 100


def bills_by_law_name(key, age, law, since):
    """지정 법 이름으로 직접 물어 since 이후 발의분만 돌려준다.

    쪽 순서를 모르므로 그 법의 의안을 다 받아 로컬에서 날짜로 거른다(한 법에 두세
    쪽이라 싸다). 제안일이 없으면 걸러낼 방법이 없으니 None — "이 경로로는 못
    한다"는 뜻이고, 호출부가 훑기로 물러선다.

    의원발의만 담긴 목록과 달리 여기에는 정부제출·위원장 대안도 함께 온다.
    """
    rows, dated = [], False
    for page in range(1, BILL_NAME_MAX_PAGES + 1):
        url = ("https://open.assembly.go.kr/portal/openapi/TVBPMBILL11?KEY=%s&Type=json&pIndex=%d&pSize=%d&AGE=%s&BILL_NAME=%s"
               % (key, page, BILL_NAME_PAGE_SIZE, age, urllib.parse.quote(law)))
        try:
            data = api_get(url)
        except Exception:
            return None
        if "RESULT" in data:          # INFO-200 = 더 없음
            break
        try:
            got = data["TVBPMBILL11"][1]["row"]
        except Exception:
            break
        if any(bill_fields(r)["date"] for r in got):
            dated = True
        rows += got
        if len(got) < BILL_NAME_PAGE_SIZE:
            break
        time.sleep(0.3)
    if rows and not dated:
        return None
    return [r for r in rows if bill_fields(r)["date"] >= since]


def collect_candidates(key, age, since):
    """지정 법 개정안 후보를 모은다. 이름으로 묻고, 안 되면 훑기로 물러선다."""
    by_name, ok = [], True
    for law in BILL_WATCH_LAWS:
        rows = bills_by_law_name(key, age, law, since)
        if rows is None:
            log("'%s' 이름 검색이 안 된다 — 최근 발의분 훑기로 물러선다" % law)
            ok = False
            break
        by_name += rows
    if ok and by_name:
        log("지정 법 이름 검색: %s~ 발의분 %d건" % (since, len(by_name)))
        return by_name
    if ok:
        log("지정 법 이름 검색이 빈손이다 — 훑기로 확인한다")
    return sweep_recent_bills(key, age, since)


def sweep_one(key, endpoint, age, since, req_size):
    """한 엔드포인트·한 쪽 크기로 훑어 본다. 못 하면 빈 목록.

    쪽 순서가 오래된 것부터인지 새 것부터인지는 문서에 없다. 그래서 첫 쪽과 마지막
    쪽의 제안일을 비교해 새 쪽이 어디인지 알아낸 뒤 그쪽부터 걸어간다.
    """
    try:
        first_rows, total = fetch_bill_page(key, endpoint, age, 1, req_size)
    except Exception:
        log("%s 조회 실패(쪽 크기 %d)" % (endpoint, req_size))
        return []
    if not first_rows:
        return []
    if not any(bill_fields(r)["date"] for r in first_rows):
        log("%s 응답에 제안일이 없다 — 이걸로는 기간 훑기를 못 한다" % endpoint)
        return []

    psize = len(first_rows)              # 요청보다 적게 주는 경우가 있다
    last_page = max(1, (total + psize - 1) // psize)
    cache = {1: first_rows}
    newest_first = True
    if last_page > 1:
        try:
            last_rows, _ = fetch_bill_page(key, endpoint, age, last_page, req_size)
        except Exception:
            last_rows = []
        if last_rows:
            cache[last_page] = last_rows
            newest_first = (max(bill_fields(r)["date"] for r in first_rows)
                            >= max(bill_fields(r)["date"] for r in last_rows))
        else:
            log("%s 마지막 쪽을 못 읽었다 — 앞쪽이 최신이라고 보고 진행한다" % endpoint)

    pages = list(range(1, last_page + 1)) if newest_first else list(range(last_page, 0, -1))
    out, walked = [], 0
    for p in pages:
        if walked >= BILL_SWEEP_MAX_PAGES:
            log("훑기 상한 %d쪽에 걸렸다 — 더 오래된 쪽은 다음 실행에서 본다" % BILL_SWEEP_MAX_PAGES)
            break
        rows = cache.get(p)
        if rows is None:
            try:
                rows, _ = fetch_bill_page(key, endpoint, age, p, req_size)
            except Exception:
                break
            time.sleep(0.3)
        walked += 1
        fresh = [r for r in rows if bill_fields(r)["date"] >= since]
        out += fresh
        if len(fresh) < len(rows):       # 이 쪽에서 기간 밖으로 넘어갔다
            break
    if out:
        log("최근 %d일(%s~) 발의 훑기: %s 에서 %d건, 쪽당 %d건으로 %d쪽 읽음%s"
            % (BILL_SWEEP_DAYS, since, endpoint, len(out), psize, walked,
               "" if newest_first else " (뒤쪽이 최신)"))
    return out


def sweep_recent_bills(key, age, since):
    """since 이후에 발의된 의안을 훑는다. 이름 필터의 부분일치 여부에 기대지 않는다."""
    for endpoint in BILL_LIST_ENDPOINTS:
        for req_size in BILL_SWEEP_PAGE_SIZES:
            rows = sweep_one(key, endpoint, age, since, req_size)
            if rows:
                return rows
    log("최근 발의 훑기 실패 — 의안 목록을 못 받았다")
    return []


def bill_summary(key, bill_no):
    """제안이유·주요내용. 못 받으면 None(모른다), 등록이 안 됐으면 빈 문자열."""
    url = ("https://open.assembly.go.kr/portal/openapi/BPMBILLSUMMARY?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_NO=%s"
           % (key, urllib.parse.quote(str(bill_no))))
    try:
        data = api_get(url)
    except Exception:
        return None
    if "RESULT" in data:          # INFO-200 = 그 의안 요약이 없다
        return ""
    try:
        rows = data["BPMBILLSUMMARY"][1]["row"]
    except Exception:
        return None
    return " ".join(str(r.get("SUMMARY") or "") for r in rows)


def pm_terms_in(text):
    return ([t for t in BILL_PM_TERMS_DIRECT if t in text],
            [t for t in BILL_PM_TERMS_NEAR if t in text])


def excerpt_around(text, term, width=130):
    i = text.find(term)
    if i < 0:
        return " ".join(text[:width].split())
    start = max(0, i - width // 3)
    return (("…" if start else "") + " ".join(text[start:i + width].split()) + "…")


def search_new_bills(key, age="22", known=(), skip=()):
    """PM에 걸리는 새 의안을 찾는다. ({의안번호: 정보}, 걸러낸 의안번호 목록).

    known  이미 추적 중인 의안번호 — 건너뛴다.
    skip   지난 실행에서 제안이유를 읽고 PM 얘기가 아니라고 판정한 의안번호.
           같은 본문을 매일 다시 받지 않으려고 기억해 둔다.
    """
    known = set(str(k) for k in known)
    skip = set(str(k) for k in skip)
    found, rejected = {}, []

    # 1) 의안명에 PM 말이 직접 들어가는 것 — 본문을 안 봐도 된다.
    for kw in BILL_NAME_KEYWORDS:
        url = ("https://open.assembly.go.kr/portal/openapi/TVBPMBILL11?KEY=%s&Type=json&pIndex=1&pSize=100&AGE=%s&BILL_NAME=%s"
               % (key, age, urllib.parse.quote(kw)))
        try:
            data = api_get(url)
            rows = data.get("TVBPMBILL11", [None, None])[1]["row"]
        except Exception:
            continue
        for r in rows:
            f = bill_fields(r)
            if not f["bill_no"] or f["bill_no"] in known:
                continue
            f["tier"], f["why"], f["excerpt"] = "name", "의안명에 '%s'" % kw, ""
            found[f["bill_no"]] = f

    # 2) 지정 법 개정안 — 최근 발의분을 훑어 후보를 고르고, 제안이유로 거른다.
    since = (now_kst() - timedelta(days=BILL_SWEEP_DAYS)).strftime("%Y-%m-%d")
    budget = BILL_SUMMARY_BUDGET
    for r in collect_candidates(key, age, since):
        f = bill_fields(r)
        no = f["bill_no"]
        if not no or no in known or no in skip or no in found:
            continue
        law = next((w for w in BILL_WATCH_LAWS if f["name"].startswith(w)), None)
        if not law:
            continue
        if budget <= 0:
            log("제안이유 조회 상한(%d건)에 걸렸다 — 남은 후보는 다음 실행에서 본다"
                % BILL_SUMMARY_BUDGET)
            break
        budget -= 1
        text = bill_summary(key, no)
        time.sleep(0.3)
        if text is None:
            continue          # 못 읽었다 — 판정하지 않는다. 걸러낸 목록에도 넣지 않는다.
        if not text:
            # 발의 직후에는 제안이유가 아직 안 올라온 경우가 있다. 지정 법 개정안이니
            # 일단 올리고 사람이 본다 — 여기서 버리면 다시 볼 기회가 없다.
            f["tier"], f["why"], f["excerpt"] = "law", "%s 개정안 (제안이유 미등록 — 확인 필요)" % law, ""
            found[no] = f
            continue
        direct, near = pm_terms_in(text)
        if direct:
            f["tier"] = "direct"
            f["why"] = "%s 개정안 · 본문에 %s" % (law, ", ".join(direct[:3]))
            f["excerpt"] = excerpt_around(text, direct[0])
        elif near:
            f["tier"] = "near"
            f["why"] = "%s 개정안 · 본문에 %s (인접어)" % (law, ", ".join(near[:3]))
            f["excerpt"] = excerpt_around(text, near[0])
        else:
            rejected.append(no)
            continue
        found[no] = f

    if rejected:
        log("지정 법 개정안이지만 제안이유에 PM 얘기가 없어 거른 것 %d건" % len(rejected))
    return found, rejected


def bill_by_no(key, bill_no):
    """ALLBILL 에 아직 없는 의안을 의안 목록 쪽에서 찾아 본다.

    갓 접수된 의안은 ALLBILL 에 며칠 늦게 올라온다(2026-09-16 에 2221403 이 그랬다).
    BILL_NO 필터가 먹는지 확인되지 않았으므로 받아 온 행의 의안번호가 정말 물어본
    번호인지 확인한다 — 필터가 무시되면 엉뚱한 의안을 그 번호로 넣게 된다.
    """
    want = str(bill_no)
    for endpoint in BILL_LIST_ENDPOINTS:
        url = ("https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_NO=%s"
               % (endpoint, key, urllib.parse.quote(want)))
        try:
            data = api_get(url)
        except Exception:
            continue
        if "RESULT" in data:
            continue
        try:
            rows = data[endpoint][1]["row"]
        except Exception:
            continue
        for r in rows:
            if bill_fields(r)["bill_no"] == want:
                log("지정 의안 %s 을 %s 에서 찾았다 (ALLBILL 에는 아직 없다)" % (want, endpoint))
                return r
    return None


def resolve_seed_bills(key, seeds, known, found):
    """사람이 번호로 찍어 준 의안을 받아 온다.

    본문 판정과 상관없이 추적에 넣는다 — 번호를 직접 넣었다는 건 이미 사람이
    보고 판단했다는 뜻이다. 받아 온 것만 seeds 에서 빠진다. 못 받으면 남겨
    두고 다음 실행에서 다시 본다(API 가 죽은 날 영영 잃지 않도록).
    """
    resolved = []
    for no in list(seeds):
        no = str(no)
        if no in known or no in found:
            resolved.append(no)
            continue
        row = bill_detail(key, no) or bill_by_no(key, no)
        if row is None:
            log("지정 의안 %s 을 못 받았다 — 다음 실행에서 다시 본다" % no)
            continue
        f = bill_fields(row)
        f["bill_no"] = f["bill_no"] or no
        f["tier"], f["why"], f["excerpt"] = "seed", "사람이 번호로 지정한 의안", ""
        found[f["bill_no"]] = f
        resolved.append(no)
    return resolved

PARTY_FIELDS = ["PLPT_NM", "POLY_NM", "PARTY_NM"]


def extract_party_raw(row):
    """정당 필드 원본을 그대로 돌려준다.

    값이 '더불어민주당/무소속'처럼 슬래시로 이어진 이력일 때 어느 쪽이 현재인지는
    확인하지 못했다(2026-08 시점, API 지연으로 원본 표본 확보 실패). 그래서 여기서
    현재 정당을 골라내려 하지 않고 문자열 전체를 저장·비교한다. 이력이 어떤 순서든
    변동이 생기면 문자열이 달라지므로 감지 자체는 정확하고, 어느 쪽으로 바뀐 건지는
    알림에 원본을 그대로 실어 사람이 판단한다.
    """
    for f in PARTY_FIELDS:
        raw = row.get(f)
        if raw:
            return raw.strip()
    return ""


def check_member_moves(key, member_snapshot):
    changes = []
    for name, info in member_snapshot.items():
        enc = urllib.parse.quote(name)
        url = "https://open.assembly.go.kr/portal/openapi/ALLNAMEMBER?KEY=%s&Type=json&pIndex=1&pSize=5&NAAS_NM=%s" % (key, enc)
        try:
            data = api_get(url)
            rows = data.get("ALLNAMEMBER", [None, None])[1]["row"]
        except Exception:
            continue
        chosen = None
        for r in rows:
            if "22" in (r.get("GTELT_ERACO") or ""):
                chosen = r
                break
        if not chosen and rows:
            chosen = rows[0]
        new_committee = (chosen.get("CMIT_NM") or "").split("/")[-1].strip() if chosen else ""
        new_active = bool(new_committee)
        old_committee = info.get("committee", "")
        old_active = info.get("active", False)
        if old_active and not new_active:
            changes.append({"type": "member_seat_lost", "name": name, "old_committee": old_committee})
        elif new_committee != old_committee:
            changes.append({"type": "member_committee_change", "name": name,
                             "old_committee": old_committee, "new_committee": new_committee})
        info["committee"] = new_committee
        info["active"] = new_active

        # 정당 변동(탈당·입당·제명·합당). 스냅샷에 party가 아직 없는 첫 실행에서는
        # 전원이 변경으로 잡히므로, 값만 심어두고 알리지 않는다.
        new_party = extract_party_raw(chosen) if chosen else ""
        old_party = info.get("party")
        if new_party:
            if old_party and new_party != old_party:
                changes.append({"type": "member_party_change", "name": name,
                                 "old_party": old_party, "new_party": new_party})
            info["party"] = new_party
    return changes

def check_bill_meetings(key, snapshot):
    changes = []
    for bill_no, info in snapshot["bills"].items():
        bill_id = info.get("bill_id")
        if not bill_id:
            continue
        url = "https://open.assembly.go.kr/portal/openapi/VCONFBILLCONFLIST?KEY=%s&Type=json&pIndex=1&pSize=30&BILL_ID=%s" % (key, bill_id)
        try:
            data = api_get(url)
            rows = data.get("VCONFBILLCONFLIST", [None, None])[1]["row"]
        except Exception:
            continue
        seen = set(info.get("seen_conf_ids", []))
        for r in rows:
            conf_id = r.get("CONF_ID")
            if conf_id and conf_id not in seen:
                changes.append({
                    "type": "bill_new_meeting", "bill_no": bill_no, "name": info["name"],
                    "conf_id": conf_id, "conf_knd": r.get("CONF_KND"),
                    "sess": r.get("SESS"), "dgr": r.get("DGR"), "date": (r.get("CONF_DT") or "").strip(),
                })
                seen.add(conf_id)
        info["seen_conf_ids"] = sorted(seen)
    return changes

def extract_pdf_text(pdf_url):
    req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)

def find_speaker_near(text, idx):
    start = text.rfind("◯", 0, idx)
    if start == -1:
        return None
    line_end = text.find("\n", start)
    if line_end == -1 or line_end > start + 40:
        line_end = start + 40
    speaker_chunk = text[start+1:line_end].strip()
    m = re.match(r'^([가-힣]{2,4})\s*' + TITLE_SUFFIX + r'(?!\S)', speaker_chunk)
    if not m:
        return None
    name = m.group(1)
    if name in ("소위원장", "위원장"):
        rest = speaker_chunk[m.end():].strip()
        nm = NAME_RE.match(rest)
        if nm and re.fullmatch(r'[가-힣]{2,4}', nm.group(1)):
            return nm.group(1)
        return None
    if not re.fullmatch(r'[가-힣]{2,4}', name):
        return None
    return name

MEETING_ENDPOINTS = [
    ("VCONFSUBCCONFLIST", "VCONFSUBCCONFLIST", "소위", ""),
    ("VCONFAPIGCONFLIST", "VCONFAPIGCONFLIST", "국정감사", "&ERACO=" + urllib.parse.quote("제22대")),
]

def check_new_meetings(key, snapshot):
    last_scan = snapshot.get("last_meeting_scan", "2000-01-01")
    changes = []
    newest_date = last_scan
    for endpoint, result_key, kind_label, extra_params in MEETING_ENDPOINTS:
        url = "https://open.assembly.go.kr/portal/openapi/%s?KEY=%s&Type=json&pIndex=1&pSize=50%s" % (endpoint, key, extra_params)
        try:
            data = api_get(url)
            rows = data.get(result_key, [None, None])[1]["row"]
        except Exception as e:
            log("회의록 목록 조회 실패 (%s): %s" % (kind_label, e))
            continue
        for r in rows:
            conf_dt = r.get("CONF_DT") or ""
            if conf_dt > newest_date:
                newest_date = conf_dt
            if conf_dt <= last_scan:
                continue
            down_url = r.get("DOWN_URL")
            if not down_url:
                continue
            try:
                text = extract_pdf_text(down_url)
            except Exception as e:
                log("PDF 추출 실패 (%s): %s" % (r.get("CONF_ID"), e))
                continue
            total_hits = 0
            speakers = set()
            for kw in MEETING_KEYWORDS:
                for m in re.finditer(re.escape(kw), text):
                    total_hits += 1
                    speaker = find_speaker_near(text, m.start())
                    if speaker:
                        speakers.add(speaker)
            if total_hits:
                changes.append({
                    "type": "new_meeting_hit", "kind": kind_label,
                    "conf_id": r.get("CONF_ID"), "committee": r.get("CMIT_NM"),
                    "sub_committee": r.get("SB_CMIT_NM"), "date": conf_dt,
                    "speakers": sorted(speakers),
                })
    return changes, newest_date

def fetch_schedule_rows(key):
    """관심 구간을 하루씩 끊어서 조회하고 (행 목록, 성공 여부)를 돌려준다.

    ALLSCHEDULE은 SCH_DT 필터를 지원한다(하루치가 20건 안팎이라 한 번에 다 온다).
    쪽 단위로 긁던 방식은 이 API의 정렬이 SCH_DT 내림차순이라 위험했다 — 먼 미래
    세미나가 등록될수록 앞으로 2주치가 1쪽 밖으로 밀려서, 조회는 성공하는데
    관심 일정만 0건이 되는 날이 생겼다(2026-08-12·14). 날짜로 직접 물으면
    정렬과 무관하게 항상 그날 전부를 받는다.

    CMIT_NM·UNIT_CD 필터는 무시되므로(전체 건수가 그대로 나온다) 위원회 추림은
    받아온 뒤에 한다.
    """
    rows = []
    for i in range(SCHEDULE_HORIZON_DAYS + 1):
        day = (now_kst() + timedelta(days=i)).strftime("%Y-%m-%d")
        url = ("https://open.assembly.go.kr/portal/openapi/ALLSCHEDULE"
               "?KEY=%s&Type=json&pIndex=1&pSize=%d&SCH_DT=%s"
               % (key, SCHEDULE_DAY_PAGE_SIZE, day))
        try:
            data = api_get(url)
        except Exception as e:
            # 하루라도 못 읽으면 이번 실행은 판단하지 않는다. 반쪽짜리 목록으로
            # 기억을 갱신하면 못 본 일정이 다음 실행에서 '새 일정'으로 튄다.
            log("일정 조회 실패(%s): %s" % (day, e))
            return rows, False
        body = data.get("ALLSCHEDULE")
        if body is None:
            code = (data.get("RESULT") or {}).get("CODE", "")
            if code.startswith("INFO-200"):
                continue  # 그날은 일정이 없다 — 정상이다
            log("일정 조회 응답 이상(%s): %s" % (day, code or data))
            return rows, False
        try:
            day_rows = body[1]["row"]
        except Exception as e:
            log("일정 조회 응답 파싱 실패(%s): %s" % (day, e))
            return rows, False
        if len(day_rows) >= SCHEDULE_DAY_PAGE_SIZE:
            # 하루가 한 쪽을 넘긴 적은 없지만, 넘긴다면 뒷부분을 통째로 놓치게 된다.
            log("!!! %s 일정이 %d건 — 한 쪽 한도라 잘렸을 수 있다" % (day, len(day_rows)))
        rows.extend(day_rows)
        time.sleep(SCHEDULE_DAY_DELAY_SEC)  # API를 몰아치지 않는다
    return rows, True


def check_schedule(key, snapshot):
    rows, ok = fetch_schedule_rows(key)
    if not ok:
        log("일정 확인 건너뜀 — 조회가 실패해 목록을 믿을 수 없다(%d건까지만 조회)" % len(rows))
        return [], []
    today = now_kst().strftime("%Y-%m-%d")
    horizon = (now_kst() + timedelta(days=SCHEDULE_HORIZON_DAYS)).strftime("%Y-%m-%d")
    relevant = []
    for r in rows:
        kind = r.get("SCH_KIND")
        cmit = (r.get("CMIT_NM") or "").strip()
        dt = (r.get("SCH_DT") or "").strip()
        if not dt or dt < today or dt > horizon:
            continue
        if kind == "본회의" or (kind == "위원회" and cmit in WATCHED_SCHEDULE_COMMITTEES):
            relevant.append(r)
    seen = set(snapshot.get("seen_schedule_keys", []))
    new_items, today_items, all_keys = [], [], set()
    for r in relevant:
        cmit = (r.get("CMIT_NM") or "").strip() or "본회의"
        dt = (r.get("SCH_DT") or "").strip()
        key_str = "%s|%s|%s" % (dt, cmit, r.get("SCH_CN"))
        all_keys.add(key_str)
        entry = {"date": dt, "committee": cmit, "content": r.get("SCH_CN"), "sess": r.get("CONF_SESS")}
        if key_str not in seen:
            new_items.append(entry)
        if dt == today:
            today_items.append(entry)
    # 통째로 덮어쓰지 않는다. 어떤 날 조회 결과에 특정 일정이 빠지면 기억에서도 지워져
    # 다음 날 같은 일정이 '새 일정'으로 다시 알려졌다(2026-08-15 중복 알림).
    # 지난 날짜만 덜어내고 이번에 본 것을 더한다 — 이러면 한 번 덜 본 날이 있어도
    # 중복 알림이 아니라 '늦은 알림'으로 끝난다.
    kept = {k for k in seen if k.split("|", 1)[0] >= today}
    snapshot["seen_schedule_keys"] = sorted(kept | all_keys)
    log("일정 %d건 조회 — 관심 일정 %d건, 신규 %d건, 오늘 %d건"
        % (len(rows), len(relevant), len(new_items), len(today_items)))
    return new_items, today_items

def send_slack(webhook_url, text):
    if not webhook_url:
        log("SLACK_WEBHOOK_URL 환경변수 없음 - 알림 전송 생략")
        return
    payload = {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                # 공개 페이지를 주 링크로 둔다 — master에 머지되면 자동 배포되므로
                # 항상 최신이다. 아티팩트는 재발행과 공유 핀 이동을 사람이 해야 해서
                # 실제로 구버전이 팀에 노출된 적이 있다.
                {"type": "button", "text": {"type": "plain_text", "text": "🌐 트래커 열기"}, "url": GITHUB_PAGES_URL, "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "🛴 아티팩트(수동 갱신)"}, "url": ARTIFACT_URL},
            ]},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        urllib.request.urlopen(req, timeout=10)
        log("슬랙 알림 전송 완료")
    except Exception as e:
        log("슬랙 알림 전송 실패: %s" % e)


def main():
    key = os.environ.get("ASSEMBLY_API_KEY")
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not key:
        log("ASSEMBLY_API_KEY 환경변수가 없습니다. 종료.")
        sys.exit(1)

    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        snapshot = json.load(f)

    changes = []

    log("=== 진행단계 재조회 시작 (%d건) ===" % len(snapshot["bills"]))
    for bill_no, info in snapshot["bills"].items():
        new_stage = get_stage(key, bill_no)
        if new_stage is None:
            continue
        if new_stage != info["stage"]:
            changes.append({"type": "stage_change", "bill_no": bill_no, "name": info["name"],
                             "old_stage": info["stage"], "new_stage": new_stage})
            info["stage"] = new_stage

    log("=== 신규 의안 검색 ===")
    known = snapshot["known_bill_nos"]
    # 제안이유를 읽고 PM 얘기가 아니라고 판정한 의안. 같은 본문을 매일 다시 받지
    # 않으려고 기억해 둔다. 훑기 구간(60일)보다 길게 들고 있을 이유는 없지만,
    # 번호만이라 가볍다.
    not_pm = snapshot.setdefault("not_pm_bill_nos", [])
    found, rejected = search_new_bills(key, known=known, skip=not_pm)

    # 사람이 번호로 찍어 준 의안. 본문 판정과 상관없이 추적에 넣는다.
    seeds = snapshot.setdefault("seed_bill_nos", [])
    for no in resolve_seed_bills(key, seeds, set(str(k) for k in known), found):
        if no in seeds:
            seeds.remove(no)

    for bill_no, info_found in found.items():
        if bill_no not in known:
            name = info_found["name"]
            bill_id = info_found.get("bill_id", "")
            changes.append({"type": "new_bill", "bill_no": bill_no, "name": name,
                             "why": info_found.get("why", ""), "tier": info_found.get("tier", ""),
                             "proposer": info_found.get("proposer", ""),
                             "date": info_found.get("date", ""),
                             "excerpt": info_found.get("excerpt", "")})
            known.append(bill_no)
            seen_conf_ids = []
            if bill_id:
                try:
                    conf_data = api_get("https://open.assembly.go.kr/portal/openapi/VCONFBILLCONFLIST?KEY=%s&Type=json&pIndex=1&pSize=30&BILL_ID=%s" % (key, bill_id))
                    conf_rows = conf_data.get("VCONFBILLCONFLIST", [None, None])[1]["row"]
                    seen_conf_ids = sorted(set(r.get("CONF_ID") for r in conf_rows if r.get("CONF_ID")))
                except Exception:
                    pass
            snapshot["bills"][bill_no] = {"name": name, "stage": get_stage(key, bill_no) or "정보없음",
                                            "committee": info_found.get("committee") or "(미확인)",
                                            "bill_id": bill_id, "seen_conf_ids": seen_conf_ids}

    # 걸러낸 것은 다음 실행에서 건너뛰도록 기억한다. 건전성 판정을 통과한 뒤에만
    # 저장되므로(아래), 실패한 실행의 판정이 기준선이 되는 일은 없다.
    if rejected:
        not_pm.extend(n for n in rejected if n not in not_pm)
        del not_pm[:-2000]

    snapshot["last_full_scan"] = now_kst().strftime("%Y-%m-%d")

    log("=== 의원 위원회 이동/직 상실 확인 ===")
    member_snapshot = None
    if os.path.exists(MEMBER_SNAPSHOT_PATH):
        with open(MEMBER_SNAPSHOT_PATH, encoding="utf-8") as f:
            member_snapshot = json.load(f)
        changes.extend(check_member_moves(key, member_snapshot))
        # 저장은 아래 건전성 판정을 통과한 뒤에 한다.

    log("=== 신규 회의록 키워드 스캔 ===")
    meeting_changes, newest_date = check_new_meetings(key, snapshot)
    changes.extend(meeting_changes)
    snapshot["last_meeting_scan"] = newest_date

    log("=== 추적 의안별 신규 상정 회의 확인 ===")
    changes.extend(check_bill_meetings(key, snapshot))

    log("=== 검토보고서 확인 ===")
    # 국회 Open API 에는 검토보고서가 없어 의안정보시스템 페이지에서 읽는다.
    # 남의 사이트라 언제든 깨질 수 있으므로 실패가 이 실행 전체를 멈추지 않게 한다 —
    # 다만 '못 읽었다'는 로그로 남긴다. 조용히 0건으로 넘어가면 안 된다.
    try:
        sys.path.insert(0, BASE_DIR)
        import review_watch
        report_changes, report_checked, report_unread = review_watch.check_reports(snapshot, log=log)
        changes.extend(report_changes)
        log("검토보고서: 의안 %d건 확인, 새 문서 %d건, 못 읽음 %d건"
            % (report_checked, len(report_changes), len(report_unread)))
    except Exception as e:
        log("검토보고서 확인 실패(%s) — 이번 실행은 건너뛴다" % e)

    log("=== 국토위/행안위/법사위/본회의 일정 확인 ===")
    new_schedule_items, today_schedule_items = check_schedule(key, snapshot)
    for s in new_schedule_items:
        changes.append({"type": "new_schedule", **s})

    # --- 실행 건전성 판정 ---
    # 조회가 절반 이상 실패했다면 이번 실행으로 본 것은 신뢰할 수 없다. 이때 상태를
    # 저장해 버리면 실패한 구간의 값이 다음 실행의 기준선이 되어, 그 사이 일어난
    # 변경은 영영 잡히지 않는다. 그래서 아무것도 쓰지 않고 그대로 끝낸다 —
    # 감지는 전부 "저장된 상태와의 비교"라 저장을 미루면 놓치는 게 아니라 미뤄질 뿐이고,
    # 다음 정상 실행이 밀린 것까지 한꺼번에 잡는다.
    ratio, summary = api_health()
    if ratio >= FAILURE_ABORT_RATIO:
        log("!!! 국회 API 조회 실패율 과다 — %s" % summary)
        log("!!! 이번 실행은 신뢰할 수 없어 상태 파일을 갱신하지 않고 종료합니다.")
        log("!!! (슬랙 알림은 보내지 않습니다. Actions 실행이 실패로 표시됩니다.)")
        print("API_FAILED=true")
        print("HAS_CHANGES=false")
        sys.exit(1)
    log("API 조회 상태: %s" % summary)

    if member_snapshot is not None:
        with open(MEMBER_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(member_snapshot, f, ensure_ascii=False, indent=2)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    # 변경이 없으면 슬랙을 보내지 않는다. 오늘 일정만 있는 날은 이미 그 일정을
    # 등록 시점에 '새 일정'으로 알렸으므로 당일 재알림은 중복이다.
    if changes:
        log("변경 사항 %d건, 오늘 일정 %d건" % (len(changes), len(today_schedule_items)))
        pending = []
        if os.path.exists(PENDING_PATH):
            with open(PENDING_PATH, encoding="utf-8") as f:
                pending = json.load(f)
        pending.append({"checked_at": now_kst().isoformat(), "changes": changes})
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)

        lines = ["*PM 법안 트래커 업데이트 (클라우드 루틴)* (%s)" % now_kst().strftime("%Y-%m-%d")]
        lines.append("")
        lines.append("변경 사항:")
        for c in changes:
            if c["type"] == "new_bill":
                head = "• 🆕 새 의안 발견 — [%s] %s" % (c["bill_no"], c["name"])
                if c.get("proposer") or c.get("date"):
                    head += "\n   %s %s" % (c.get("proposer", ""), c.get("date", ""))
                if c.get("why"):
                    head += "\n   걸린 이유: %s" % c["why"]
                if c.get("excerpt"):
                    head += "\n   > %s" % c["excerpt"]
                lines.append(head)
            elif c["type"] == "stage_change":
                lines.append("• 🔄 [%s] %s\n   %s → %s" % (c["bill_no"], c["name"], c["old_stage"], c["new_stage"]))
            elif c["type"] == "member_seat_lost":
                lines.append("• 🚪 %s 의원 — 의원직/소속위원회 정보 소실 (직 상실 가능성, 확인 필요)" % c["name"])
            elif c["type"] == "member_committee_change":
                lines.append("• 🔀 %s 의원 — 소속위원회 변경: %s → %s" % (c["name"], c["old_committee"] or "(없음)", c["new_committee"] or "(없음)"))
            elif c["type"] == "member_party_change":
                lines.append("• 🏳️ %s 의원 — 정당 이력 변경: `%s` → `%s`\n   (API 원본 그대로입니다. 페이지 정당 표기를 확인·정정해 주세요)" % (c["name"], c["old_party"], c["new_party"]))
            elif c["type"] == "new_meeting_hit":
                where = c["committee"] + (" " + c["sub_committee"] if c.get("sub_committee") else " " + c["kind"])
                if c["speakers"]:
                    who = ", ".join(c["speakers"][:3]) + ("의원 등이" if len(c["speakers"]) > 1 else " 의원이")
                    lines.append("• 📄 [%s, %s] %sPM 관련 언급" % (where, c["date"], who + " "))
                else:
                    lines.append("• 📄 [%s, %s] PM 관련 언급 있음(발언자 특정 안 됨)" % (where, c["date"]))
            elif c["type"] == "bill_new_meeting":
                lines.append("• 🏛️ [%s] %s — 새로 상정됨 (%s %s, %s)" % (c["bill_no"], c["name"], c["sess"], c["dgr"], c["date"]))
            elif c["type"] == "new_report":
                lines.append("• 📑 검토보고서 — [%s] %s\n   %s\n   %s"
                             % (c["bill_no"], c["name"], c["title"][:90], c["url"]))
            elif c["type"] == "new_schedule":
                lines.append("• 🗓️ 새 일정 — %s, %s: %s" % (c["date"], c["committee"], c["content"]))
        lines.append("")
        lines.append("Claude Code에서 \"PM 트래커 업데이트 반영해줘\"라고 요청하면 위 변경사항이 페이지에 반영됩니다.")

        if today_schedule_items:
            lines.append("")
            lines.append("오늘 일정:")
            for s in today_schedule_items:
                lines.append("• %s — %s" % (s["committee"], s["content"]))

        send_slack(webhook, "\n".join(lines))
        print("HAS_CHANGES=true")
    else:
        log("변경 사항 없음")
        print("HAS_CHANGES=false")

if __name__ == "__main__":
    main()
