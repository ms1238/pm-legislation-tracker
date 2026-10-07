# -*- coding: utf-8 -*-
"""유튜브 생중계(국정감사·상임위 등) 음성을 계속 듣다가 키워드가 나오면 슬랙으로 알린다.

알림에는 발언 시각, 발언자(추정), 발언 성격·취지 태그, 앞뒤 발언 흐름이 붙는다.
분석은 규칙 기반이라 외부 서비스 가입이나 비용이 없다. 발언자는 위원장의 마지막
호명("○○○ 위원님 질의해 주십시오")으로 추정한다.

슬랙은 웹훅이 묶인 채널 한 곳으로만 간다(예: #nationalauditrealtimetracker).
팀 공용 알림의 SLACK_WEBHOOK_URL 은 일부러 읽지 않는다 — 음성 인식 오탐이 섞이는
알림이라 받을 곳을 따로 정해 둔다.

GitHub Actions 에서는 돌리지 않는다. 유튜브가 데이터센터 IP 의 yt-dlp 요청을
봇으로 막는 일이 잦고, 작업 시간도 6시간으로 잘린다. 회의 날 개인 PC 에서 켠다.

    pip install -r automation/requirements-live.txt   # ffmpeg 는 따로 설치
    python automation/live_keyword_watch.py "https://www.youtube.com/watch?v=..."

비밀값은 스크립트 옆 텍스트 파일에 한 줄씩 넣어 두면 된다(.gitignore 처리됨).
같은 이름의 환경변수가 있으면 그쪽이 먼저다.
    live_webhook.txt        슬랙 웹훅 주소        (SLACK_LIVE_WEBHOOK_URL)
텔레그램은 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 환경변수로 켠다(선택).

시작할 때 각 알림 수단으로 '감시 시작' 메시지를 한 번 보낸다. 이게 안 오면
키워드를 기다릴 것 없이 연결부터 잘못된 것이다. 화면에 실패 이유가 찍힌다.

동작 방식:
  - ffmpeg 를 한 번만 띄워 계속 듣고, 15초 구간을 3초씩 겹쳐 인식한다.
  - 인식은 별도 스레드에서 한다. 인식이 느려도 녹음이 멈추지 않는다.
  - 키워드가 나오면 바로 보내지 않고 다음 두 구간(약 25초)을 더 듣는다. 발언 취지는
    키워드 뒤에 나오는 경우가 많아서다. 그 뒤 앞뒤 발언을 붙여 보낸다.
  - 키워드가 언급될 때마다 알린다. 25초 안에 이어진 언급은 한 알림으로 묶는다.
  - 발음 보정: 음성 인식의 다른 후보 문장도 보고, 자모 단위로 한 글자 정도 어긋난
    표현(킥버드, 퀵보드 등)도 잡는다. 늘 틀리는 표현은 live_keywords.txt 에 등록한다.
  - '방송 열기' 링크는 실시간이 아니라 발언 시점(40초 전)으로 간다.
  - 정회로 방송이 끊겨도 꺼지지 않고 1분마다 재개를 확인한다. 끝난 방송을 다시보기로
    처음부터 재생하지 않는다.
  - 정회·속개를 채널에 알린다(위원장 선포, 또는 송출이 3분 넘게 멈췄다가 재개).
  - 위원장의 산회(감사 종료) 선포를 듣고, 방송 송출까지 끝나면 스스로 마친다.
    선포만 듣고는 끄지 않는다 — 잘못 알아들었을 때 오후 감사를 놓치지 않기 위해서다.
  - 윈도우에서는 실행 중 PC가 절전으로 들어가지 않게 한다.
  - 인식한 발언 전부를 스크립트 옆 logs/live_log_날짜.txt 에 시각·방송 경과 시간과 함께
    남긴다. 질의 순서, 키워드 감지, 정회·재개·산회도 표시된다.
"""
import collections, json, os, queue, re, subprocess, sys, threading, time, urllib.error, urllib.request

import speech_recognition as sr
import yt_dlp

# ==================== [ 설정 ] ====================
YOUTUBE_URL = "https://www.youtube.com/watch?v=nCAVxaqGiVM"

# 띄어쓰기는 무시하고 비교한다. 한 발언에 여러 개가 걸리면 모두 알린다.
# '피엠'은 'PM'을 음성 인식이 한글로 받아 적은 형태다.
# 실제로는 스크립트 옆 live_keywords.txt 를 쓴다(없으면 아래 목록으로 만든다).
# 그 파일은 실행 중에 고쳐도 저장하는 즉시 반영된다 — 다시 켤 필요가 없다.
KEYWORDS = [
    "개인형 이동장치", "개인형 이동수단", "퍼스널 모빌리티", "피엠",
    "공유 킥보드", "전동킥보드", "킥보드", "킥라니", "공유 모빌리티",
]

