# -*- coding: utf-8 -*-
"""입법예고 감시 — 킥보드·개인형 이동장치·자전거·전기자전거가 걸리면 알린다.

이 트래커의 나머지(cloud_check_updates.py)는 국회 의안을 본다. 여기는 정부 쪽
입법예고를 본다. 법안이 발의되기 전에, 부처가 시행령·시행규칙을 고치겠다고
예고하는 단계라서 먼저 알수록 의견제출 기간이 남는다.

두 갈래를 본다.
  정부 입법예고 — 국민참여입법센터. 본문(lmPpCts)이 오므로 본문 키워드로 판정한다.
  국회 입법예고 — 국회 Open API. 본문이 안 오므로 의안명과 지정 법 이름으로 판정한다.

쓰는 API (국민참여입법센터, OC 인증):

  목록  https://www.lawmaking.go.kr/rest/ogLmPpMod.xml?OC=..&diff=0
        요청변수: lsClsCd(법령종류) cptOfiOrgCd(소관부처) diff(0 진행/1 종료)
                  pntcNo·pntcNo2(공고번호) stYdFmt·edYdFmt(예고일자) lsNm(예고명)
        응답변수: ogLmPpSeq lsNm lsClsNm asndOfiNm pntcNo pntcDt stYd edYd
                  FileName FileDownLink modDt status readCnt mappingLbicId announceType

  본문  https://www.lawmaking.go.kr/rest/ogLmPpMod/{ogLmPpSeq}/{mappingLbicId}/{announceType}.xml?OC=..
        여기에 lmPpCts(예고 본문)가 있다. 목록에는 본문이 없다.

  목록이 상세 조회에 필요한 세 값을 그대로 준다(ogLmPpSeq·mappingLbicId·
  announceType). 그래서 화면을 긁을 일이 없다.

왜 본문까지 읽는가:
  제명(lsNm)만으로는 안 걸린다. 실측으로 '킥보드' 0건, '이동장치' 0건이다.
  PM 관련 개정은 제명이 "도로교통법 시행규칙 일부개정령안"이고, '개인형 이동장치'
  같은 말은 본문에만 나온다. 본문을 훑는 검색은 이 사이트에 없다(후보 주소가 전부
  404이거나 검색어를 무시했다). 그래서 본문은 우리가 받아서 우리가 판정한다.

비용:
  목록 1회로 진행중인 예고 전부의 번호가 온다. 본문은 아직 안 읽은 번호만 부른다.
  평소 한 번 실행 = 1 + (새로 올라온 건수). 첫 실행만 열려 있는 전부를 읽는다
  (--limit 으로 나눠 읽을 수 있다).

국회 입법예고 API (국회 Open API, 키 인증):
  목록  https://open.assembly.go.kr/portal/openapi/nknalejkafmvgzmpt
        응답변수: BILL_ID BILL_NO BILL_NAME AGE PROPOSER_KIND_CD CURR_COMMITTEE
                  NOTI_ED_DT LINK_URL PROPOSER CURR_COMMITTEE_ID
        제안이유·주요내용은 없다. BILL_NAME 필터는 부분일치가 안 된다(실측).

비밀값은 파일이 아니라 환경변수로 받는다(이 저장소는 퍼블릭이다):
      LAWMAKING_OC        국민참여입법센터 승인 아이디
      ASSEMBLY_API_KEY    국회 Open API 키 (없으면 국회 쪽은 건너뛴다)
      SLACK_WEBHOOK_URL   알림 보낼 채널
상태 파일(notice_state.json)은 이 폴더에 있고, 커밋·푸시는 호출하는 워크플로 몫이다.

인자:
  --dry-run   슬랙을 보내지 않고 상태 파일도 쓰지 않는다. 판정만 출력한다.
  --limit N   본문을 읽을 건수 상한(밀린 게 많을 때 나눠 읽는다).
"""
import json, os, re, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "notice_state.json")
# 페이지(index.html)의 "정부 입법예고" 탭이 읽어 가는 파일. 상태 파일과 달리
# 사람이 볼 내용이라 제명·부처·기간·발췌까지 담는다.
NOTICES_PATH = os.path.join(BASE_DIR, "notices.json")

REST = "https://www.lawmaking.go.kr/rest/ogLmPpMod"
DETAIL_PAGE = "https://opinion.lawmaking.go.kr/gcom/ogLmPp/%s"

UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25
DELAY_SEC = 0.4

# 목록은 기본 20건만 준다. pageSize·pageIndex는 문서에 없지만 실제로 먹는다.
PAGE_SIZE = 100
MAX_PAGES = 10

# 이 트래커가 찾는 말. '자전거'는 '자전거도로'·'전기자전거'까지 함께 걸리라고
# 통째로 둔다 — 이 분야에서 넓게 거는 쪽이 놓치는 쪽보다 낫다.
KEYWORDS = [
    "개인형 이동장치", "개인형이동장치", "개인형 이동수단", "개인형이동수단",
    "전동킥보드", "킥보드", "전동이륜평행차", "전동기의 동력만으로",
    "자전거", "전기자전거", "퍼스널 모빌리티", "퍼스널모빌리티",
    # 법령문에서 헬멧은 '안전모'·'인명보호 장구'로 쓴다. 87924(도로교통법 시행규칙,
    # 안전모 미착용 벌점)가 '헬멧'으로는 한 글자도 안 걸렸다.
    "안전모", "인명보호", "승차용 안전모",
]

# 본문에 관심어가 없어도 이 법의 개정이면 사람이 봐야 한다.
# 87924가 그 예다 — 본문은 "기초질서 벌점 정비"라고만 하고, PM에 어떻게 걸리는지는
# 별표 28(첨부 hwpx)에 있다. 첨부는 API로 안 오므로 본문만 믿으면 놓친다.
WATCH_LAWS = [
    "도로교통법",
    "도로법",
    "자전거",          # 자전거 이용 활성화에 관한 법률
    "주차장법",
    "교통약자",
    "편의증진",
    "자동차관리법",
    "위치정보",
    "개인정보 보호법",
]

# --- 국회 입법예고 ---------------------------------------------------------
# 국회에 접수된 의안도 예고 기간을 둔다. 다만 이 API는 의안명·소관위·예고종료일만
# 주고 제안이유·주요내용은 주지 않는다(응답 필드 10개, 200자 넘는 필드 없음).
# 그래서 정부 쪽처럼 본문 키워드로 못 거른다 — 의안명으로 걸러야 하고, 그러려면
# PM 규제가 실리는 법 이름을 알고 있어야 한다. 아래 목록이 그 역할이다.
ASSEMBLY_ENDPOINT = "https://open.assembly.go.kr/portal/openapi/nknalejkafmvgzmpt"
ASSEMBLY_PAGE_SIZE = 100
# 의안 제안이유·주요내용. 예고 목록에는 본문이 없어서 의안명만 보면 "도로교통법
# 일부개정법률안" 20건이 전부 걸린다(음주운전·신호위반 등 PM과 무관한 것 포함).
# 법 이름으로 좁힌 뒤 여기서 본문을 받아 확인하면 정부 쪽과 같은 정밀도가 된다.
ASSEMBLY_SUMMARY = "https://open.assembly.go.kr/portal/openapi/BPMBILLSUMMARY"

# 호출 성패 집계. "볼 게 없었다"와 "못 봤다"를 구분해야 조용한 실패를 안 만든다.
API_ATTEMPTS = 0
API_FAILURES = 0
FAILURE_ABORT_RATIO = 0.5


def now_kst():
    return datetime.now(KST)


def log(msg):
    print("[%s] %s" % (now_kst().strftime("%Y-%m-%d %H:%M:%S"), msg))


def oc():
    return os.environ.get("LAWMAKING_OC", "").strip()


def redact(text):
    return text.replace(oc(), "***OC***") if (oc() and text) else text


def fetch(url, tries=3):
    """(본문 또는 None). 러너에서 .go.kr 연결은 자주 끊겨서 재시도한다."""
    global API_ATTEMPTS, API_FAILURES
    API_ATTEMPTS += 1
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == tries:
                log("조회 실패 %s — %r" % (redact(url), e))
                API_FAILURES += 1
                return None
            time.sleep(2 * attempt)
    return None


def api_health():
    if API_ATTEMPTS == 0:
        return 0.0, "조회 없음"
    ratio = API_FAILURES / API_ATTEMPTS
    return ratio, "%d건 중 %d건 실패 (%.0f%%)" % (API_ATTEMPTS, API_FAILURES, ratio * 100)


