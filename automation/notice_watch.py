# -*- coding: utf-8 -*-
"""입법예고 감시 — 킥보드·개인형 이동장치·자전거·전기자전거가 걸리면 알린다.

이 트래커의 나머지(cloud_check_updates.py)는 국회 의안을 본다. 여기는 정부 쪽
입법예고를 본다. 법안이 발의되기 전에, 부처가 시행령·시행규칙을 고치겠다고
예고하는 단계라서 먼저 알수록 의견제출 기간이 남는다.

쓰는 API 두 개 (국민참여입법센터, OC 인증):

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

비밀값은 파일이 아니라 환경변수로 받는다(이 저장소는 퍼블릭이다):
      LAWMAKING_OC        국민참여입법센터 승인 아이디
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

REST = "https://www.lawmaking.go.kr/rest/ogLmPpMod"
DETAIL_PAGE = "https://opinion.lawmaking.go.kr/gcom/ogLmPp/%s"

UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25
DELAY_SEC = 0.4

# 이 트래커가 찾는 말. '자전거'는 '자전거도로'·'전기자전거'까지 함께 걸리라고
# 통째로 둔다 — 이 분야에서 넓게 거는 쪽이 놓치는 쪽보다 낫다.
KEYWORDS = [
    "개인형 이동장치", "개인형이동장치", "개인형 이동수단", "개인형이동수단",
    "전동킥보드", "킥보드", "전동이륜평행차", "전동기의 동력만으로",
    "자전거", "전기자전거", "퍼스널 모빌리티", "퍼스널모빌리티",
]

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
    """진행중(diff=0)인 입법예고 전부. 한 번 호출로 번호와 메타가 다 온다."""
    xml = fetch("%s.xml?OC=%s&diff=0" % (REST, urllib.parse.quote(oc())))
    if xml is None:
        return None
    if "<retMsg>401</retMsg>" in xml:
        log("OC 인증 실패(401) — LAWMAKING_OC 를 확인해야 한다")
        return None
    rows = [r for r in records(xml) if r.get("ogLmPpSeq")]
    if not rows:
        log("목록 응답에 항목이 없다 — 응답 앞부분: %s" % redact(xml)[:400])
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


def hits_in(text):
    return [k for k in KEYWORDS if k in (text or "")]


def excerpt(text, keyword, width=140):
    """키워드가 나온 자리를 앞뒤로 잘라 보여준다 — 왜 걸렸는지 바로 보이게."""
    i = text.find(keyword)
    if i < 0:
        return ""
    s = max(0, i - width // 2)
    return ("…" if s else "") + text[s:s + width] + "…"


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "alerted": [], "last_run": None}


def save_state(state):
    state["last_run"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


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
        txt = ("*<%s|%s>*\n%s · %s · 공고 %s\n예고기간 %s ~ %s\n적중: `%s`\n> %s"
               % (DETAIL_PAGE % f["ogLmPpSeq"], f.get("lsNm") or "(제명 없음)",
                  f.get("asndOfiNm", ""), f.get("lsClsNm", ""), f.get("pntcNo", ""),
                  f.get("stYd", ""), f.get("edYd", ""),
                  "`, `".join(f["hits"]), f["excerpt"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
    return "입법예고 알림 %d건" % len(found), blocks


def main():
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
        hits = hits_in((row.get("lsNm") or "") + " " + cts)
        if not hits or seq in alerted:
            continue
        row["hits"] = hits
        row["excerpt"] = excerpt(cts, hits[0])
        found.append(row)
        log("적중 %s | %s | %s" % (seq, row.get("lsNm"), ", ".join(hits)))

    ratio, health = api_health()
    log("조회 상태: %s" % health)
    if ratio >= FAILURE_ABORT_RATIO:
        log("실패율이 높아 이번 실행은 믿을 수 없다. 상태를 갱신하지 않는다.")
        return 1

    if found:
        text, blocks = build_blocks(found)
        if dry:
            log("--dry-run: 슬랙 전송 생략. 보냈을 내용:")
            print(json.dumps(blocks, ensure_ascii=False, indent=1))
        else:
            log("슬랙 전송 완료 (HTTP %s), %d건" % (slack_send(webhook, text, blocks), len(found)))
            alerted.update(f["ogLmPpSeq"] for f in found)
    else:
        log("관심 키워드에 걸린 새 입법예고 없음.")

    if dry:
        log("--dry-run: 상태 파일도 쓰지 않는다.")
        return 0

    state["seen"] = sorted(seen, key=lambda s: -int(s))[:4000]
    state["alerted"] = sorted(alerted, key=lambda s: -int(s))[:500]
    save_state(state)
    log("상태 갱신 완료 (seen %d건, alerted %d건)"
        % (len(state["seen"]), len(state["alerted"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
