# -*- coding: utf-8 -*-
"""입법예고 감시 — 킥보드·개인형 이동장치·자전거·전기자전거가 걸리면 알린다.

이 트래커의 나머지(cloud_check_updates.py)는 국회 의안을 본다. 여기는 정부 쪽
입법예고를 본다. 법안이 발의되기 전에, 부처가 시행령·시행규칙을 고치겠다고
예고하는 단계라서 먼저 알수록 의견제출 기간이 남는다.

왜 제명만 보면 안 되는가:
  국민참여입법센터 목록의 제명 검색(lsNm)은 실제로 이렇게 나온다.
      자전거   → 1건        도로교통 → 2건(종료포함 6건)
      킥보드   → 0건        이동장치 → 0건
  PM 관련 개정은 제명이 "도로교통법 시행규칙 일부개정령안"이고, '개인형 이동장치'
  같은 말은 본문에만 나온다. 그래서 본문(lmPpCts)까지 읽어야 한다.

읽는 방법(실측으로 확정):
  목록  : https://opinion.lawmaking.go.kr/gcom/ogLmPp   (서버가 그린 HTML, 한 쪽 20건)
          각 행은 /gcom/ogLmPp/{번호} 로 이어진다. 정렬은 최신순이다.
  본문  : https://www.lawmaking.go.kr/rest/ogLmPpMod/{번호}/0/TYPE5.xml?OC=...
          목록의 번호를 ogLmPpSeq 자리에 그대로 넣으면 그 공고가 온다.
          응답 필드: ogLmPpSeq, lsNm, asndOfiNm, asndDptNm, lmTpNm, lsClsNm,
                     stYd, edYd, telNo, faxNo, email, modDt, status, readCnt, lmPpCts
          잘못된 OC 는 <result><retMsg>401</retMsg></result> 를 준다.

무엇을 보는가:
  1) 최신 1쪽(20건). 어느 법에서 튀어나오든 새 예고를 잡는다. PM 규제가 늘 예상한
     법에서만 오지는 않는다 — 배터리 안전(전기용품법), 공유 모빌리티(여객자동차법)
     처럼 목록에 없는 법에서 오는 경우가 실제로 있다.
  2) WATCH_LAWS 의 제명 검색. 그 법의 시행령·시행규칙이면 제명에 법 이름이 그대로
     들어가므로 확실히 걸리고, 1쪽 밖으로 밀려난 예고까지 잡는 안전망이 된다.

왜 이렇게 적게 부르는가:
  상세 한 번 호출에 제명·부처·기간·본문이 다 온다. 그래서 목록 쪽은 "무슨 번호가
  있나"만 알면 되고, 본문은 아직 안 읽은 번호에 대해서만 부른다.
  평소 한 번 실행: 목록 1 + 지정 법 검색 9 + 새 글 수(하루 10건 안팎) ≒ 20회 미만.

  1쪽이 통째로 새 글이면 그때만 놓친 구간을 의심한다. 그 경우 HTML 쪽 넘김 대신
  번호로 직접 메운다(ogLmPpSeq는 순번이다). 화면 구조에 덜 기대고, 요청도 적다.

비밀값은 파일이 아니라 환경변수로 받는다(이 저장소는 퍼블릭이다):
      LAWMAKING_OC        국민참여입법센터 승인 아이디
      SLACK_WEBHOOK_URL   알림 보낼 채널
상태 파일(notice_state.json)은 이 폴더에 있고, 커밋·푸시는 호출하는 워크플로 몫이다.

인자:
  --dry-run   슬랙을 보내지 않고 상태 파일도 쓰지 않는다. 판정만 출력한다.
  --limit N   본문을 읽을 건수 상한(밀린 게 많을 때 나눠 읽는다).
"""
import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "notice_state.json")

LIST_URL = "https://opinion.lawmaking.go.kr/gcom/ogLmPp"
DETAIL_PAGE = "https://opinion.lawmaking.go.kr/gcom/ogLmPp/%s"
REST_URL = "https://www.lawmaking.go.kr/rest/ogLmPpMod/%s/0/TYPE5.xml?OC=%s"

UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25
DELAY_SEC = 0.4

# 1쪽이 전부 새 글일 때만 번호로 구간을 메운다. 이 상한을 넘도록 밀렸으면
# 조용히 반쯤 훑는 대신 밀렸다고 알리는 편이 낫다.
MAX_GAP_FILL = 120