ENTITIES = [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
            ("&quot;", '"'), ("&#39;", "'"), ("&middot;", "·"),
            ("&ldquo;", '"'), ("&rdquo;", '"'), ("&rsquo;", "'"), ("&lsquo;", "'")]


def clean(text):
    """본문은 HTML 조각이 섞여 온다. 키워드 판정은 글자만 보면 된다."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in ENTITIES:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def records(xml):
    """ogLmPpSeq를 가진 항목을 전부 뽑는다. 목록이든 상세든 같은 방식으로 읽는다."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # 본문 쪽은 성한 XML이 아닌 조각이 섞여 오는 일이 있다. 그때는 정규식으로 읽는다.
        out = []
        for chunk in re.split(r"(?=<ogLmPpSeq>)", xml)[1:]:
            rec = {}
            for m in re.finditer(r"<(\w+)>(.*?)</\1>", chunk, re.S):
                rec.setdefault(m.group(1), m.group(2))
            if rec.get("ogLmPpSeq"):
                out.append(rec)
        return out
    out = []
    for el in root.iter():
        if el.find("ogLmPpSeq") is None:
            continue
        rec = {}
        for child in el:
            rec.setdefault(child.tag, "".join(child.itertext()).strip())
        out.append(rec)
    return out


def fetch_open_notices():
    """진행중(diff=0)인 입법예고 전부.

    기본 응답은 20건이다. 문서의 요청변수에는 페이징이 없지만 pageSize·pageIndex가
    실제로 먹는다(실측). 한 쪽 100건씩 받아, 덜 온 쪽이 나오면 거기서 멈춘다.
    """
    rows, seen_ids = [], set()
    for page in range(1, MAX_PAGES + 1):
        xml = fetch("%s.xml?OC=%s&diff=0&pageSize=%d&pageIndex=%d"
                    % (REST, urllib.parse.quote(oc()), PAGE_SIZE, page))
        if xml is None:
            return None if page == 1 else rows
        if "<retMsg>401</retMsg>" in xml:
            log("OC 인증 실패(401) — LAWMAKING_OC 를 확인해야 한다")
            return None
        got = [r for r in records(xml) if r.get("ogLmPpSeq")]
        fresh = [r for r in got if r["ogLmPpSeq"] not in seen_ids]
        if not fresh:
            break
        rows += fresh
        seen_ids |= {r["ogLmPpSeq"] for r in fresh}
        if len(got) < PAGE_SIZE:
            break
        time.sleep(DELAY_SEC)
    if not rows:
        log("목록 응답에 항목이 없다")
        return None
    return rows


def fetch_body(row):
    """한 건의 예고 본문(lmPpCts). 목록이 준 세 값을 그대로 경로에 넣는다."""
    url = "%s/%s/%s/%s.xml?OC=%s" % (REST, row.get("ogLmPpSeq", ""),
                                     row.get("mappingLbicId") or "0",
                                     row.get("announceType") or "TYPE5",
                                     urllib.parse.quote(oc()))
    xml = fetch(url)
    if xml is None:
        return None
    m = re.search(r"<lmPpCts>(.*?)</lmPpCts>", xml, re.S)
    return clean(m.group(1)) if m else ""


def tidy_name(name):
    """제명 앞에 붙는 [진행] 같은 상태 표시는 알림에서 군더더기다."""
    return re.sub(r"^\s*\[[^\]]{1,6}\]\s*", "", name or "").strip()


def hits_in(text):
    return [k for k in KEYWORDS if k in (text or "")]


