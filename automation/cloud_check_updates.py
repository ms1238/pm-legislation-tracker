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
# ALLSCHEDULE은 전체가 9만 건이 넘어서 다 읽을 수 없다(다 읽으려 3만 건을 긁었더니
# 직후 호출이 전부 타임아웃했다 — 세게 긁으면 막힌다). 한 쪽은 작게 두고, 관심 구간
# 행이 안 나오는 쪽이 연속으로 나오면 멈춘다.
SCHEDULE_PAGE_SIZE = 300
SCHEDULE_MAX_PAGES = 8
SCHEDULE_EMPTY_PAGE_STOP = 2
SCHEDULE_PAGE_DELAY_SEC = 0.5

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


def api_get(url):
    global API_ATTEMPTS, API_FAILURES
    API_ATTEMPTS += 1
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        API_FAILURES += 1
        raise


def api_health():
    """(실패율, 요약문자열)을 돌려준다. 호출이 아예 없었으면 실패율 0으로 본다."""
    if API_ATTEMPTS == 0:
        return 0.0, "API 호출 없음"
    ratio = API_FAILURES / API_ATTEMPTS
    return ratio, "%d건 중 %d건 실패 (%.0f%%)" % (API_ATTEMPTS, API_FAILURES, ratio * 100)

def get_stage(key, bill_no):
    url = "https://open.assembly.go.kr/portal/openapi/ALLBILL?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_NO=%s" % (key, bill_no)
    try:
        data = api_get(url)
        row = data.get("ALLBILL", [None, None])[1]["row"][0]
    except Exception:
        return None
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

def search_new_bills(key, age="22"):
    keywords = ["개인형 이동", "퍼스널모빌리티", "전동킥보드", "킥라니"]
    found = {}
    for kw in keywords:
        enc = urllib.parse.quote(kw)
        url = "https://open.assembly.go.kr/portal/openapi/TVBPMBILL11?KEY=%s&Type=json&pIndex=1&pSize=50&AGE=%s&BILL_NAME=%s" % (key, age, enc)
        try:
            data = api_get(url)
            rows = data.get("TVBPMBILL11", [None, None])[1]["row"]
        except Exception:
            continue
        for r in rows:
            found[r["BILL_NO"]] = {"name": r["BILL_NAME"], "bill_id": r.get("BILL_ID", "")}
    return found

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
    """관심 구간(오늘~horizon)을 덮을 만큼만 ALLSCHEDULE을 읽어서 (행 목록, 성공 여부)를 돌려준다.

    전체는 9만 건이 넘어서 다 읽을 수 없다. 실제로 다 읽으려고 30쪽(3만 건)을 긁었더니
    그 직후 같은 키의 호출이 12건 전부 타임아웃했다 — 세게 긁으면 API가 막힌다.
    그래서 반대로 간다: 한 쪽은 작게(300건) 두고, 관심 구간 행이 더 안 나오면 멈춘다.

    이 API는 날짜순이 아니라 등록/수정 최신순으로 내려주는 것으로 보인다(1쪽 300건이
    2026-07-30~09-09에 걸쳐 있었다). 그래서 관심 구간 행이 한 쪽에 몰려 있지 않을 수
    있어, 빈 쪽이 연속 2번 나올 때까지는 더 읽는다.
    """
    today = now_kst().strftime("%Y-%m-%d")
    horizon = (now_kst() + timedelta(days=SCHEDULE_HORIZON_DAYS)).strftime("%Y-%m-%d")
    rows, empty_streak = [], 0
    for page in range(1, SCHEDULE_MAX_PAGES + 1):
        url = ("https://open.assembly.go.kr/portal/openapi/ALLSCHEDULE"
               "?KEY=%s&Type=json&pIndex=%d&pSize=%d" % (key, page, SCHEDULE_PAGE_SIZE))
        try:
            data = api_get(url)
        except Exception as e:
            # 한 쪽이라도 실패하면 이번 실행은 판단하지 않는다. 반쪽짜리 목록으로 기억을
            # 갱신하면 못 본 일정이 다음 실행에서 통째로 '새 일정'으로 튄다.
            log("일정 조회 실패(%d쪽): %s" % (page, e))
            return rows, False
        body = data.get("ALLSCHEDULE")
        if body is None:
            # 마지막 쪽을 지나면 INFO-200(데이터 없음)이 온다. 고장이 아니라 끝이다.
            code = (data.get("RESULT") or {}).get("CODE", "")
            if code.startswith("INFO-200"):
                return rows, True
            log("일정 조회 응답 이상(%d쪽): %s" % (page, code or data))
            return rows, False
        try:
            page_rows = body[1]["row"]
        except Exception as e:
            log("일정 조회 응답 파싱 실패(%d쪽): %s" % (page, e))
            return rows, False
        rows.extend(page_rows)
        hits = sum(1 for r in page_rows
                   if today <= (r.get("SCH_DT") or "").strip() <= horizon)
        empty_streak = 0 if hits else empty_streak + 1
        if len(page_rows) < SCHEDULE_PAGE_SIZE:
            return rows, True
        if empty_streak >= SCHEDULE_EMPTY_PAGE_STOP:
            return rows, True
        time.sleep(SCHEDULE_PAGE_DELAY_SEC)  # API를 몰아치지 않는다
    log("일정 조회: 상한 %d쪽(%d건)까지 읽었다" % (SCHEDULE_MAX_PAGES, len(rows)))
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
    found = search_new_bills(key)
    for bill_no, info_found in found.items():
        if bill_no not in snapshot["known_bill_nos"]:
            name = info_found["name"]
            bill_id = info_found.get("bill_id", "")
            changes.append({"type": "new_bill", "bill_no": bill_no, "name": name})
            snapshot["known_bill_nos"].append(bill_no)
            seen_conf_ids = []
            if bill_id:
                try:
                    conf_data = api_get("https://open.assembly.go.kr/portal/openapi/VCONFBILLCONFLIST?KEY=%s&Type=json&pIndex=1&pSize=30&BILL_ID=%s" % (key, bill_id))
                    conf_rows = conf_data.get("VCONFBILLCONFLIST", [None, None])[1]["row"]
                    seen_conf_ids = sorted(set(r.get("CONF_ID") for r in conf_rows if r.get("CONF_ID")))
                except Exception:
                    pass
            snapshot["bills"][bill_no] = {"name": name, "stage": get_stage(key, bill_no) or "정보없음",
                                            "committee": "국토교통위원회", "bill_id": bill_id, "seen_conf_ids": seen_conf_ids}

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
                lines.append("• 🆕 새 의안 발견 — [%s] %s" % (c["bill_no"], c["name"]))
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