# PM 규제가 실제로 실리는 법들. 이 법의 시행령·시행규칙 개정이면 제명에 법 이름이
# 그대로 들어가므로, 제명 검색(lsNm)으로 확실히 잡힌다. 최신 1쪽 밖으로 밀려난
# 예고도 여기서 걸리므로 안전망 역할을 한다.
#
# 검색은 제명에 대한 부분일치라 짧고 튀는 조각으로 넣는다. '도로'처럼 너무 흔한
# 조각은 관계없는 예고를 잔뜩 끌고 오므로 피한다.
WATCH_LAWS = [
    "도로교통",      # 도로교통법 — 개인형 이동장치 정의·통행방법이 여기 있다
    "도로법",
    "자전거",        # 자전거 이용 활성화에 관한 법률
    "주차장",
    "교통약자",
    "장애인",        # 장애인·노인·임산부 등의 편의증진 보장에 관한 법률
    "자동차관리",
    "위치정보",
    "개인정보",
]

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


def redact(text):
    oc = os.environ.get("LAWMAKING_OC", "").strip()
    return text.replace(oc, "***OC***") if (oc and text) else text


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


def strip_tags(html):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in ENTITIES:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"max_seq": 0, "seen": [], "alerted": [], "last_run": None}


def save_state(state):
    state["last_run"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def list_numbers(query="", titles=None):
    """목록 화면 한 쪽에서 글 번호를 최신순으로 뽑는다.

    제목도 같이 챙긴다. 상세 응답의 lsNm은 비어서 오는 일이 잦아서(실측),
    알림에 쓸 제목은 목록 쪽이 더 믿을 만하다.
    """
    html = fetch(LIST_URL + query)
    if html is None:
        return None
    body = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S | re.I)
    if not body:
        log("목록에서 tbody를 못 찾았다 — 화면 구조가 바뀌었을 수 있다")
        return None
    pairs = re.findall(r'href="/gcom/ogLmPp/(\d+)"[^>]*title="([^"]*)"', body.group(1))
    if titles is not None:
        for no, title in pairs:
            titles.setdefault(no, strip_tags(title))
    return [int(no) for no, _ in pairs]


def title_from_cts(cts):
    """본문 머리말에서 제명을 건져 낸다.

    번호로 구간을 메울 때는 목록 행이 없어 제목을 따로 얻을 데가 없다. 본문은
    "⊙○○부공고제2026-1015호 <제명> 입법예고를 하는데 있어…"로 시작한다.
    """
    m = re.search(r"제\s*[\d\-]+\s*호\s*(.{4,90}?)\s*(?:입법예고|행정예고)", cts or "")
    return m.group(1).strip() if m else ""


def fetch_notice(seq):
    """한 건의 제명·부처·기간·본문을 한 번에 가져온다.

    번호가 비었거나 다른 유형이면 lsNm·lmPpCts가 비어서 온다. 그건 '없는 글'로
    보고 넘긴다(구간 메우기에서 실제로 생긴다).
    """
    oc = os.environ.get("LAWMAKING_OC", "").strip()
    xml = fetch(REST_URL % (seq, urllib.parse.quote(oc)))
    if xml is None:
        return None
    if "<retMsg>401</retMsg>" in xml:
        log("OC 인증 실패(401) — LAWMAKING_OC 를 확인해야 한다")
        return None
    out = {"no": str(seq)}
    for tag in ["lsNm", "asndOfiNm", "asndDptNm", "lsClsNm", "stYd", "edYd"]:
        m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), xml, re.S)
        out[tag] = strip_tags(m.group(1)) if m else ""
    m = re.search(r"<lmPpCts>(.*?)</lmPpCts>", xml, re.S)
    out["cts"] = strip_tags(m.group(1)) if m else ""
    if not out["lsNm"] and not out["cts"]:
        return {}      # 빈 번호 — 실패가 아니다
    return out


def hits_in(text):
    return [k for k in KEYWORDS if k in (text or "")]