def excerpt(text, keyword, width=140):
    """키워드가 나온 자리를 앞뒤로 잘라 보여준다 — 왜 걸렸는지 바로 보이게."""
    i = text.find(keyword)
    if i < 0:
        return ""
    s = max(0, i - width // 2)
    return ("…" if s else "") + text[s:s + width] + "…"


def fetch_assembly_notices():
    """국회 입법예고(진행중) 전부. 한 쪽 100건씩 받는다."""
    key = os.environ.get("ASSEMBLY_API_KEY", "").strip()
    if not key:
        log("ASSEMBLY_API_KEY 가 없다 — 국회 입법예고는 건너뛴다")
        return []
    rows, page = [], 1
    while page <= 20:
        raw = fetch("%s?KEY=%s&Type=json&pIndex=%d&pSize=%d"
                    % (ASSEMBLY_ENDPOINT, key, page, ASSEMBLY_PAGE_SIZE))
        if raw is None:
            return None if page == 1 else rows
        try:
            data = json.loads(raw)
        except Exception:
            log("국회 응답이 JSON이 아니다")
            return None if page == 1 else rows
        if "RESULT" in data:            # 더 없으면 INFO-200 을 준다
            break
        try:
            got = data["nknalejkafmvgzmpt"][1]["row"]
        except Exception:
            break
        rows += got
        if len(got) < ASSEMBLY_PAGE_SIZE:
            break
        page += 1
        time.sleep(DELAY_SEC)
    return rows


def assembly_summary(bill_id):
    """의안 제안이유·주요내용. 못 받으면 None(모른다), 없으면 빈 문자열."""
    key = os.environ.get("ASSEMBLY_API_KEY", "").strip()
    raw = fetch("%s?KEY=%s&Type=json&pIndex=1&pSize=5&BILL_ID=%s"
                % (ASSEMBLY_SUMMARY, key, urllib.parse.quote(bill_id)))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if "RESULT" in data:          # INFO-200 = 그 의안 요약이 없다
        return ""
    try:
        rows = data["BPMBILLSUMMARY"][1]["row"]
    except Exception:
        return None
    return clean(" ".join(str(v) for r in rows for v in r.values() if v))


def assembly_hits(name):
    """의안명에서 걸리는 것: 관심 키워드가 직접 나오거나, 지정 법의 개정이거나."""
    name = name or ""
    found = [k for k in KEYWORDS if k in name]
    laws = [w for w in WATCH_LAWS if w in name]
    return found, laws


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "alerted": [], "assembly_alerted": [], "last_run": None}


