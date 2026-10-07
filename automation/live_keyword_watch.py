# -*- coding: utf-8 -*-
"""유튜브 생중계(국회 회의 등) 음성을 계속 듣다가 PM 키워드가 나오면 알린다.

알림은 텔레그램과 '개인' 슬랙으로 간다. 팀 채널로 가는 SLACK_WEBHOOK_URL 은
일부러 읽지 않는다 — 음성 인식 오탐이 섞이는 알림이라 본인만 받는다.

GitHub Actions 에서는 돌리지 않는다. 유튜브가 데이터센터 IP 의 yt-dlp 요청을
봇으로 막는 일이 잦고, 작업 시간도 6시간으로 잘린다. 회의 날 개인 PC 에서 켠다.

    pip install -r automation/requirements-live.txt   # ffmpeg 는 따로 설치
    export SLACK_PERSONAL_WEBHOOK_URL="https://hooks.slack.com/services/..."
    export TELEGRAM_TOKEN="123456789:ABC..."        # 선택
    export TELEGRAM_CHAT_ID="987654321"             # 선택
    python automation/live_keyword_watch.py "https://www.youtube.com/watch?v=..."

주소를 생략하면 아래 YOUTUBE_URL 을 쓴다. 알림 수단이 하나도 설정돼 있지 않으면
콘솔에만 출력한다.

예전 판(15초 녹음 → 인식 → 반복)에서 고친 점:
  - ffmpeg 를 한 번만 띄워 계속 듣는다. 예전엔 주소 추출·인식하는 동안 방송의
    30~40%를 듣지 못했다.
  - 15초 구간을 3초씩 겹친다. 구간 경계에 걸린 발언도 잡힌다.
  - 인식은 별도 스레드에서 한다. 인식이 느려도 녹음이 멈추지 않는다.
  - 임시 wav 파일을 쓰지 않는다. 예전엔 ffmpeg 가 실패하면 지난 녹음을 다시 인식했다.
  - 중복 방지는 '키워드별 10분 대기'다. 예전의 '문장이 완전히 같으면 생략'은
    인식 결과가 매번 달라 사실상 걸러지는 게 없었다.
  - 스트림이 끊기면 다시 접속한다.
"""
import json, os, queue, subprocess, sys, threading, time, urllib.request

import speech_recognition as sr
import yt_dlp

# ==================== [ 설정 ] ====================
YOUTUBE_URL = "https://www.youtube.com/watch?v=nCAVxaqGiVM"

SLACK_PERSONAL_WEBHOOK_URL = os.environ.get("SLACK_PERSONAL_WEBHOOK_URL", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

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
# ==================================================

last_alert = {}                    # keyword -> 마지막 알림 시각


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def post_json(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def send_alerts(keywords, text):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if SLACK_PERSONAL_WEBHOOK_URL:
        msg = (f"🚨 *생중계 키워드 감지* — {', '.join(keywords)}\n"
               f"> {text}\n"
               f"{stamp} · <{YOUTUBE_URL}|방송 열기>")
        try:
            post_json(SLACK_PERSONAL_WEBHOOK_URL, {"text": msg})
            log("슬랙(개인) 전송 성공")
        except Exception as e:
            log(f"슬랙(개인) 전송 실패: {e}")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        msg = (f"🚨 [키워드 감지 알림]\n"
               f"• 감지 키워드: {', '.join('#' + k.replace(' ', '') for k in keywords)}\n"
               f"• 방송 발언: \"{text}\"\n"
               f"• 시각: {stamp}\n"
               f"• 방송: {YOUTUBE_URL}")
        try:
            post_json(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      {"chat_id": TELEGRAM_CHAT_ID, "text": msg})
            log("텔레그램 전송 성공")
        except Exception as e:
            log(f"텔레그램 전송 실패: {e}")


def check_keywords(text):
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
    log(f"🚨 키워드 감지: {', '.join(fresh)}")
    log(f"🗣️ {text}")
    print("=" * 50 + "\n", flush=True)
    send_alerts(fresh, text)


def stt_worker(q):
    r = sr.Recognizer()
    while True:
        pcm = q.get()
        try:
            text = r.recognize_google(sr.AudioData(pcm, RATE, WIDTH), language="ko-KR")
            log(text)
            check_keywords(text)
        except sr.UnknownValueError:
            pass                       # 무음이거나 알아듣지 못함
        except sr.RequestError as e:
            log(f"STT 오류: {e}")
        except Exception as e:         # 스레드가 죽으면 조용히 인식이 멈추므로 다 잡는다
            log(f"인식 중 오류: {e}")


def get_live_audio_url(url):
    """유튜브 생중계의 스트림 주소(HLS)를 꺼낸다."""
    opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info.get("is_live"):
        log("⚠️ 생중계가 아닙니다(다시보기면 처음부터 재생하며 듣습니다).")
    return info["url"]


def monitor_live_stream():
    channels = [n for n, ok in (("슬랙(개인)", SLACK_PERSONAL_WEBHOOK_URL),
                                ("텔레그램", TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)) if ok]
    log(f"📡 모니터링 시작: {YOUTUBE_URL}")
    log(f"알림: {', '.join(channels) if channels else '없음 — 콘솔에만 출력'}")

    q = queue.Queue(maxsize=20)
    threading.Thread(target=stt_worker, args=(q,), daemon=True).start()

    while True:                        # 바깥 루프 = 끊겼을 때 재접속
        try:
            stream_url = get_live_audio_url(YOUTUBE_URL)
        except Exception as e:
            log(f"스트림 주소 추출 실패: {e} — 30초 후 재시도")
            time.sleep(30)
            continue

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
                    try:
                        q.put_nowait(buf)
                    except queue.Full:
                        log("⚠️ 인식이 밀려 구간 하나를 건너뜀")
                    buf = buf[-OVERLAP_SEC * BPS:]    # 마지막 3초는 다음 구간에도
        finally:
            proc.kill()
            proc.wait()
        log("스트림 끊김 — 10초 후 재접속")
        time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        YOUTUBE_URL = sys.argv[1]
    try:
        monitor_live_stream()
    except KeyboardInterrupt:
        log("종료")