RATE, WIDTH = 16000, 2             # 16kHz, 16bit 모노
BPS = RATE * WIDTH                 # 초당 바이트
CHUNK_SEC, OVERLAP_SEC = 15, 3     # 15초 구간, 3초 겹침
COOLDOWN_SEC = 0                   # 같은 키워드 재알림 최소 간격(초). 0 = 언급될 때마다.
                                   # 25초 안에 이어진 언급은 어차피 한 알림으로 묶인다.
FOLLOW_CHUNKS = 2                  # 감지 후 더 들을 구간 수(취지 파악용)
CONTEXT_BEFORE_SEC = 30            # 알림에 붙일 감지 앞쪽 발언 길이
LINK_LEAD_SEC = 40                 # '방송 열기'를 발언보다 이만큼 앞에서 시작한다.
                                   # 유튜브 생중계 지연(10~30초)과 앞 맥락을 덮는다.
# ==================================================

HERE = os.path.dirname(os.path.abspath(__file__))


def load_secret(env_name, filename, legacy_env=None):
    """값과 출처를 돌려준다. 환경변수가 파일보다 먼저다."""
    for name in (env_name, legacy_env):
        if name and os.environ.get(name, "").strip():
            return os.environ[name].strip(), f"환경변수 {name}"
    path = os.path.join(HERE, filename)
    if os.path.exists(path):
        raw = open(path, "rb").read()
        try:
            text = raw.decode("utf-8-sig")               # 메모장·PowerShell 이 붙이는 BOM 제거
        except UnicodeDecodeError:
            text = raw.decode("cp949")                   # 윈도우 PowerShell 5 Set-Content 기본값
        return text.strip().strip('"').strip("'"), filename
    return "", ""


# 발언 기록은 logs 폴더에 따로 둔다. 이 폴더만 OneDrive 등으로 공유하면 웹훅 주소가
# 든 live_webhook.txt 를 함께 노출하지 않는다. 그 공유 링크를 live_log_link.txt 에 넣어
# 두면 슬랙 알림마다 '전체 발언 기록 보기' 링크가 붙는다.
# 다른 곳(예: 구글 드라이브 데스크톱의 G:\내 드라이브\국감기록)에 쓰려면 그 경로를
# live_log_dir.txt 에 한 줄로 넣는다. 드라이브 앱이 알아서 올린다.
LOG_DIR = load_secret("LIVE_LOG_DIR", "live_log_dir.txt")[0] or os.path.join(HERE, "logs")
LOG_LINK, _ = load_secret("LIVE_LOG_LINK", "live_log_link.txt")

SLACK_WEBHOOK, SLACK_WEBHOOK_SRC = load_secret("SLACK_LIVE_WEBHOOK_URL", "live_webhook.txt",
                                               legacy_env="SLACK_PERSONAL_WEBHOOK_URL")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