def save_state(state):
    state["last_run"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def publish_notices(found):
    """적중한 정부 입법예고를 페이지가 읽을 파일에 쌓는다.

    슬랙은 그때그때 알리고 지나가지만, 페이지는 "지금까지 뭐가 있었나"를 보여줘야
    한다. 그래서 새로 걸린 건을 기존 목록에 합쳐 두고, 예고일 최신순으로 적는다.
    """
    try:
        with open(NOTICES_PATH, encoding="utf-8") as f:
            prev = json.load(f).get("notices", [])
    except Exception:
        prev = []
    by_no = {n["no"]: n for n in prev}
    today = now_kst().strftime("%Y-%m-%d")
    for f in found:
        no = f["ogLmPpSeq"]
        by_no[no] = {
            "no": no,
            "name": tidy_name(f.get("lsNm")) or "(제명 없음)",
            "office": f.get("asndOfiNm", ""),
            "lsCls": f.get("lsClsNm", ""),
            "pntcNo": f.get("pntcNo", ""),
            "st": f.get("stYd", ""),
            "ed": f.get("edYd", ""),
            "hits": f.get("hits") or [],
            "laws": f.get("laws") or [],
            "excerpt": f["excerpt"],
            "link": DETAIL_PAGE % no,
            "found": by_no.get(no, {}).get("found", today),
        }
    notices = sorted(by_no.values(), key=lambda n: n.get("st", ""), reverse=True)
    with open(NOTICES_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": now_kst().strftime("%Y-%m-%d %H:%M"), "notices": notices},
                  f, ensure_ascii=False, indent=1)
    log("페이지용 목록 갱신: 총 %d건 (%s)" % (len(notices), NOTICES_PATH))


def slack_send(webhook, text, blocks):
    payload = {"text": text, "blocks": blocks}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def build_blocks(found):
    head = "*🛴 입법예고 알림 — 관심 키워드 %d건*" % len(found)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    for f in found:
        # 본문 키워드로 걸린 것과 법 이름만 보고 올린 것은 신뢰도가 다르다.
        # 후자는 실질이 첨부(별표)에 있을 수 있어 사람이 열어봐야 한다고 적어 준다.
        why = ("본문 `%s`" % "`, `".join(f["hits"])) if f.get("hits") \
              else ("지정 법 `%s` — 본문엔 관심어 없음, 첨부(별표) 확인 필요"
                    % "`, `".join(f.get("laws") or []))
        txt = ("*<%s|%s>*\n%s · %s · 공고 %s\n예고기간 %s ~ %s\n걸린 이유: %s\n> %s"
               % (DETAIL_PAGE % f["ogLmPpSeq"], tidy_name(f.get("lsNm")) or "(제명 없음)",
                  f.get("asndOfiNm", ""), f.get("lsClsNm", ""), f.get("pntcNo", ""),
                  f.get("stYd", ""), f.get("edYd", ""), why, f.get("excerpt", "")))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
    return "입법예고 알림 %d건" % len(found), blocks


def build_assembly_blocks(found):
    head = "*🏛 국회 입법예고 — 관심 의안 %d건*" % len(found)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    for f in found:
        why = ("`%s`" % "`, `".join(f["hits"])) if f["hits"] else ("지정 법 `%s`" % "`, `".join(f["laws"]))
        if f.get("note"):
            why += " — " + f["note"]
        txt = ("*<%s|%s>*\n%s · %s\n예고 종료 %s · 의안번호 %s\n걸린 이유: %s%s"
               % (f.get("LINK_URL", ""), f.get("BILL_NAME", ""),
                  f.get("PROPOSER", ""), f.get("CURR_COMMITTEE", ""),
                  f.get("NOTI_ED_DT", ""), f.get("BILL_NO", ""), why,
                  ("\n> " + f["excerpt"]) if f.get("excerpt") else ""))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
    return blocks


def inspect(seq):
    """한 건만 열어서 본문이 실제로 어떻게 오는지 본다.

    "안 걸렸다"가 '본문에 그 말이 없다'인지 '본문이 안 왔다'인지 구분해야 키워드를
    손볼지 수집을 손볼지 정할 수 있다.
    """
    # 상세는 번호만 있으면 불린다(mappingLbicId는 검증하지 않는다). 목록을 훑을 일이 없다.
    row = {"ogLmPpSeq": str(seq)}
    cts = fetch_body(row)
    if cts is None:
        log("본문 조회 실패")
        return 1
    log("lmPpCts 길이 %d자" % len(cts))
    log("키워드 적중: %s" % (", ".join(hits_in(cts)) or "없음"))
    for probe in ["개인형", "이동장치", "킥보드", "자전거", "헬멧", "인명보호", "벌점", "별표"]:
        i = cts.find(probe)
        log("  '%s' %s" % (probe, ("%d번째 글자 — …%s…" % (i, cts[max(0, i-60):i+80])) if i >= 0 else "없음"))
    log("본문 앞 600자: %s" % cts[:600])
    return 0


def main():
    if "--inspect" in sys.argv:
        if not oc():
            log("LAWMAKING_OC 가 없다. 종료.")
            return 1
        return inspect(sys.argv[sys.argv.index("--inspect") + 1])
    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

    if not oc():
        log("LAWMAKING_OC 환경변수가 없다. 종료.")
        return 1
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook and not dry:
        log("SLACK_WEBHOOK_URL 환경변수가 없다. 종료.")
        return 1

    state = load_state()
    first_run = not state.get("seen")
    seen = set(state.get("seen", []))
    alerted = set(state.get("alerted", []))
    log("상태: 본문을 읽어 둔 예고 %d건%s" % (len(seen), " (첫 실행)" if not seen else ""))

    rows = fetch_open_notices()
    if rows is None:
        log("목록을 못 읽었다. 상태를 건드리지 않고 종료한다.")
        return 1
    log("진행중인 입법예고 %d건" % len(rows))

    todo = [r for r in rows if r.get("ogLmPpSeq") not in seen]
    log("아직 본문을 안 읽은 예고 %d건" % len(todo))
    if limit and len(todo) > limit:
        log("이번엔 %d건만 읽는다(나머지는 다음 실행에서 본다)" % limit)
        todo = todo[:limit]

    found = []
    for row in todo:
        seq = row.get("ogLmPpSeq", "")
        cts = fetch_body(row)
        time.sleep(DELAY_SEC)
        if cts is None:
            continue          # 못 읽었다 — seen 에 넣지 않아 다음 실행이 다시 본다
        seen.add(seq)
        name = row.get("lsNm") or ""
        hits = hits_in(name + " " + cts)
        laws = [w for w in WATCH_LAWS if w in name]
        if (not hits and not laws) or seq in alerted:
            continue
        row["hits"] = hits
        row["laws"] = laws
        row["excerpt"] = excerpt(cts, hits[0]) if hits else (cts[:180] + "…" if cts else "")
        found.append(row)
        log("적중 %s | %s | %s"
            % (seq, name, ", ".join(hits) if hits else "지정 법 " + ", ".join(laws)))

    # --- 국회 입법예고 ---
    assembly_alerted = set(state.get("assembly_alerted", []))
    assembly_found = []
    arows = fetch_assembly_notices()
    if arows is None:
        log("국회 입법예고를 못 읽었다 — 이번엔 정부 쪽만 본다")
    else:
        log("국회 입법예고 %d건" % len(arows))
        skipped = 0
        for r in arows:
            bid = r.get("BILL_ID") or r.get("BILL_NO") or ""
            if not bid or bid in assembly_alerted:
                continue
            hits, laws = assembly_hits(r.get("BILL_NAME"))
            if not hits and not laws:
                continue
            if not hits:
                # 법 이름만 걸렸다 — 제안이유를 읽어 PM 얘기인지 확인한다.
                summary = assembly_summary(r.get("BILL_ID") or "")
                time.sleep(DELAY_SEC)
                if summary is None:
                    r["note"] = "제안이유를 못 읽어 법 이름만으로 올림"
                elif summary:
                    found_in = hits_in(summary)
                    if not found_in:
                        skipped += 1
                        continue
                    hits = found_in
                    r["excerpt"] = excerpt(summary, found_in[0])
                else:
                    r["note"] = "제안이유가 등록되지 않아 법 이름만으로 올림"
            r["hits"], r["laws"] = hits, laws
            assembly_found.append(r)
            log("국회 적중 %s | %s | %s"
                % (r.get("BILL_NO"), r.get("BILL_NAME"), ", ".join(hits or laws)))
        if skipped:
            log("국회 %d건은 법 이름만 걸리고 제안이유에 관심어가 없어 걸렀다" % skipped)

    ratio, health = api_health()
    log("조회 상태: %s" % health)
    if ratio >= FAILURE_ABORT_RATIO:
        log("실패율이 높아 이번 실행은 믿을 수 없다. 상태를 갱신하지 않는다.")
        return 1

    blocks = []
    if found:
        _, blocks = build_blocks(found)
    if assembly_found:
        blocks += build_assembly_blocks(assembly_found)

    if blocks and first_run and not dry:
        # 첫 실행은 이미 열려 있던 예고를 통째로 훑는다. 그걸 다 보내면 채널이
        # 묻히고, 대부분은 이미 지나간 얘기다. 조용히 채워 두고 다음 실행부터 알린다.
        log("첫 실행이라 슬랙은 보내지 않는다 — 정부 %d건, 국회 %d건을 기록만 한다"
            % (len(found), len(assembly_found)))
        alerted.update(f["ogLmPpSeq"] for f in found)
        assembly_alerted.update((f.get("BILL_ID") or f.get("BILL_NO")) for f in assembly_found)
        blocks = []

    if blocks:
        text = "입법예고 알림 (정부 %d건, 국회 %d건)" % (len(found), len(assembly_found))
        if dry:
            log("--dry-run: 슬랙 전송 생략. 보냈을 내용:")
            print(json.dumps(blocks, ensure_ascii=False, indent=1))
        else:
            log("슬랙 전송 완료 (HTTP %s) — %s" % (slack_send(webhook, text, blocks), text))
            alerted.update(f["ogLmPpSeq"] for f in found)
            assembly_alerted.update((f.get("BILL_ID") or f.get("BILL_NO")) for f in assembly_found)
    else:
        log("관심 키워드에 걸린 새 입법예고 없음.")

    if dry:
        log("--dry-run: 상태 파일도 쓰지 않는다.")
        return 0

    if found:
        publish_notices(found)

    state["seen"] = sorted(seen, key=lambda s: -int(s))[:4000]
    state["alerted"] = sorted(alerted, key=lambda s: -int(s))[:500]
    state["assembly_alerted"] = sorted(assembly_alerted)[-1000:]
    save_state(state)
    log("상태 갱신 완료 (seen %d건, 정부 alerted %d건, 국회 alerted %d건)"
        % (len(state["seen"]), len(state["alerted"]), len(state["assembly_alerted"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
