# -*- coding: utf-8 -*-
"""유튜브 생중계(국정감사·상임위 등) 음성을 계속 듣다가 키워드가 나오면 슬랙으로 알린다.

알림에는 발언 시각, 발언자(추정), 발언 요지·취지 분석이 함께 붙는다. 분석은
Claude API 로 하며, 키가 없으면 분석 없이 원문만 보낸다.

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
    live_anthropic_key.txt  Claude API 키(선택)   (ANTHROPIC_API_KEY)
텔레그램은 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 환경변수로 켠다(선택).

시작할 때 각 알림 수단으로 '감시 시작' 메시지를 한 번 보낸다. 이게 안 오면
키워드를 기다릴 것 없이 연결부터 잘못된 것이다. 화면에 실패 이유가 찍힌다.

동작 방식:
  - ffmpeg 를 한 번만 띄워 계속 듣고, 15초 구간을 3초씩 겹쳐 인식한다.
  - 인식은 별도 스레드에서 한다. 인식이 느려도 녹음이 멈추지 않는다.
  - 키워드가 나오면 바로 보내지 않고 다음 두 구간(약 25초)을 더 듣는다. 발언 취지는
    키워드 뒤에 나오는 경우가 많아서다. 그 뒤 앞뒤 대화록을 붙여 분석하고 보낸다.
  - 같은 키워드는 10분에 한 번만 알린다.
  - 스트림이 끊기면 다시 접속한다.
"""
import collections, json, os, queue, subprocess, sys, threading, time, urllib.error, urllib.request

import speech_recognition as sr
import yt_dlp

try:
    import anthropic
except ImportError:                    # 분석은 선택 기능이라 없어도 돈다
    anthropic = None

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
CONTEXT_SEC = 8 * 60               # 분석에 넘길 앞쪽 대화록 길이. 국감 질의 한 순서(7분)를
                                   # 덮어야 위원장의 '○○○ 위원님 질의하십시오'가 들어온다.
CLAUDE_MODEL = "claude-opus-5-5"
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
ANTHROPIC_KEY = load_secret("ANTHROPIC_API_KEY", "live_anthropic_key.txt")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

last_alert = {}                    # keyword -> 마지막 알림 시각
transcript = collections.deque()   # (구간 시작 시각, 문장) — 최근 CONTEXT_SEC 만 보관
pending = []                       # 뒤 구간을 기다리는 감지 건
stream_title = ""                  # 유튜브 방송 제목(회의명 파악용)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def hhmmss(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


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


# -------------------- 발언 분석 --------------------
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker": {"type": "string"},
        "speaker_basis": {"type": "string"},
        "speaker_confidence": {"type": "string", "enum": ["높음", "중간", "낮음"]},
        "role": {"type": "string", "enum": ["위원 질의", "정부 답변", "위원장 진행", "참고인·증인", "기타"]},
        "summary": {"type": "string"},
        "intent": {"type": "string"},
        "implication": {"type": "string"},
    },
    "required": ["speaker", "speaker_basis", "speaker_confidence", "role",
                 "summary", "intent", "implication"],
    "additionalProperties": False,
}

ANALYSIS_SYSTEM = """\
당신은 국회 회의(국정감사·상임위원회) 생중계를 모니터링하는 대외협력 담당자를 돕는다.
담당자는 공유 전동킥보드 등 개인형 이동장치(PM) 업계에서 일한다.

입력은 음성 인식으로 받아 적은 대화록이다. 오탈자, 띄어쓰기 오류, 동음이의어 오인식이
많고 화자 구분이 없다. 각 줄 앞의 시각은 PC가 방송을 받은 시각이다.

'감지 발언'의 화자를 추정하고, 그 발언의 요지와 취지를 정리하라.

- speaker: 위원장 호명("○○○ 위원님 질의해 주십시오"), 호칭("장관님", "위원님"),
  자기소개, 답변 흐름 같은 단서로 추정한다. 이름은 대화록에 실제로 나온 것만 쓴다.
  단서가 없으면 "불명"이라고 쓰고, 역할만 알면 "질의 위원(이름 불명)"처럼 쓴다.
- speaker_basis: 추정 근거를 대화록 표현을 인용해 한 문장으로.
- summary: 감지 발언과 바로 이어지는 흐름의 요지, 1~2문장.
- intent: 발언자가 무엇을 원하는지(규제 강화 요구, 현황 질타, 대책 촉구, 해명 등), 1~2문장.
- implication: PM 업계 관점의 시사점 한 문장. 직접 관련이 없으면 "직접 관련 없음"이라고 쓴다.
- 음성 인식 오류로 보이는 단어는 문맥상 맞는 말로 읽되, 확신이 없으면 지어내지 마라.
"""


def analyze(alert, context_lines):
    """감지 건을 Claude 로 분석한다. 키가 없거나 실패하면 None."""
    if not (anthropic and ANTHROPIC_KEY):
        return None
    convo = "\n".join(f"[{hhmmss(t)}] {s}" for t, s in context_lines)
    user = (f"방송 제목: {stream_title or '알 수 없음'}\n"
            f"감지 키워드: {', '.join(alert['keywords'])}\n"
            f"감지 발언 [{hhmmss(alert['start'])}]: {alert['text']}\n\n"
            f"대화록(오래된 순):\n{convo}")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            system=ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
            # 안전 분류기가 거절하면 서버가 다른 모델로 다시 돌린다.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        log("❌ 분석 실패: Claude API 키가 올바르지 않습니다")
        return None
    except anthropic.RateLimitError:
        log("❌ 분석 실패: 사용량 한도 초과(잠시 후 다시 됩니다)")
        return None
    except anthropic.APIStatusError as e:
        log(f"❌ 분석 실패: HTTP {e.status_code} {e.message}")
        return None
    except anthropic.APIConnectionError:
        log("❌ 분석 실패: Claude API 에 연결할 수 없습니다")
        return None
    if resp.stop_reason != "end_turn":
        log(f"❌ 분석 실패: 응답이 끝나지 않음({resp.stop_reason})")
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log("❌ 분석 실패: 응답 형식 오류")
        return None