def excerpt(text, keyword, width=120):
    """키워드가 나온 자리를 앞뒤로 잘라 보여준다 — 왜 걸렸는지 바로 보이게."""
    i = text.find(keyword)
    if i < 0:
        return ""
    s = max(0, i - width // 2)
    return ("…" if s else "") + text[s:s + width] + "…"


def targets(state, seen, titles):
    """이번에 본문을 읽어야 할 번호를 정한다.

    두 갈래를 합친다.
      1) 최신 1쪽 — 어느 법에서 튀어나오든 새 예고를 잡는다(요청 1회).
      2) 지정 법 제명 검색 — PM 규제가 실리는 법은 이름이 제명에 그대로 들어간다.
         1쪽 밖으로 밀려난 예고도 여기서 걸린다(요청 9회).
    둘 다 '아직 본문을 안 읽은 번호'만 남기므로, 평소 본문 조회는 새 글 수만큼이다.
    """
    nums = list_numbers(titles=titles)
    if nums is None:
        return None
    log("목록 1쪽 %d건 (최신 %s)" % (len(nums), max(nums) if nums else "-"))

    picked = set(nums)
    for kw in WATCH_LAWS:
        found = list_numbers("?lsNm=%s" % urllib.parse.quote(kw), titles=titles)
        if found is None:
            continue
        new_here = set(found) - picked
        # 0건도 찍는다. 조용하면 '그 법에 예고가 없다'와 '검색이 안 먹는다'를
        # 구분할 수 없고, 안전망이 죽은 걸 눈치채지 못한다.
        log("지정 법 '%s' → %d건%s"
            % (kw, len(found), " (그중 %d건은 1쪽 밖)" % len(new_here) if new_here else ""))
        picked |= set(found)
        time.sleep(DELAY_SEC)

    max_seq = int(state.get("max_seq") or 0)
    todo = {n for n in picked if str(n) not in seen}

    # 1쪽이 통째로 새 글이면 그 아래가 잘렸다는 뜻이다. 화면을 더 넘기는 대신
    # 번호로 직접 메운다(ogLmPpSeq는 순번이다).
    if max_seq and nums and all(n > max_seq for n in nums):
        gap = [n for n in range(max_seq + 1, max(nums)) if n not in picked]
        if len(gap) > MAX_GAP_FILL:
            log("밀린 구간이 %d개로 상한(%d)을 넘는다 — 최근 것부터 채운다"
                % (len(gap), MAX_GAP_FILL))
            gap = gap[-MAX_GAP_FILL:]
        if gap:
            log("1쪽이 전부 새 글이라 번호 %d~%d 구간 %d개를 함께 확인한다"
                % (max_seq + 1, max(nums), len(gap)))
        todo |= set(gap)

    return sorted(todo, reverse=True)


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
        txt = ("*<%s|%s>*\n%s\n예고기간 %s ~ %s\n적중: `%s`\n> %s"
               % (DETAIL_PAGE % f["no"], f["lsNm"], f["asndOfiNm"],
                  f["stYd"], f["edYd"], "`, `".join(f["hits"]), f["excerpt"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
    return "입법예고 알림 %d건" % len(found), blocks


def main():
    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

    if not os.environ.get("LAWMAKING_OC", "").strip():
        log("LAWMAKING_OC 환경변수가 없다. 종료.")
        return 1
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook and not dry:
        log("SLACK_WEBHOOK_URL 환경변수가 없다. 종료.")
        return 1

    state = load_state()
    alerted = set(state.get("alerted", []))
    seen = set(state.get("seen", []))
    max_seq = int(state.get("max_seq") or 0)
    log("상태: 마지막으로 본 번호 %s, 본문을 읽어 둔 예고 %d건"
        % (max_seq or "(첫 실행)", len(seen)))

    titles = {}
    todo = targets(state, seen, titles)
    if todo is None:
        log("목록을 못 읽었다. 상태를 건드리지 않고 종료한다.")
        return 1
    log("본문을 읽을 대상 %d건" % len(todo))
    if limit and len(todo) > limit:
        log("이번엔 %d건만 읽는다(나머지는 다음 실행에서 본다)" % limit)
        todo = todo[:limit]

    found, read_ok = [], []
    for seq in todo:
        notice = fetch_notice(seq)
        time.sleep(DELAY_SEC)
        if notice is None:
            continue          # 못 읽었다 — max_seq를 올리지 않아 다음에 다시 본다
        read_ok.append(seq)
        seen.add(str(seq))
        if not notice:
            continue          # 빈 번호
        notice["lsNm"] = (titles.get(str(seq)) or notice["lsNm"]
                          or title_from_cts(notice["cts"]) or "입법예고 %s" % seq)
        hits = hits_in(notice["lsNm"] + " " + notice["cts"])
        if not hits or str(seq) in alerted:
            continue
        notice["hits"] = hits
        notice["excerpt"] = excerpt(notice["cts"], hits[0])
        found.append(notice)
        log("적중 %s | %s | %s" % (seq, notice["lsNm"], ", ".join(hits)))

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
            alerted.update(f["no"] for f in found)
    else:
        log("관심 키워드에 걸린 새 입법예고 없음.")

    if dry:
        log("--dry-run: 상태 파일도 쓰지 않는다.")
        return 0

    # 끝까지 읽은 번호까지만 진도로 인정한다. 못 읽은 건 다음 실행이 다시 본다.
    if read_ok:
        state["max_seq"] = max(max_seq, max(read_ok))
    state["seen"] = sorted(seen, key=int, reverse=True)[:3000]
    state["alerted"] = sorted(alerted, key=int, reverse=True)[:500]
    save_state(state)
    log("상태 갱신 완료 (max_seq %s, seen %d건, alerted %d건)"
        % (state["max_seq"], len(state["seen"]), len(state["alerted"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