last_alert = {}                    # keyword -> 마지막 알림 시각
transcript = collections.deque()   # (구간 시작 시각, 문장) — 최근 몇 분만 보관
pending = []                       # 뒤 구간을 기다리는 감지 건
stream_title = ""                  # 유튜브 방송 제목(회의명 파악용)
stream_meta = {"title": "", "id": "", "start": None}
adjourned = threading.Event()      # 산회·감사 종료 선포를 들었다


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def hhmmss(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


# -------------------- 발언 기록 파일 --------------------
# 인식한 발언을 날짜별 텍스트 파일에 이어 쓴다. 같은 날 다시 켜도 같은 파일에 붙는다.
# 구간이 3초씩 겹쳐서 이웃한 줄의 끝·처음 몇 단어가 겹칠 수 있다.
_log_lock = threading.Lock()


def record(line, at=None):
    at = at or time.time()
    path = os.path.join(LOG_DIR, time.strftime("live_log_%Y-%m-%d.txt", time.localtime(at)))
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            log(f"⚠️ 기록 파일 쓰기 실패: {e}")


def stream_pos(at):
    """방송 경과 시간(h:mm:ss). 시작 시각을 모르면 빈 문자열."""
    t0 = stream_meta.get("start")
    if not t0:
        return ""
    sec = max(0, int(at - t0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def record_utterance(text, at):
    pos = stream_pos(at)
    record(f"[{hhmmss(at)}{' | 방송 ' + pos if pos else ''}] {text}", at)


# -------------------- 알림 전송 --------------------
def post_json(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        # 슬랙은 실패 이유를 본문에 적어 보낸다(invalid_token, no_service 등).
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {e.code} {detail}") from None


def send_slack(msg):
    if not SLACK_WEBHOOK:
        return
    try:
        post_json(SLACK_WEBHOOK, {"text": msg})
        log("슬랙 전송 성공")
    except Exception as e:
        log(f"❌ 슬랙 전송 실패: {e}")
        if "no_service" in str(e) or "404" in str(e):
            # 지운 웹훅이거나, 같은 터미널에 예전에 넣어 둔 환경변수가 새 파일을 가리는 경우가 많다.
            log(f"   → 이 웹훅은 슬랙에서 삭제됐거나 잘못된 주소입니다. 읽은 곳: {SLACK_WEBHOOK_SRC}")


def send_telegram(msg):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        post_json(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  {"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        log("텔레그램 전송 성공")
    except Exception as e:
        log(f"❌ 텔레그램 전송 실패: {e}")


# -------------------- 발언 분석(규칙 기반, 무료) --------------------
# 음성 인식은 화자를 구분하지 못한다. 대신 국감·상임위 질의는 위원장이
# "○○○ 위원님 질의해 주십시오"라고 호명한 뒤 그 위원과 증인이 몇 분간 주고받는
# 구조라, 마지막 호명을 기억해 두면 '지금 누구의 질의 순서인지'는 꽤 맞힌다.

# 질의 위원을 알아내는 단서 세 가지(앞에 띄어쓰기나 문장 시작이 있어야 이름으로 본다).
#  - 위원장 호명: "김철수 위원님 질의해 주십시오", "이영희 위원님 순서입니다", "박민수 위원 하십시오"
#  - 위원장 예고: "다음은 (○○당) 김철수 위원"
#  - 위원 자기소개: "국민의힘 김철수 위원입니다", "김철수 의원입니다"
_NAME = r"(?:^|\s)([가-힣]{2,4})\s?"
CALL_RES = [
    re.compile(_NAME + r"위원님?\s?(?:께서\s?)?(?:보충\s?|추가\s?)?"
               r"(?:질의|질문|발언|순서|하십시오|해\s?주십시오|해\s?주시기|말씀해)"),
    re.compile(r"다음은?\s?(?:[가-힣]+당\s?)?" + _NAME.replace("(?:^|\\s)", "") + r"위원"),
    re.compile(_NAME + r"(?:위원|의원)입니다"),
]
NOT_NAMES = {"다음", "다음은", "존경하는", "여러", "상임", "전문", "소속", "모든", "각", "해당",
             "그", "이", "저", "우리", "선배", "동료", "여야", "야당", "여당", "민주당", "국민의힘",
             "보충", "추가", "질의", "전체", "간사", "소위", "정부", "관계", "여러분", "위원장"}
CALL_STALE_SEC = 15 * 60           # 호명 후 이만큼 지나면 '바뀌었을 수 있음'을 붙인다
STATE_FILE = os.path.join(HERE, "live_state.json")   # 다시 켜도 질의 위원을 이어받는다

# 질의인지 답변인지: 정부·증인을 부르면 위원의 질의, 답변 어투면 정부·증인 쪽.
ADDRESS_RE = re.compile(r"(장관님|차관님|청장님|사장님|원장님|이사장님|본부장님|실장님|국장님|"
                        r"회장님|대표님|증인|참고인)")
QUESTION_RE = re.compile(r"(습니까|십니까|겠습니까|않습니까|아닙니까|어떻게\s?생각|여쭙|묻겠습니다|"
                         r"답변해\s?주십시오|말씀해\s?주십시오)")
ANSWER_RE = re.compile(r"(답변\s?드리|말씀\s?드리겠|말씀\s?드립니다|검토하겠습니다|"
                       r"살펴보겠습니다|조치하겠습니다|노력하겠습니다|위원님\s?말씀)")

# 감사 종료: '산회'는 그날 회의를 끝낼 때만 쓰고 점심 '정회'와 다르다.
# 이 말을 들어도 바로 끄지 않는다 — 방송까지 끝난 걸 확인한 뒤에 끈다.
ADJOURN_RE = re.compile(r"(산회를?\s?선포|산회하겠습니다|국정감사를?\s?모두\s?마치)")

current_call = None                # (이름, 호명 시각, 호명 문장)


# 정회·속개를 채널에 알린다. 같은 상태를 두 번 알리지 않도록 상태가 바뀔 때만 보낸다.
# 근거는 두 가지다: 위원장의 선포(음성 인식)와 방송 송출 멈춤·재개.
RECESS_RE = re.compile(r"(정회를?\s?선포|정회하겠습니다|정회하도록\s?하겠습니다)")
RESUME_RE = re.compile(r"(속개하겠습니다|속개를?\s?선포|(?:회의|감사)를?\s?속개|속개하도록|"
                       r"개의를?\s?선포|개의하겠습니다)")
PAUSE_NOTICE_SEC = 180             # 선포 없이 송출이 이만큼 멈추면 정회로 보고 알린다
session_state = "unknown"          # unknown / 진행 / 정회
_state_lock = threading.Lock()


def set_session(new, reason, at=None):
    """회의 상태가 바뀌었으면 기록하고 채널에 알린다."""
    global session_state
    at = at or time.time()
    with _state_lock:
        if session_state == new:
            return
        old, session_state = session_state, new
    if new == "정회":
        head = f"⏸ *정회* ({hhmmss(at)}경)"
        tail = "꺼지지 않고 속개를 기다립니다."
    else:
        head = f"▶ *{'속개' if old == '정회' else '회의 진행 중'}* ({hhmmss(at)}경)"
        tail = "다시 듣습니다."
    log(f"{head.replace('*', '')} — {reason}")
    record(f"\n===== {head.replace('*', '')} — {reason} =====", at)
    lines = [head, f"_{reason}_"]
    if stream_title:
        lines.append(f"*회의*  {stream_title}")
    if new == "정회" and current_call:
        lines.append(f"*정회 직전 질의 순서*  {current_call[0]} 위원")
    url, pos = moment_link(at)
    if pos:
        lines.append(f"<{url}|▶ 해당 지점 보기 ({pos})>")
    if LOG_LINK:
        lines.append(f"<{LOG_LINK}|📄 전체 발언 기록 보기>")
    lines.append(tail)
    send_slack("\n".join(lines))


def watch_session(text, start):
    if RECESS_RE.search(text):
        set_session("정회", "위원장 정회 선포", start)
    elif RESUME_RE.search(text):
        set_session("진행", "위원장 속개·개의 선포", start)
    if ADJOURN_RE.search(text) and "정회" not in text and not adjourned.is_set():
        adjourned.set()
        log("🏁 산회(감사 종료) 선포 감지 — 방송이 끝나면 감시를 마칩니다.")
        record("\n===== 🏁 산회(감사 종료) 선포 =====")
        send_slack("🏁 산회(감사 종료) 선포 감지 — 방송 송출이 끝나면 감시를 마칩니다.")


def load_speaker_state():
    """직전 실행에서 알아낸 질의 위원을 이어받는다(정회 중 재실행 대비)."""
    global current_call
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if time.time() - st["t"] < CALL_STALE_SEC:
            current_call = (st["name"], st["t"], "")
            log(f"👤 직전 실행의 질의 순서를 이어받음: {st['name']} 위원 ({hhmmss(st['t'])})")
    except (OSError, ValueError, KeyError):
        pass


def update_speaker(text, start):
    global current_call
    for rx in CALL_RES:
        for m in rx.finditer(text):
            name = m.group(1)
            if name in NOT_NAMES or (current_call and current_call[0] == name):
                continue
            current_call = (name, start, text)
            log(f"👤 질의 순서 바뀜: {name} 위원")
            record(f"\n----- 👤 {name} 위원 질의 순서 ({hhmmss(start)}) -----", start)
            try:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"name": name, "t": start}, f, ensure_ascii=False)
            except OSError:
                pass
            if session_state == "정회":
                # 속개 선포를 못 알아들었어도 질의가 다시 시작됐으면 회의는 진행 중이다.
                set_session("진행", f"{name} 위원 질의 호명 감지", start)


def speaker_line(text, at):
    """감지 문장의 발언자를 한 줄로. 질의 위원 이름 + 질의/답변 구분."""
    if current_call:
        name, t, _ = current_call
        who = f"{name} 위원"
        note = f" — {int((at - t) // 60)}분 전 확인, 바뀌었을 수 있음" if at - t > CALL_STALE_SEC else ""
    else:
        who, note = "질의 위원(이름 미확인 — 감시 시작 후 호명·자기소개를 못 들음)", ""
    asks = ADDRESS_RE.search(text) or QUESTION_RE.search(text)
    if ANSWER_RE.search(text) and not asks:
        return f"정부·증인 측 답변으로 보임 ({who} 질의에 대한){note}"
    if asks:
        return f"{who} (질의 중){note}"
    return f"{who} 질의 순서 중 (질의·답변 구분 어려움){note}"


def merge_chunks(texts):
    """3초씩 겹친 구간 문장을 한 문단으로 잇는다. 겹친 단어는 한 번만 남긴다."""
    words = []
    for t in texts:
        nxt = t.split()
        if words and nxt:
            for k in range(min(len(words), len(nxt), 10), 0, -1):
                tail, head = words[-k:], nxt[:k]
                if tail == head:
                    nxt = nxt[k:]
                    break
                # 경계에서 잘린 마지막 단어("20" ↔ "2023년")는 다음 구간 쪽을 남긴다.
                if tail[:-1] == head[:-1] and head[-1].startswith(tail[-1]):
                    words = words[:-k]
                    break
        words += nxt
    return " ".join(words)


def highlight(text, terms, mark="*"):
    """키워드가 든 어절을 굵게. 슬랙은 * 앞뒤가 띄어쓰기여야 굵게 보여서 어절 단위로 감싼다."""
    spans = []
    for term in terms:
        flat = term.replace(" ", "")
        if not flat:
            continue
        rx = re.compile(r"\s?".join(map(re.escape, flat)), re.IGNORECASE)
        for m in rx.finditer(text):
            s0 = text.rfind(" ", 0, m.start()) + 1
            e0 = text.find(" ", m.end())
            spans.append((s0, len(text) if e0 < 0 else e0))
    if not spans:
        return text
    spans.sort()
    merged = [list(spans[0])]
    for s0, e0 in spans[1:]:
        if s0 <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e0)
        else:
            merged.append([s0, e0])
    out, pos = [], 0
    for s0, e0 in merged:
        out += [text[pos:s0], mark, text[s0:e0], mark]
        pos = e0
    out.append(text[pos:])
    return "".join(out)


def moment_link(at):
    """발언 시점으로 가는 유튜브 링크. 시작 시각을 모르면 실시간 링크."""
    vid, t0 = stream_meta.get("id"), stream_meta.get("start")
    if not (vid and t0):
        return YOUTUBE_URL, None
    offset = max(0, int(at - t0 - LINK_LEAD_SEC))
    h, rem = divmod(offset, 3600)
    return f"https://www.youtube.com/watch?v={vid}&t={offset}s", f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def format_slack(alert, context):
    body = highlight(merge_chunks([t for _, t in context]), alert["terms"])
    span = f"{hhmmss(context[0][0])}~{hhmmss(context[-1][0] + CHUNK_SEC)}" if context else ""
    lines = [f"🚨 *키워드 감지: {', '.join(alert['keywords'])}*"]
    if stream_title:
        lines.append(f"*회의*  {stream_title}")
    lines += [
        f"*발언 시각*  {hhmmss(alert['start'])}경 (PC 수신 기준)",
        f"*발언자(추정)*  {alert['speaker']}",
        f"*발언 내용* ({span}, 음성 인식이라 오탈자 있음)",
        f"> {body}",
        link_line(alert),
    ]
    if LOG_LINK:
        lines.append(f"<{LOG_LINK}|📄 전체 발언 기록 보기>")
    return "\n".join(lines)


def link_line(alert):
    url, pos = alert["link"]
    if pos:
        return f"<{url}|▶ 발언 지점부터 보기 ({pos})>  _발언 {LINK_LEAD_SEC}초 전부터 재생_"
    return f"<{url}|▶ 방송 열기(실시간)>  _시작 시각을 몰라 발언 지점 링크를 못 만듦_"


def format_telegram(alert, context):
    url, pos = alert["link"]
    lines = ["🚨 [키워드 감지 알림]",
             f"• 키워드: {', '.join(alert['keywords'])}",
             f"• 발언 시각: {hhmmss(alert['start'])}경",
             f"• 발언자(추정): {alert['speaker']}",
             f"• 발언 내용: {merge_chunks([t for _, t in context])}",
             f"• 방송({pos} 지점): {url}" if pos else f"• 방송(실시간): {url}"]
    return "\n".join(lines)


def dispatch(alert):
    """앞뒤 발언을 붙여 보낸다. 전송이 느려도 인식이 멈추지 않게 별도 스레드에서 돈다."""
    context = [(t, s) for t, s in list(transcript)
               if alert["start"] - CONTEXT_BEFORE_SEC <= t]

    def run():
        send_slack(format_slack(alert, context))
        send_telegram(format_telegram(alert, context))

    threading.Thread(target=run, daemon=True).start()


# -------------------- 감지 --------------------
KEYWORDS_FILE = os.path.join(HERE, "live_keywords.txt")
_kw_mtime = None
ALIASES = {"피엠": ["PM", "P.M"]}  # 키워드 -> 음성 인식이 대신 적는 표현들
DEFAULT_KEYWORDS_FILE = """\
# 감시할 키워드 — 한 줄에 하나. 저장하면 실행 중에도 바로 반영됩니다.
#
# 음성 인식이 늘 틀리게 적는 표현이 있으면 '=' 뒤에 쉼표로 적어 두세요.
#   예) 킥보드 = 퀵보드, 킥보더
# 적지 않아도 발음이 한 글자 정도 어긋난 건(킥버드, 킥보도 등) 알아서 잡습니다.
# 두 글자 이하 키워드는 오탐을 막으려고 정확히 일치할 때만 잡습니다.

개인형 이동장치 = 개인용 이동장치
개인형 이동수단 = 개인용 이동수단
퍼스널 모빌리티
피엠 = PM, P.M
공유 킥보드
전동킥보드
킥보드
킥라니
공유 모빌리티
"""


# ---- 발음 보정: 한글을 자모로 풀어 '거의 같은' 표현도 잡는다 ----
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def to_jamo(text):
    """글자를 자모로 풀고, 각 자모가 원문 몇 번째 글자에서 왔는지 함께 돌려준다."""
    out, idx = [], []
    for i, ch in enumerate(text):
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            parts = [_CHO[code // 588], _JUNG[code % 588 // 28]]
            if code % 28:
                parts.append(_JONG[code % 28])
        else:
            parts = [ch.lower()]
        out += parts
        idx += [i] * len(parts)
    return out, idx


def _edit_distance(a, b, limit):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def fuzzy_find(word, nospace):
    """nospace 안에서 word 와 발음이 거의 같은 부분을 찾아 그 원문을 돌려준다.
    두 글자 이하는 보정하지 않는다('장관'이 '장군'으로 잡히는 걸 막는다)."""
    syll = len(word)
    if syll <= 2:
        return None
    limit = 1 if syll <= 4 else 2
    wj, _ = to_jamo(word)
    tj, ti = to_jamo(nospace)
    best = None
    for size in range(len(wj) - limit, len(wj) + limit + 1):
        for s0 in range(0, len(tj) - size + 1):
            d = _edit_distance(wj, tj[s0:s0 + size], limit)
            if d <= limit and (best is None or d < best[0]):
                best = (d, s0, s0 + size)
    if not best:
        return None
    _, s0, e0 = best
    # 첫 자모가 같아야 한다 — '킥보드'와 '익보드' 같은 엉뚱한 일치를 줄인다.
    if tj[s0] != wj[0]:
        return None
    return nospace[ti[s0]:ti[e0 - 1] + 1]


def find_keywords(texts):
    """여러 인식 후보에서 키워드를 찾는다. [(키워드, 실제로 적힌 표현)]
    표현이 None 이면 1순위 문장에 그대로 있었고, "" 이면 다른 인식 후보에 있었다."""
    found = {}
    for n, text in enumerate(texts):
        flat = text.replace(" ", "")
        low = flat.lower()
        for k in KEYWORDS:
            if k in found:
                continue
            kk = k.replace(" ", "")
            if kk in flat:
                found[k] = None if n == 0 else ""
                continue
            for a in ALIASES.get(k, []):
                if a.replace(" ", "").lower() in low:
                    found[k] = a
                    break
            else:
                hit = fuzzy_find(kk, flat)
                if hit:
                    found[k] = hit
    return list(found.items())



def refresh_keywords():
    """live_keywords.txt 가 바뀌었으면 다시 읽는다. 한 줄에 하나, # 뒤는 메모."""
    global KEYWORDS, ALIASES, _kw_mtime
    try:
        if not os.path.exists(KEYWORDS_FILE):
            with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
                f.write(DEFAULT_KEYWORDS_FILE)
        mtime = os.path.getmtime(KEYWORDS_FILE)
        if mtime == _kw_mtime:
            return
        raw = open(KEYWORDS_FILE, "rb").read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp949")
        words, aliases = [], {}
        for ln in text.splitlines():
            ln = ln.split("#", 1)[0].strip()
            if not ln:
                continue
            head, _, rest = ln.partition("=")
            head = head.strip()
            if not head:
                continue
            words.append(head)
            aliases[head] = [a.strip() for a in rest.split(",") if a.strip()]
        if not words:
            log("⚠️ live_keywords.txt 가 비어 있어 이전 키워드를 그대로 씁니다")
        else:
            if _kw_mtime is not None:
                log(f"🔄 키워드 갱신: {', '.join(words)}")
            KEYWORDS, ALIASES = words, aliases
        _kw_mtime = mtime
    except OSError as e:
        log(f"⚠️ 키워드 파일을 읽지 못함: {e}")


def check_keywords(text, start, alternatives=()):
    refresh_keywords()
    hits = find_keywords([text, *alternatives])
    if not hits:
        return
    now = time.time()
    fresh = [(k, v) for k, v in hits if now - last_alert.get(k, 0) >= COOLDOWN_SEC]
    for k, _ in hits:
        last_alert[k] = now
    if not fresh:
        log(f"(재알림 간격이라 생략: {', '.join(k for k, _ in hits)})")
        return
    labels = [k if v is None else f"{k}(다른 인식 후보)" if v == "" else f"{k}(←'{v}')"
              for k, v in fresh]
    terms = [k for k, _ in fresh] + [v for _, v in fresh if v]
    record(f"   ↑ 🚨 키워드 감지: {', '.join(labels)}", start)
    # 아직 보내지 않은 감지 건이 있으면 거기에 합친다. 겹친 구간에서 같은 말이 두 번
    # 잡히거나, 한 질의에서 연달아 언급될 때 알림이 쏟아지지 않게 한다.
    if pending:
        alert = pending[-1]
        alert["terms"] += terms
        for lab in labels:
            if lab.split("(")[0] not in [x.split("(")[0] for x in alert["keywords"]]:
                alert["keywords"].append(lab)
        log(f"🚨 키워드 추가 감지: {', '.join(labels)} — 직전 알림에 합칩니다")
        return
    print("\n" + "=" * 50, flush=True)
    log(f"🚨 키워드 감지: {', '.join(labels)} — 뒤 발언을 {FOLLOW_CHUNKS}구간 더 듣고 보냅니다")
    log(f"🗣️ {text}")
    print("=" * 50 + "\n", flush=True)
    pending.append({"keywords": labels, "text": text, "start": start,
                    # 감지 문장과 바로 앞 문장으로 질의·답변을 가린다.
                    "terms": terms,
                    "speaker": speaker_line(" ".join(t for _, t in list(transcript)[-2:]) or text, start),
                    "link": moment_link(start),
                    "wait": FOLLOW_CHUNKS + 1})   # +1: 감지된 구간 자신도 곧 advance 된다


def advance_pending():
    """구간 하나를 처리할 때마다 부른다. 기다림이 끝난 감지 건을 내보낸다."""
    for alert in list(pending):
        alert["wait"] -= 1
        if alert["wait"] <= 0:
            pending.remove(alert)
            dispatch(alert)


def flush_pending():
    """방송이 끊기면 기다리던 건을 바로 보낸다."""
    while pending:
        dispatch(pending.pop(0))


def stt_worker(q):
    r = sr.Recognizer()
    while True:
        item = q.get()
        if item is None:               # 스트림 끊김 신호
            flush_pending()
            continue
        pcm, start = item
        try:
            # show_all: 1순위 문장 말고 다른 인식 후보도 받아 키워드를 찾는다.
            res = r.recognize_google(sr.AudioData(pcm, RATE, WIDTH), language="ko-KR",
                                     show_all=True)
            alts = [a["transcript"] for a in (res or {}).get("alternative", [])
                    if isinstance(a, dict) and a.get("transcript")] if isinstance(res, dict) else []
            if not alts:
                raise sr.UnknownValueError()
            text, others = alts[0], alts[1:]
            log(text)
            record_utterance(text, start)
            transcript.append((start, text))
            while transcript and transcript[0][0] < start - 300:
                transcript.popleft()
            update_speaker(text, start)
            watch_session(text, start)
            check_keywords(text, start, others)
        except sr.UnknownValueError:
            pass                       # 무음이거나 알아듣지 못함
        except sr.RequestError as e:
            log(f"STT 오류: {e}")
        except Exception as e:         # 스레드가 죽으면 조용히 인식이 멈추므로 다 잡는다
            log(f"인식 중 오류: {e}")
        advance_pending()


# -------------------- 스트림 --------------------
class NotLiveNow(Exception):
    """방송이 지금 송출 중이 아니다(정회로 끊겼거나, 아직 시작 전이거나, 끝났다)."""


def get_live_stream(url):
    """유튜브 생중계의 오디오 주소(HLS)와 방송 정보를 꺼낸다."""
    opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    status = info.get("live_status")
    if status in ("was_live", "post_live"):
        # 끝난 생중계를 그대로 열면 다시보기를 처음부터 재생해 오전 발언을 또 알린다.
        raise NotLiveNow("방송이 끝난 상태(정회로 송출이 멈췄을 수 있음)")
    if status == "is_upcoming":
        raise NotLiveNow("방송 시작 전")
    if status != "is_live":
        log("⚠️ 생중계가 아닌 일반 영상입니다 — 처음부터 재생하며 듣습니다(시험용).")
    return info["url"], {
        "title": info.get("title") or "",
        "id": info.get("id") or "",
        # 실제 송출 시작 시각(liveBroadcastDetails.startTimestamp). '방송 열기' 링크를
        # 발언 시점으로 보내는 데 쓴다.
        "start": info.get("release_timestamp"),
    }


def keep_awake():
    """윈도우가 절전으로 들어가 녹음이 멈추는 걸 막는다(정회 중 자리를 비워도)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log("절전 방지: 켜짐(이 창이 떠 있는 동안 PC가 잠들지 않습니다)")
    except Exception as e:
        log(f"⚠️ 절전 방지 설정 실패: {e} — 전원 설정에서 절전을 꺼 주세요")


def monitor_live_stream():
    global stream_title, stream_meta
    channels = [n for n, ok in (("슬랙", SLACK_WEBHOOK),
                                ("텔레그램", TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)) if ok]
    log(f"📡 모니터링 시작: {YOUTUBE_URL}")
    log(f"알림: {', '.join(channels) if channels else '없음 — 콘솔에만 출력'}")
    if SLACK_WEBHOOK:
        log(f"슬랙 웹훅: {SLACK_WEBHOOK_SRC}에서 읽음 (…{SLACK_WEBHOOK[-6:]})")
    if SLACK_WEBHOOK and not SLACK_WEBHOOK.startswith("https://hooks.slack.com/"):
        log(f"⚠️ 슬랙 웹훅 주소 형식이 이상합니다: {SLACK_WEBHOOK[:40]}...")
    refresh_keywords()
    log(f"키워드: {', '.join(KEYWORDS)}  (live_keywords.txt — 실행 중 고쳐도 반영)")
    log("끝내려면 Ctrl+C. 정회로 방송이 멈춰도 꺼지지 않고 재개를 기다립니다.")
    load_speaker_state()
    keep_awake()

    q = queue.Queue(maxsize=20)
    threading.Thread(target=stt_worker, args=(q,), daemon=True).start()
    greeted = False
    waiting_since = None

    while True:                        # 바깥 루프 = 끊겼을 때 재접속
        try:
            stream_url, stream_meta = get_live_stream(YOUTUBE_URL)
            stream_title = stream_meta["title"]
        except NotLiveNow as e:
            if adjourned.is_set():
                # 산회가 선포됐고 방송도 끝났다 — 이제 정말 끝이다.
                log("🏁 산회 선포 후 방송 종료 확인 — 감시를 마칩니다.")
                record(f"##### 감시 종료 {time.strftime('%Y-%m-%d %H:%M:%S')}")
                send_slack("🏁 산회 선포 후 방송 종료 — 생중계 감시를 마칩니다.")
                return
            if waiting_since is None:
                waiting_since = time.time()
                record(f"\n===== ⏸ 방송 송출 멈춤 ({hhmmss(waiting_since)}) =====")
                log(f"⏸ {e} — 1분마다 재개를 확인합니다.")
            elif time.time() - waiting_since >= PAUSE_NOTICE_SEC and session_state != "정회":
                set_session("정회", f"방송 송출이 {int((time.time() - waiting_since) // 60)}분째 "
                                   f"멈춤(선포는 못 들었음)", waiting_since)
            elif int(time.time() - waiting_since) % 600 < 60:
                mins = int((time.time() - waiting_since) // 60)
                log(f"⏸ 재개 대기 중({mins}분째). 오후 방송이 새 주소로 열리면 "
                    f"Ctrl+C 후 새 주소로 다시 실행하세요.")
            time.sleep(60)
            continue
        except Exception as e:
            log(f"스트림 주소 추출 실패: {e} — 30초 후 재시도")
            time.sleep(30)
            continue
        if waiting_since is not None:
            log("▶ 방송 재개 — 다시 듣습니다.")
            record(f"\n===== ▶ 방송 재개 ({hhmmss(time.time())}) =====")
            waiting_since = None
            if session_state == "정회":
                set_session("진행", "방송 송출 재개")
        if not greeted:
            record(f"\n\n##### 감시 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} — "
                   f"{stream_title or YOUTUBE_URL}\n##### {YOUTUBE_URL}")
            log(f"📝 발언 기록: {os.path.join(LOG_DIR, time.strftime('live_log_%Y-%m-%d.txt'))}")
            log(f"기록 공유 링크: {'있음 — 알림에 붙습니다' if LOG_LINK else '없음(live_log_link.txt)'}")
            # 연결 확인용. 이게 안 오면 키워드를 기다릴 필요 없이 알림 설정부터 봐야 한다.
            hello = f"✅ 생중계 키워드 감시 시작 — {stream_title or YOUTUBE_URL}"
            if LOG_LINK:
                hello += f"\n<{LOG_LINK}|📄 전체 발언 기록 보기>"
            send_slack(hello)
            send_telegram(hello)
            greeted = True
        if not stream_meta["start"]:
            log("⚠️ 방송 시작 시각을 몰라 '방송 열기'가 실시간 화면으로 연결됩니다.")

        proc = subprocess.Popen(
            # -rw_timeout: 정회 화면에서 송출이 멎어 응답이 끊기면 30초 뒤 빠져나와 재접속한다.
            ["ffmpeg", "-loglevel", "error", "-rw_timeout", "30000000", "-i", stream_url,
             "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-"],
            stdout=subprocess.PIPE)
        buf = b""
        try:
            while True:
                data = proc.stdout.read(BPS)          # 1초씩
                if not data:
                    break                             # 방송 종료·정회·주소 만료
                buf += data
                if len(buf) >= CHUNK_SEC * BPS:
                    start = time.time() - CHUNK_SEC   # 이 구간이 시작된 시각(수신 기준)
                    try:
                        q.put_nowait((buf, start))
                    except queue.Full:
                        log("⚠️ 인식이 밀려 구간 하나를 건너뜀")
                    buf = buf[-OVERLAP_SEC * BPS:]    # 마지막 3초는 다음 구간에도
        finally:
            proc.kill()
            proc.wait()
        q.put(None)                    # 기다리던 감지 건을 내보내라는 신호
        log("스트림 끊김 — 10초 후 재접속")
        time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        YOUTUBE_URL = sys.argv[1]
    try:
        monitor_live_stream()
    except KeyboardInterrupt:
        log("종료")
