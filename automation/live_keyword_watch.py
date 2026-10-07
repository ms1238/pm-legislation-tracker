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
  - 같은 키워드는 10분에 한 번만 알린다.
  - '방송 열기' 링크는 실시간이 아니라 발언 시점(40초 전)으로 간다.
  - 정회로 방송이 끊겨도 꺼지지 않고 1분마다 재개를 확인한다. 끝난 방송을 다시보기로
    처음부터 재생하지 않는다.
  - 위원장의 산회(감사 종료) 선포를 듣고, 방송 송출까지 끝나면 스스로 마친다.
    선포만 듣고는 끄지 않는다 — 잘못 알아들었을 때 오후 감사를 놓치지 않기 위해서다.
  - 윈도우에서는 실행 중 PC가 절전으로 들어가지 않게 한다.
  - 인식한 발언 전부를 스크립트 옆 live_log_날짜.txt 에 시각·방송 경과 시간과 함께
    남긴다. 질의 순서, 키워드 감지, 정회·재개·산회도 표시된다.
"""
import collections, json, os, queue, re, subprocess, sys, threading, time, urllib.error, urllib.request

import speech_recognition as sr
import yt_dlp

# ==================== [ 설정 ] ====================
YOUTUBE_URL = "https://www.youtube.com/watch?v=nCAVxaqGiVM"

# 띄어쓰기는 무시하고 비교한다. 한 발언에 여러 개가 걸리면 모두 알린다.
# '피엠'은 'PM'을 음성 인식이 한글로 받아 적은 형태다.
KEYWORDS = [
    "개인형 이동장치", "개인형 이동수단", "퍼스널 모빌리티", "피엠",
    "공유 킥보드", "전동킥보드", "킥보드", "킥라니", "공유 모빌리티",
]

RATE, WIDTH = 16000, 2             # 16kHz, 16bit 모노
BPS = RATE * WIDTH                 # 초당 바이트
CHUNK_SEC, OVERLAP_SEC = 15, 3     # 15초 구간, 3초 겹침
COOLDOWN_SEC = 600                 # 같은 키워드는 10분에 한 번만 알림
FOLLOW_CHUNKS = 2                  # 감지 후 더 들을 구간 수(취지 파악용)
CONTEXT_BEFORE_SEC = 30            # 알림에 붙일 감지 앞쪽 발언 길이
LINK_LEAD_SEC = 40                 # '방송 열기'를 발언보다 이만큼 앞에서 시작한다.
                                   # 유튜브 생중계 지연(10~30초)과 앞 맥락을 덮는다.
# ==================================================

HERE = os.path.dirname(os.path.abspath(__file__))


def load_secret(env_name, filename, legacy_env=None):
    val = os.environ.get(env_name, "").strip()
    if not val and legacy_env:
        val = os.environ.get(legacy_env, "").strip()
    path = os.path.join(HERE, filename)
    if not val and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:      # 메모장이 붙이는 BOM 제거
            val = f.read().strip().strip('"').strip("'")
    return val


SLACK_WEBHOOK = load_secret("SLACK_LIVE_WEBHOOK_URL", "live_webhook.txt",
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
    path = os.path.join(HERE, time.strftime("live_log_%Y-%m-%d.txt", time.localtime(at)))
    with _log_lock:
        try:
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

# 호명: "김철수 위원님 질의해 주시기 바랍니다", "다음은 이영희 위원 보충질의" 등
CALL_RE = re.compile(r"(?:^|\s)([가-힣]{2,4})\s?위원(?:님)?(?:께서|의)?\s?(?:보충\s?)?(?:질의|질문|발언|주질의)")
NOT_NAMES = {"다음", "다음은", "존경하는", "여러", "상임", "전문", "소속", "모든", "각", "해당",
             "그", "이", "저", "우리", "선배", "동료", "여야", "야당", "여당", "민주당", "국민의힘"}
CALL_STALE_SEC = 15 * 60           # 호명 후 이만큼 지나면 '바뀌었을 수 있음'을 붙인다

# 발언 성격 추정: 문장 끝 어미로 질의/답변을 가른다.
ANSWER_RE = re.compile(r"(답변\s?드리|말씀\s?드리겠|말씀\s?드립니다|검토하겠습니다|"
                       r"살펴보겠습니다|조치하겠습니다|노력하겠습니다|그렇습니다|맞습니다)")
QUESTION_RE = re.compile(r"(습니까|십니까|입니까|겠습니까|나요|어떻게\s?생각|아십니까|않습니까)")
CHAIR_RE = re.compile(r"(위원장입니다|정회|속개|산회|개의|의사일정|질의해\s?주시기|질의하십시오)")

# 취지 태그: 앞뒤 1분 대화록에 나온 단어로 붙인다. 단어 빈도일 뿐 판단이 아니다.
INTENT_TAGS = [
    ("안전·사고", ["사고", "사망", "부상", "안전", "헬멧", "안전모"]),
    ("단속·처벌", ["단속", "과태료", "처벌", "범칙금", "무면허", "적발"]),
    ("주차·방치", ["방치", "주차", "견인", "보도", "통행", "불법 주차"]),
    ("법·제도", ["법안", "개정", "입법", "규제", "면허", "제도", "기준", "시행령", "법률"]),
    ("대책 촉구", ["대책", "강구", "마련", "촉구", "해야", "필요"]),
    ("사업자·업계", ["업체", "사업자", "운영사", "대여", "업계", "플랫폼"]),
    ("청소년", ["청소년", "학생", "미성년", "10대", "중학생", "고등학생"]),
    ("지자체", ["지자체", "시청", "구청", "조례", "서울시"]),
]

# 감사 종료: '산회'는 그날 회의를 끝낼 때만 쓰고 점심 '정회'와 다르다.
# 이 말을 들어도 바로 끄지 않는다 — 방송까지 끝난 걸 확인한 뒤에 끈다.
ADJOURN_RE = re.compile(r"(산회를?\s?선포|산회하겠습니다|국정감사를?\s?모두\s?마치)")

current_call = None                # (이름, 호명 시각, 호명 문장)


def watch_session(text):
    if "정회" in text and "선포" in text:
        log("⏸ 정회 선포 감지 — 꺼지지 않고 속개를 기다립니다.")
        record("\n===== ⏸ 정회 선포 =====")
    if ADJOURN_RE.search(text) and "정회" not in text and not adjourned.is_set():
        adjourned.set()
        log("🏁 산회(감사 종료) 선포 감지 — 방송이 끝나면 감시를 마칩니다.")
        record("\n===== 🏁 산회(감사 종료) 선포 =====")
        send_slack("🏁 산회(감사 종료) 선포 감지 — 방송 송출이 끝나면 감시를 마칩니다.")


def update_speaker(text, start):
    global current_call
    for m in CALL_RE.finditer(text):
        name = m.group(1)
        if name not in NOT_NAMES:
            current_call = (name, start, text)
            log(f"👤 질의 순서 바뀜: {name} 위원")
            record(f"\n----- 👤 {name} 위원 질의 순서 ({hhmmss(start)}) -----", start)


def speaker_line(at):
    if not current_call:
        return "불명 (감시 시작 후 위원장 호명을 아직 듣지 못함)"
    name, t, _ = current_call
    mins = int((at - t) // 60)
    line = f"{name} 위원 질의 순서 ({hhmmss(t)} 호명)"
    if at - t > CALL_STALE_SEC:
        line += f" — 호명 후 {mins}분 지나 바뀌었을 수 있음"
    return line


def kind_of(text):
    if CHAIR_RE.search(text):
        return "위원장 진행"
    a, q = bool(ANSWER_RE.search(text)), bool(QUESTION_RE.search(text))
    if q and not a:
        return "위원 질의로 보임"
    if a and not q:
        return "정부·증인 답변으로 보임"
    return "판별 어려움"


def intent_tags(text):
    flat = text.replace(" ", "")
    return [tag for tag, words in INTENT_TAGS if any(w.replace(" ", "") in flat for w in words)]


def moment_link(at):
    """발언 시점으로 가는 유튜브 링크. 시작 시각을 모르면 실시간 링크."""
    vid, t0 = stream_meta.get("id"), stream_meta.get("start")
    if not (vid and t0):
        return YOUTUBE_URL, None
    offset = max(0, int(at - t0 - LINK_LEAD_SEC))
    h, rem = divmod(offset, 3600)
    return f"https://www.youtube.com/watch?v={vid}&t={offset}s", f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def format_slack(alert, context):
    flow = "\n".join(f"> `{hhmmss(t)}` {s}" for t, s in context)
    tags = intent_tags(" ".join(s for _, s in context))
    lines = [f"🚨 *키워드 감지: {', '.join(alert['keywords'])}*"]
    if stream_title:
        lines.append(f"*회의*  {stream_title}")
    lines += [
        f"*발언 시각*  {hhmmss(alert['start'])}경 (PC 수신 기준)",
        f"*발언자(추정)*  {alert['speaker']}",
        f"*발언 성격(추정)*  {kind_of(alert['text'])}",
        f"*취지 태그*  {' · '.join(tags) if tags else '해당 없음'}  _(앞뒤 발언 단어 기준 자동 분류)_",
        "*발언 흐름(음성 인식 원문)*",
        flow,
        link_line(alert),
    ]
    return "\n".join(lines)


def link_line(alert):
    url, pos = alert["link"]
    if pos:
        return f"<{url}|▶ 발언 지점부터 보기 ({pos})>  _발언 {LINK_LEAD_SEC}초 전부터 재생_"
    return f"<{url}|▶ 방송 열기(실시간)>  _시작 시각을 몰라 발언 지점 링크를 못 만듦_"


def format_telegram(alert, context):
    tags = intent_tags(" ".join(s for _, s in context))
    lines = ["🚨 [키워드 감지 알림]",
             f"• 키워드: {', '.join(alert['keywords'])}",
             f"• 발언 시각: {hhmmss(alert['start'])}경",
             f"• 발언자(추정): {alert['speaker']}",
             f"• 발언 성격(추정): {kind_of(alert['text'])}",
             f"• 취지 태그: {', '.join(tags) if tags else '해당 없음'}",
             "• 발언 흐름:"]
    lines += [f"  {hhmmss(t)} {s}" for t, s in context]
    url, pos = alert["link"]
    lines.append(f"• 방송({pos} 지점): {url}" if pos else f"• 방송(실시간): {url}")
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
def check_keywords(text, start):
    nospace = text.replace(" ", "")
    hits = [k for k in KEYWORDS if k.replace(" ", "") in nospace]
    if not hits:
        return
    now = time.time()
    fresh = [k for k in hits if now - last_alert.get(k, 0) > COOLDOWN_SEC]
    # 같은 발언에 걸린 키워드는 모두 대기를 시작한다 — 겹친 구간에서 다시 잡혀도 조용하다.
    for k in hits:
        last_alert[k] = now
    if not fresh:
        log(f"(대기 중이라 생략: {', '.join(hits)})")
        return
    print("\n" + "=" * 50, flush=True)
    log(f"🚨 키워드 감지: {', '.join(fresh)} — 뒤 발언을 {FOLLOW_CHUNKS}구간 더 듣고 보냅니다")
    log(f"🗣️ {text}")
    print("=" * 50 + "\n", flush=True)
    record(f"   ↑ 🚨 키워드 감지: {', '.join(fresh)}", start)
    pending.append({"keywords": fresh, "text": text, "start": start,
                    "speaker": speaker_line(start), "link": moment_link(start),
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
            text = r.recognize_google(sr.AudioData(pcm, RATE, WIDTH), language="ko-KR")
            log(text)
            record_utterance(text, start)
            transcript.append((start, text))
            while transcript and transcript[0][0] < start - 300:
                transcript.popleft()
            update_speaker(text, start)
            watch_session(text)
            check_keywords(text, start)
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
    if SLACK_WEBHOOK and not SLACK_WEBHOOK.startswith("https://hooks.slack.com/"):
        log(f"⚠️ 슬랙 웹훅 주소 형식이 이상합니다: {SLACK_WEBHOOK[:40]}...")
    log(f"키워드: {', '.join(KEYWORDS)}")
    log("끝내려면 Ctrl+C. 정회로 방송이 멈춰도 꺼지지 않고 재개를 기다립니다.")
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
        if not greeted:
            record(f"\n\n##### 감시 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} — "
                   f"{stream_title or YOUTUBE_URL}\n##### {YOUTUBE_URL}")
            log(f"📝 발언 기록: {os.path.join(HERE, time.strftime('live_log_%Y-%m-%d.txt'))}")
            # 연결 확인용. 이게 안 오면 키워드를 기다릴 필요 없이 알림 설정부터 봐야 한다.
            hello = f"✅ 생중계 키워드 감시 시작 — {stream_title or YOUTUBE_URL}"
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