def format_slack(alert, a):
    lines = [f"🚨 *키워드 감지: {', '.join(alert['keywords'])}*"]
    if stream_title:
        lines.append(f"*회의*  {stream_title}")
    lines.append(f"*발언 시각*  {hhmmss(alert['start'])}경 (PC 수신 기준)")
    if a:
        lines += [
            f"*발언자(추정)*  {a['speaker']} · {a['role']} · 확신도 {a['speaker_confidence']}",
            f"      _근거: {a['speaker_basis']}_",
            f"*요지*  {a['summary']}",
            f"*취지*  {a['intent']}",
            f"*PM 시사점*  {a['implication']}",
        ]
    else:
        lines.append("_발언자·취지 분석 없음(Claude API 키 미설정 또는 분석 실패)_")
    lines += [f"*원문(음성 인식)*", f"> {alert['text']}", f"<{YOUTUBE_URL}|▶ 방송 열기>"]
    return "\n".join(lines)


def format_telegram(alert, a):
    lines = ["🚨 [키워드 감지 알림]",
             f"• 키워드: {', '.join(alert['keywords'])}",
             f"• 발언 시각: {hhmmss(alert['start'])}경"]
    if a:
        lines += [f"• 발언자(추정): {a['speaker']} ({a['role']})",
                  f"• 요지: {a['summary']}",
                  f"• 취지: {a['intent']}"]
    lines += [f"• 원문: \"{alert['text']}\"", f"• 방송: {YOUTUBE_URL}"]
    return "\n".join(lines)


def dispatch(alert):
    """분석하고 보낸다. 분석이 수십 초 걸릴 수 있어 별도 스레드에서 돈다."""
    context = [(t, s) for t, s in list(transcript) if t >= alert["start"] - CONTEXT_SEC]

    def run():
        a = analyze(alert, context)
        if a:
            log(f"🧠 분석: {a['speaker']} — {a['summary']}")
        send_slack(format_slack(alert, a))
        send_telegram(format_telegram(alert, a))

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
    pending.append({"keywords": fresh, "text": text, "start": start,
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
            transcript.append((start, text))
            while transcript and transcript[0][0] < start - CONTEXT_SEC - 120:
                transcript.popleft()
            check_keywords(text, start)
        except sr.UnknownValueError:
            pass                       # 무음이거나 알아듣지 못함
        except sr.RequestError as e:
            log(f"STT 오류: {e}")
        except Exception as e:         # 스레드가 죽으면 조용히 인식이 멈추므로 다 잡는다
            log(f"인식 중 오류: {e}")
        advance_pending()


# -------------------- 스트림 --------------------
def get_live_audio_url(url):
    """유튜브 생중계의 스트림 주소(HLS)와 방송 제목을 꺼낸다."""
    opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info.get("is_live"):
        log("⚠️ 생중계가 아닙니다(다시보기면 처음부터 재생하며 듣습니다).")
    return info["url"], info.get("title") or ""


def monitor_live_stream():
    global stream_title
    channels = [n for n, ok in (("슬랙", SLACK_WEBHOOK),
                                ("텔레그램", TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)) if ok]
    log(f"📡 모니터링 시작: {YOUTUBE_URL}")
    log(f"알림: {', '.join(channels) if channels else '없음 — 콘솔에만 출력'}")
    if SLACK_WEBHOOK and not SLACK_WEBHOOK.startswith("https://hooks.slack.com/"):
        log(f"⚠️ 슬랙 웹훅 주소 형식이 이상합니다: {SLACK_WEBHOOK[:40]}...")
    if anthropic is None:
        log("분석: 꺼짐 — anthropic 패키지 없음(pip install anthropic)")
    elif not ANTHROPIC_KEY:
        log("분석: 꺼짐 — Claude API 키 없음(live_anthropic_key.txt)")
    else:
        log(f"분석: 켜짐 ({CLAUDE_MODEL})")
    log(f"키워드: {', '.join(KEYWORDS)}")

    q = queue.Queue(maxsize=20)
    threading.Thread(target=stt_worker, args=(q,), daemon=True).start()
    greeted = False

    while True:                        # 바깥 루프 = 끊겼을 때 재접속
        try:
            stream_url, stream_title = get_live_audio_url(YOUTUBE_URL)
        except Exception as e:
            log(f"스트림 주소 추출 실패: {e} — 30초 후 재시도")
            time.sleep(30)
            continue
        if not greeted:
            # 연결 확인용. 이게 안 오면 키워드를 기다릴 필요 없이 알림 설정부터 봐야 한다.
            hello = f"✅ 생중계 키워드 감시 시작 — {stream_title or YOUTUBE_URL}"
            send_slack(hello)
            send_telegram(hello)
            greeted = True

        proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-i", stream_url,
             "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-"],
            stdout=subprocess.PIPE)
        buf = b""
        try:
            while True:
                data = proc.stdout.read(BPS)          # 1초씩
                if not data:
                    break                             # 방송 종료·주소 만료
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
