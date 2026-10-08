# -*- coding: utf-8 -*-
"""유튜브 생중계(국정감사·상임위 등) 음성을 계속 듣다가 키워드가 나오면 슬랙으로 알린다.

알림에는 발언 시각, 발언자(추정), 앞뒤 발언 흐름, 발언 지점 링크가 붙는다. 분석은 규칙 기반이라
외부 서비스 가입이나 비용이 없다. 발언자는 위원장의 마지막 호명("○○○ 위원님 질의해
주십시오")·위원 자기소개로 추정하고, 위원 명단으로 음성 인식 오탈자를 바로잡는다.

슬랙은 웹훅이 묶인 채널 한 곳으로만 간다(예: #nationalauditrealtimetracker).
팀 공용 알림의 SLACK_WEBHOOK_URL 은 일부러 읽지 않는다.

GitHub Actions 에서는 돌리지 않는다. 유튜브가 데이터센터 IP 의 yt-dlp 요청을
봇으로 막는 일이 잦고, 작업 시간도 6시간으로 잘린다. 회의 날 개인 PC 에서 켠다.

    pip install -r automation/requirements-live.txt   # ffmpeg 는 따로 설치
    python live_keyword_watch.py                      # 원격 지정 주소로 시작(권장)
    python live_keyword_watch.py "https://youtu.be/…" # 직접 지정
    python live_keyword_watch.py --update             # 저장소의 최신 버전으로 바꾸기
    python live_keyword_watch.py --install-startup    # 윈도우 로그인 때 자동으로 켜기(--uninstall-startup)

원격 지정: 이 저장소의 automation/live_target.txt 에 유튜브 주소를 한 줄에 하나씩 적어 두면,
실행 중인 PC가 30초마다 확인해서 1분 안팎에 그 방송(들)을 듣는다. 여러 줄이면 동시에 듣고,
주소 앞에 이름을 붙이면(예: '국토위 https://…') 알림·기록 파일에 그 이름이 붙는다. 'stop' 은
모두 멈춤. 채널 주소(@채널/live)는 받지 않는다. 저장소가 공개라 PC 쪽엔 키가 필요 없다.
끄려면 live_remote.txt 에 off 를 적는다.

구조: 처음 켠 프로그램(관리)은 원격 지정을 보며 방송마다 감시 프로그램(--child)을 하나씩 띄우고,
꺼지면 다시 켜고, 주소가 바뀌면 바꿔 켠다. 감시 프로그램 하나가 방송 하나를 맡는다.

스크립트 옆 파일(.gitignore 처리됨, 같은 이름의 환경변수가 있으면 그쪽이 먼저):
    live_webhook.txt        슬랙 웹훅 주소(SLACK_LIVE_WEBHOOK_URL)
    live_keywords.txt       키워드·대체 표현·문맥 조건(실행 중 고쳐도 반영)
    live_watch_members.txt  질의 시작을 따로 알릴 관심 의원(실행 중 고쳐도 반영)
    live_members.txt        명단에 없는 위원 직접 추가(선택, '이름 위원회')
    live_log_dir.txt        발언 기록·요약을 쓸 폴더(예: 구글 드라이브)
    live_log_link.txt       그 폴더의 공유 링크 — 알림에 붙는다
텔레그램은 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 환경변수로 켠다(선택).

동작 방식:
  - ffmpeg 를 한 번만 띄워 계속 듣고, 15초 구간을 3초씩 겹쳐 인식한다(인식은 별도 스레드).
  - 키워드가 나오면 약 25초 더 듣고 앞뒤 발언을 붙여 보낸다. 25초 안의 언급은 한 알림으로 묶는다.
  - 발음 보정: 다른 인식 후보와 자모 단위 유사 표현까지 본다. 뜻이 여럿인 말('PM', '자전거')은
    문맥 조건이 맞을 때만 알린다.
  - '방송 열기' 링크는 발언 시점(40초 전)으로 간다.
  - 정회·속개(국감은 '감사중지/감사를 계속')를 채널에 알린다. 정회로 송출이 끊겨도 꺼지지 않는다.
  - 방송이 끝나면 같은 채널에서 위원회·날짜가 같은 생중계(2부)를 찾아 이어 듣는다. 위원장이
    예고한 재개 시각이 10분 지나도 못 들으면 알린다.
  - 위원장의 산회(감사 종료) 선포 후 송출까지 끝나면 그 방송 감시를 마치고 요약을 보낸다.
  - 오래 인식이 없거나 음성 인식 오류가 이어지면 알리고, 매시간 상태를 보고한다.
  - 마감 요약(질의 순서·키워드 알림·정회)을 슬랙과 logs/live_summary_날짜.md 로 보낸다.
  - 인식한 발언 전부를 logs/live_log_날짜.txt 에 시각·방송 경과 시간과 함께 남긴다.
  - 저장소에 새 버전이 올라오면 알린다. 윈도우에서는 실행 중 PC가 절전으로 들어가지 않게 한다.
"""
import collections, json, os, queue, re, subprocess, sys, threading, time, traceback, urllib.error, urllib.parse, urllib.request

import speech_recognition as sr
import yt_dlp

VERSION = "2026.10.08-1"            # 슬랙 시작 메시지와 새 버전 알림에 쓴다

# ==================== [ 설정 ] ====================
YOUTUBE_URL = "https://www.youtube.com/watch?v=nCAVxaqGiVM"   # 원격 지정도 명령줄 주소도 없을 때

# 원격 지정 파일 위치. 공개 저장소라 인증 없이 읽는다.
# 깃허브 API 는 인증이 없으면 시간당 60회(304 응답 포함)라 30초 주기를 못 버티고,
# raw 주소는 5분 동안 캐시된다. 그래서 git 프로토콜로 브랜치의 최신 커밋 번호만 묻고
# (호출 한도 없음, 1~2KB), 번호가 바뀌었을 때만 그 커밋에 고정된 raw 주소로 파일을 읽는다.
REMOTE_REPO = "ms1238/pm-legislation-tracker"
REMOTE_BRANCH = "claude/compassionate-hypatia-3an51t"
REMOTE_PATH = "automation/live_target.txt"
REMOTE_POLL_SEC = 30

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
SILENT_ALERT_SEC = 10 * 60         # 회의 진행 중인데 이만큼 인식된 발언이 없으면 경고
STT_ERROR_ALERT = 5                # 음성 인식 오류가 이만큼 연달아 나면 경고
HEARTBEAT_MIN = 60                 # 상태 보고 주기(분). 0 이면 끈다
FOLLOW_SEARCH_SEC = 120            # 방송이 끝났을 때 같은 회의의 다음 방송(2부)을 찾는 주기
RESUME_GRACE_SEC = 10 * 60         # 예고된 재개 시각에서 이만큼 지나도 못 들으면 경고
UPDATE_CHECK_SEC = 30 * 60         # 새 버전 확인 주기
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
REMOTE_ON = load_secret("LIVE_REMOTE", "live_remote.txt")[0].strip().lower() not in (
    "off", "0", "false", "no", "끔")

last_alert = {}                    # keyword -> 마지막 알림 시각
transcript = collections.deque()   # (구간 시작 시각, 문장) — 최근 몇 분만 보관
pending = []                       # 뒤 구간을 기다리는 감지 건
stream_title = ""                  # 유튜브 방송 제목(회의명 파악용)
stream_meta = {"title": "", "id": "", "start": None}
adjourned_gen = None               # 산회·감사 종료 선포를 들은 감시 대상(target gen)


# 여러 방송을 동시에 들을 때 프로그램은 방송마다 하나씩 뜬다(--child). 그때 화면·기록 파일·
# 슬랙 메시지에 붙일 방송 이름(예: 국토위). 하나만 들을 때는 비어 있다.
SLOT_LABEL = ""


def label_suffix():
    return f"_{re.sub(r'[^0-9A-Za-z가-힣]+', '', SLOT_LABEL)}" if SLOT_LABEL else ""


def log(msg):
    tag = f"[{SLOT_LABEL}] " if SLOT_LABEL else ""
    print(f"[{time.strftime('%H:%M:%S')}] {tag}{msg}", flush=True)


def hhmmss(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


# -------------------- 발언 기록 파일 --------------------
# 인식한 발언을 날짜별 텍스트 파일에 이어 쓴다. 같은 날 다시 켜도 같은 파일에 붙는다.
# 구간이 3초씩 겹쳐서 이웃한 줄의 끝·처음 몇 단어가 겹칠 수 있다.
_log_lock = threading.Lock()


def record(line, at=None):
    at = at or time.time()
    path = os.path.join(LOG_DIR, time.strftime("live_log_%Y-%m-%d", time.localtime(at))
                        + label_suffix() + ".txt")
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            log(f"⚠️ 기록 파일 쓰기 실패: {e}")


def stream_pos(at, meta=None):
    """방송 경과 시간(h:mm:ss). 시작 시각을 모르면 빈 문자열."""
    t0 = (meta or stream_meta).get("start")
    if not t0:
        return ""
    sec = max(0, int(at - t0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def record_utterance(text, at, meta=None):
    pos = stream_pos(at, meta)
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
    if SLOT_LABEL:
        msg = f"`{SLOT_LABEL}` {msg}"
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


# -------------------- 회의 기록(마감 보고서·상태 보고용) --------------------
_ev_lock = threading.Lock()
events = []                        # 지금 회의(1부·2부 포함)에서 일어난 일 — 마감 보고서 재료
stats = {"chunks": 0, "texts": 0, "stt_errors": 0, "consec_err": 0, "alerts": 0,
         "suppressed": 0, "last_text": None, "listen_since": None}


def add_event(kind, t, **kw):
    with _ev_lock:
        events.append(dict(kind=kind, t=t, **kw))


# -------------------- 위원 명단(이름 보정)·관심 의원 --------------------
# 음성 인식은 위원 이름을 자주 틀린다(2026-10-07 국토위: 질의 순서 16건 중 약 7건 — '목용',
# '김해', '복귀한', '저녁에' …). 이 저장소의 법안 트래커가 관리하는 의원 명단(위원회 포함)에서
# 발음이 가장 가까운 이름으로 바로잡는다. 명단에 없는 사람을 엉뚱한 위원으로 바꾸지 않게,
# 차이가 충분히 작고 2등 후보와 구분될 때만 바꾸고 원래 인식된 표기를 함께 보여 준다.
ROSTER_URL = f"https://raw.githubusercontent.com/{REMOTE_REPO}/master/automation/member_snapshot.json"
ROSTER_CACHE = os.path.join(HERE, "live_roster_cache.json")
EXTRA_MEMBERS_FILE = os.path.join(HERE, "live_members.txt")      # 명단에 없는 위원을 직접 추가(선택)
WATCH_FILE = os.path.join(HERE, "live_watch_members.txt")        # 질의 시작을 따로 알릴 관심 의원
DEFAULT_WATCH_FILE = """\
# 질의를 시작하면 슬랙으로 따로 알릴 관심 의원 — 한 줄에 한 명. 저장하면 바로 반영됩니다.
염태영
"""
# 방송 제목의 위원회 약칭 → 명단의 위원회 이름에 들어 있는 말
COMMITTEE_ABBR = {"국토위": "국토교통", "행안위": "행정안전", "과방위": "과학기술정보", "산자위": "산업통상",
                  "산자중기위": "산업통상", "정무위": "정무", "기재위": "기획재정", "재경위": "재정경제",
                  "법사위": "법제사법", "환노위": "노동", "교육위": "교육", "문체위": "문화체육",
                  "농해수위": "농림", "복지위": "보건복지", "외통위": "외교통일", "국방위": "국방",
                  "여가위": "여성가족", "성평등가족위": "성평등가족", "운영위": "운영", "정보위": "정보"}
roster = {}                        # 이름 → 위원회(쉼표로 이어진 문자열)
watch_members = set()
_watch_mtime = None


def _read_text(path):
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp949")


def load_roster():
    """명단을 저장소에서 받아 오고(실패하면 지난번 받아 둔 것), 직접 추가한 이름을 더한다."""
    global roster
    data = {}
    try:
        data = json.loads(_http_get(ROSTER_URL).decode("utf-8"))
        with open(ROSTER_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        try:
            data = json.load(open(ROSTER_CACHE, encoding="utf-8"))
            log(f"위원 명단: 저장소에서 못 받아 지난번 것을 씀({short_error(e)})")
        except (OSError, ValueError):
            log(f"⚠️ 위원 명단을 받지 못함 — 이름 보정 없이 갑니다({short_error(e)})")
    new = {n: (v.get("committee") or "") for n, v in data.items() if isinstance(v, dict)}
    if os.path.exists(EXTRA_MEMBERS_FILE):
        for ln in _read_text(EXTRA_MEMBERS_FILE).splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                name, _, comm = ln.partition(" ")
                new.setdefault(name, comm.strip())
    roster = new
    log(f"위원 명단: {len(roster)}명 (이름 보정에 씀)")


def refresh_watch():
    """live_watch_members.txt 가 바뀌었으면 다시 읽는다(없으면 만든다)."""
    global watch_members, _watch_mtime
    try:
        if not os.path.exists(WATCH_FILE):
            with open(WATCH_FILE, "w", encoding="utf-8") as f:
                f.write(DEFAULT_WATCH_FILE)
        mtime = os.path.getmtime(WATCH_FILE)
        if mtime == _watch_mtime:
            return
        names = {ln.split("#", 1)[0].strip() for ln in _read_text(WATCH_FILE).splitlines()}
        names.discard("")
        if _watch_mtime is not None:
            log(f"🔄 관심 의원 갱신: {', '.join(sorted(names)) or '없음'}")
        watch_members, _watch_mtime = names, mtime
    except OSError as e:
        log(f"⚠️ 관심 의원 파일을 읽지 못함: {e}")


def committee_hint(title):
    """방송 제목에서 위원회 약칭(국토위 등)을 찾아 명단 검색어로 바꾼다. 모르면 None."""
    for abbr in sorted(COMMITTEE_ABBR, key=len, reverse=True):
        if abbr in (title or ""):
            return COMMITTEE_ABBR[abbr]
    m = re.search(r"([가-힣]{2,})위원회", title or "")
    return m.group(1) if m else None


def committee_pool(meta=None):
    """(이 방송 위원회의 명단, 명단이 충분한가). 충분하면 명단에 없는 이름은 오인식으로 본다."""
    hint = committee_hint((meta or stream_meta).get("title"))
    pool = [n for n, c in roster.items() if hint and hint in c] if hint else []
    return pool, len(pool) >= 10


def snap_name(name, meta=None):
    """음성 인식된 이름을 명단의 가장 가까운 이름으로 바로잡는다. 못 바꾸면 None."""
    pool, loose = committee_pool(meta)   # 그 위원회 명단이 충분할 때만 넉넉하게 바로잡는다
    if not loose:
        pool = list(roster) + sorted(watch_members)
    pool = list(dict.fromkeys(pool + sorted(watch_members)))
    if not pool or name in pool:
        return name if name in pool else None
    wj = to_jamo(name)[0]
    scored = sorted((_edit_distance(wj, to_jamo(c)[0], 6), c) for c in pool)
    best_d, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 99
    limit = min(4, max(1, len(to_jamo(best)[0]) // 2)) if loose else 1
    if best_d <= limit and second > best_d:
        return best
    return None


# -------------------- 발언 분석(규칙 기반, 무료) --------------------
# 음성 인식은 화자를 구분하지 못한다. 대신 국감·상임위 질의는 위원장이
# "○○○ 위원님 질의해 주십시오"라고 호명한 뒤 그 위원과 증인이 몇 분간 주고받는
# 구조라, 마지막 호명을 기억해 두면 '지금 누구의 질의 순서인지'는 꽤 맞힌다.

# 질의 위원을 알아내는 단서 세 가지(앞에 띄어쓰기나 문장 시작이 있어야 이름으로 본다).
#  - 위원장 호명: "김철수 위원님 질의해 주십시오", "이영희 위원님 순서입니다", "박민수 위원 하십시오"
#  - 위원장 예고: "다음은 (○○당) 김철수 위원"
#  - 위원 자기소개: "국민의힘 김철수 위원입니다", "김철수 의원입니다"
_NAME = r"(?:^|\s)([가-힣]{2,4})\s?"
# 2026-10-07 국토위 실제 기록에서 위원장은 '위원님'과 '의원님'을 섞어 썼고("다음은 장경태
# 의원님"), 음성 인식은 '질의해'를 '치료해'로 자주 적었다. 자기소개는 "김남근 국회의원입니다".
# 증인 신문 시간에는 "○○○ 위원님 신문해 주시기 바랍니다"(인식: '신문에 주시기')라고 부른다.
CALL_RES = [
    re.compile(_NAME + r"(?:위원|의원)님?\s?(?:께서\s?)?(?:보충\s?|추가\s?)?"
               r"(?:질의|질문|발언|순서|신문|하십시오|해\s?주십시오|해\s?주시기|말씀해|치료해)"),
    re.compile(r"다음은?\s?(?:[가-힣]+당\s?)?" + _NAME.replace("(?:^|\\s)", "") + r"(?:위원|의원)"),
    re.compile(_NAME + r"(?:국회\s?)?(?:위원|의원)입니다"),
]
CHAIR_CALLS = (0, 1)               # 위원장 호명 패턴 — 이름을 못 알아들어도 순서는 바뀐 것
NOT_NAMES = {"다음", "다음은", "존경하는", "여러", "상임", "전문", "소속", "모든", "각", "해당",
             "그", "이", "저", "우리", "선배", "동료", "여야", "야당", "여당", "민주당", "국민의힘",
             "보충", "추가", "질의", "전체", "간사", "소위", "정부", "관계", "여러분", "위원장",
             "국회", "남은"}
# 음성 인식이 이름을 다른 말로 적은 경우("다음 저녁에 위원님 질의해 주시기 바랍니다").
# 이름으로 쓰지 않되, 위원장 호명이면 질의 순서가 바뀐 건 맞으니 '이름 미확인'으로 바꾼다.
BAD_NAME_WORDS = {"오늘", "내일", "어제", "오전", "오후", "아침", "점심", "저녁", "지금", "이번", "먼저"}
BAD_NAME_ENDS = ("에", "께", "에서", "에게", "부터")
CALL_STALE_SEC = 15 * 60           # 호명 후 이만큼 지나면 '바뀌었을 수 있음'을 붙인다
def state_file():
    """다시 켜도 질의 위원을 이어받기 위한 파일(방송 이름별로 따로)."""
    return os.path.join(HERE, f"live_state{label_suffix()}.json")

# 질의인지 답변인지: 정부·증인을 부르면 위원의 질의, 답변 어투면 정부·증인 쪽.
ADDRESS_RE = re.compile(r"(장관님|차관님|청장님|사장님|원장님|이사장님|본부장님|실장님|국장님|"
                        r"회장님|대표님|증인|참고인)")
QUESTION_RE = re.compile(r"(습니까|십니까|겠습니까|않습니까|아닙니까|어떻게\s?생각|여쭙|묻겠습니다|"
                         r"답변해\s?주십시오|말씀해\s?주십시오)")
ANSWER_RE = re.compile(r"(답변\s?드리|말씀\s?드리겠|말씀\s?드립니다|검토하겠습니다|"
                       r"살펴보겠습니다|조치하겠습니다|노력하겠습니다|위원님\s?말씀)")

# 감사 종료: '산회'는 그날 회의를 끝낼 때만 쓰고 점심 '정회'와 다르다.
# 이 말을 들어도 바로 끄지 않는다 — 방송까지 끝난 걸 확인한 뒤에 끈다.
# 국정감사에서는 '감사종료를 선포'도 쓴다. '모두 마치고 …'처럼 이어지는 말은 끝이 아니라서
# '마치겠습니다'까지 들어야 한다.
ADJOURN_RE = re.compile(r"(산회를?\s?선포|산회하겠습니다|국정감사를?\s?모두\s?마치겠습니다|"
                        r"감사\s?종료를?\s?선포)")

current_call = None                # (이름, 호명 시각, 호명 문장)


# 정회·속개를 채널에 알린다. 같은 상태를 두 번 알리지 않도록 상태가 바뀔 때만 보낸다.
# 근거는 두 가지다: 위원장의 선포(음성 인식)와 방송 송출 멈춤·재개.
# 상임위는 '정회/속개'를, 국정감사는 '감사중지/감사를 계속'을 쓴다(2026-10-07 국토위 실제
# 기록: "잠시 감사를 중지했다가 … 국정감사 중지를 선포합니다" → 음성 인식은 '중기'로 적었다,
# "국정감사를 계속하도록 하겠습니다"). 정회 중에도 방송 송출은 계속돼서 말로만 알 수 있다.
RECESS_RE = re.compile(r"(정회를?\s?선포|정회하겠습니다|정회하도록\s?하겠습니다|"
                       r"(?:감사\s?)?중[지기]를?\s?선포(?:합니다|하겠습니다|함)|"
                       r"감사를?\s?(?:잠시\s?)?(?:중지|중단)(?:하겠|하도록|했다가|하고))")
RESUME_RE = re.compile(r"(속개하겠습니다|속개를?\s?선포|(?:회의|감사)를?\s?속개|속개하도록|"
                       r"개의를?\s?선포|개의하겠습니다|감사를?\s?계속(?:하도록\s?하|하)겠습니다|"
                       r"감사를?\s?재개|질의를?\s?계속하겠습니다)")
# "오후 5시 20분에 감사를 계속하도록 하겠습니다"는 정회 안내다 — 재개로 보면 안 된다.
FUTURE_TIME_RE = re.compile(r"(?:\d+|[한두세네다섯여섯일곱여덟아홉열]+)\s?시\s?(?:\d+\s?분|반)?\s?(?:부터|에)")
PAUSE_NOTICE_SEC = 180             # 선포 없이 송출이 이만큼 멈추면 정회로 보고 알린다
session_state = "unknown"          # unknown / 진행 / 정회
_state_lock = threading.Lock()


def set_session(new, reason, at=None, meta=None):
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
    add_event("state", at, state=new, label=head.replace("*", "").split(" (")[0], reason=reason)
    lines = [head, f"_{reason}_"]
    title = (meta or stream_meta).get("title") or stream_title
    if title:
        lines.append(f"*회의*  {title}")
    if new == "정회" and current_call and current_call[0]:
        lines.append(f"*정회 직전 질의 순서*  {current_call[0]} 위원")
    url, pos = moment_link(at, meta)
    if pos:
        lines.append(f"<{url}|▶ 해당 지점 보기 ({pos})>")
    if LOG_LINK:
        lines.append(f"<{LOG_LINK}|📄 전체 발언 기록 보기>")
    lines.append(tail)
    send_slack("\n".join(lines))


def is_current(cgen):
    """이 구간이 지금 감시 대상에서 온 것인가(갈아탄 뒤 늦게 인식된 이전 방송 구간이 아닌가)."""
    with _target_lock:
        return target["gen"] == cgen


def watch_session(text, start, cgen, meta=None):
    global adjourned_gen, expected_resume
    if not is_current(cgen):
        return                         # 이전 방송의 정회·산회가 새 방송 상태를 바꾸면 안 된다
    recess = RECESS_RE.search(text)
    if recess or (RESUME_RE.search(text) and FUTURE_TIME_RE.search(text)):
        at = parse_resume_time(text, start)    # "9시에 감사를 계속하겠습니다"
        if at and not (expected_resume and expected_resume["gen"] == cgen
                       and expected_resume["at"] == at):
            expected_resume = {"gen": cgen, "at": at, "alerted": False}
            log(f"⏰ 재개 예정 {time.strftime('%H:%M', time.localtime(at))}")
            record(f"   (재개 예정 {time.strftime('%H:%M', time.localtime(at))})", start)
    if recess:
        set_session("정회", f"위원장 선포: “{recess.group(0)}”", start, meta)
    elif RESUME_RE.search(text) and not FUTURE_TIME_RE.search(text):
        set_session("진행", f"위원장 선포: “{RESUME_RE.search(text).group(0)}”", start, meta)
    if ADJOURN_RE.search(text) and not recess and "정회" not in text and adjourned_gen != cgen:
        adjourned_gen = cgen
        log("🏁 산회(감사 종료) 선포 감지 — 방송이 끝나면 감시를 마칩니다.")
        record("\n===== 🏁 산회(감사 종료) 선포 =====")
        send_slack("🏁 산회(감사 종료) 선포 감지 — 방송 송출이 끝나면 감시를 마칩니다.")


def load_speaker_state():
    """직전 실행에서 알아낸 질의 위원을 이어받는다(정회 중 재실행 대비)."""
    global current_call
    try:
        with open(state_file(), encoding="utf-8") as f:
            st = json.load(f)
        if time.time() - st["t"] < CALL_STALE_SEC:
            current_call = (st["name"], st["t"], "")
            log(f"👤 직전 실행의 질의 순서를 이어받음: {st['name'] or '이름 미확인'} 위원 "
                f"({hhmmss(st['t'])})")
    except (OSError, ValueError, KeyError):
        pass


def update_speaker(text, start, cgen, meta=None):
    global current_call
    if not is_current(cgen):
        return
    refresh_watch()
    for i, rx in enumerate(CALL_RES):
        for m in rx.finditer(text):
            name = raw = m.group(1)
            if name in NOT_NAMES:
                continue
            fixed = snap_name(name, meta)
            if fixed:
                name = fixed               # 명단으로 바로잡음(예: 목용 → 모경종)
            elif (name in BAD_NAME_WORDS or name.endswith(BAD_NAME_ENDS)
                  or (committee_pool(meta)[1] and name not in roster)):
                # 이름이 아닌 말이거나, 위원회 명단이 있는데 그 어디와도 맞지 않는 이름('전기요금')
                if i not in CHAIR_CALLS:   # 위원장 호명일 때만 순서가 바뀐 걸로 본다
                    continue
                name = ""                  # 이름 미확인
            if current_call and current_call[0] == name and (name or start - current_call[1] < 20):
                if name:                   # 같은 위원을 다시 들었다 — 마지막 확인 시각만 갱신
                    current_call = (name, start, text)
                continue                   # 같은 위원, 또는 겹친 구간에서 같은 호명을 다시 들음
            if (current_call and name and current_call[0] and start - current_call[1] < 30
                    and _edit_distance(to_jamo(name)[0], to_jamo(current_call[0])[0], 2) <= 2):
                continue                   # 방금 호명된 이름을 겹친 구간에서 비슷하게 잘못 들음
                                           # ("장종태 의원입니다" → 다음 구간 "장경태 의원입니다")
            current_call = (name, start, text)
            label = f"{name} 위원" if name else "다음 위원(이름을 알아듣지 못함)"
            heard = f"(인식: {raw})" if raw != name else ""
            log(f"👤 질의 순서 바뀜: {label}{heard}")
            record(f"\n----- 👤 {label}{heard} 질의 순서 ({hhmmss(start)}) -----", start)
            url, pos = moment_link(start, meta)
            add_event("call", start, name=name, raw=raw, url=url, pos=pos)
            if name and name in watch_members:
                add_event("watch", start, name=name, url=url, pos=pos)
                title = (meta or stream_meta).get("title") or stream_title
                send_slack("\n".join(filter(None, [
                    f"🎯 *관심 의원 질의 시작: {name} 위원* ({hhmmss(start)}경){heard}",
                    f"*회의*  {title}" if title else "",
                    f"<{url}|▶ 해당 지점 보기 ({pos})>" if pos else f"<{url}|▶ 방송 열기>",
                    f"<{LOG_LINK}|📄 전체 발언 기록 보기>" if LOG_LINK else ""])))
            try:
                with open(state_file(), "w", encoding="utf-8") as f:
                    json.dump({"name": name, "t": start}, f, ensure_ascii=False)
            except OSError:
                pass
            if session_state == "정회":
                # 속개 선포를 못 알아들었어도 질의가 다시 시작됐으면 회의는 진행 중이다.
                set_session("진행", f"{label} 질의 호명 감지", start)


def speaker_line(text, at):
    """감지 문장의 발언자를 한 줄로. 질의 위원 이름 + 질의/답변 구분."""
    if current_call:
        name, t, _ = current_call
        who = f"{name} 위원" if name else "질의 위원(위원장 호명을 알아듣지 못함)"
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


def moment_link(at, meta=None):
    """발언 시점으로 가는 유튜브 링크. 시작 시각을 모르면 그 방송 주소(실시간)."""
    m = meta or stream_meta
    vid, t0 = m.get("id"), m.get("start")
    if not (vid and t0):
        return (m.get("url") or YOUTUBE_URL), None
    offset = max(0, int(at - t0 - LINK_LEAD_SEC))
    h, rem = divmod(offset, 3600)
    return f"https://www.youtube.com/watch?v={vid}&t={offset}s", f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def format_slack(alert, context):
    body = highlight(merge_chunks([t for _, t in context]), alert["terms"])
    span = f"{hhmmss(context[0][0])}~{hhmmss(context[-1][0] + CHUNK_SEC)}" if context else ""
    lines = [f"🚨 *키워드 감지: {', '.join(alert['keywords'])}*"]
    if alert.get("title"):
        lines.append(f"*회의*  {alert['title']}")
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


def rule_passes(keyword, variant, ctx):
    """문맥 조건 검사. ctx 는 앞뒤 발언을 이은 문단."""
    rule = RULES.get(keyword)
    if not rule:
        return True
    flat = ctx.replace(" ", "")
    low = flat.lower()
    if rule.get("with") and not any(w.replace(" ", "").lower() in low for w in rule["with"]):
        return False
    if rule.get("without"):
        terms = [keyword] + ALIASES.get(keyword, []) + ([variant] if variant else [])
        for term in terms:
            t = term.replace(" ", "").lower()
            for m in re.finditer(re.escape(t), low):
                near = low[max(0, m.start() - 15):m.end() + 15]
                if any(w.replace(" ", "").lower() in near for w in rule["without"]):
                    return False
    return True


def dispatch(alert):
    """앞뒤 발언을 붙여 보낸다. 전송이 느려도 인식이 멈추지 않게 별도 스레드에서 돈다."""
    context = [(t, s) for t, s in list(transcript)
               if alert["start"] - CONTEXT_BEFORE_SEC <= t]
    ctx = merge_chunks([s for _, s in context]) or alert["text"]
    kept = [(k, v) for k, v in alert["hits"] if rule_passes(k, v, ctx)]
    dropped = [k for k, _ in alert["hits"] if k not in [x[0] for x in kept]]
    if not kept:
        stats["suppressed"] += 1
        log(f"(문맥 조건에 맞지 않아 알림 생략: {', '.join(dropped)})")
        record(f"   ↑ (문맥 조건에 맞지 않아 알림 생략: {', '.join(dropped)})", alert["start"])
        add_event("suppressed", alert["start"], keywords=dropped)
        return
    if dropped:
        alert["keywords"] = [x for x in alert["keywords"] if x.split("(")[0] not in dropped]
    stats["alerts"] += 1
    url, pos = alert["link"]
    add_event("alert", alert["start"], keywords=list(alert["keywords"]), speaker=alert["speaker"],
              url=url, pos=pos, excerpt=ctx[:160])

    def run():
        send_slack(format_slack(alert, context))
        send_telegram(format_telegram(alert, context))

    threading.Thread(target=run, daemon=True).start()


# -------------------- 감지 --------------------
KEYWORDS_FILE = os.path.join(HERE, "live_keywords.txt")
_kw_mtime = None
ALIASES = {"피엠": ["PM", "P.M"]}  # 키워드 -> 음성 인식이 대신 적는 표현들
RULES = {}                         # 키워드 -> {"with": [...], "without": [...]} 문맥 조건
# 뜻이 여럿인 키워드는 기본 문맥 조건을 건다(키워드 파일에 '! 항상'을 쓰면 끈다).
# 2026-10-07 국토위에서 'PM' 4건은 모두 KDI 예타 '박상준 PM'(사업 책임자)이었다.
DEFAULT_RULES = {
    "피엠": {"with": ["킥보드", "이동장치", "이동수단", "모빌리티", "주차", "견인", "헬멧", "안전모",
                     "면허", "대여", "공유", "도로교통법", "보도", "인도", "전동", "사고", "업체"],
             "without": ["박사", "용역", "예타", "예비타당성"]},
    "자전거": {"with": ["공유", "대여", "전기", "개인형", "이동장치", "이동수단", "킥보드", "도로교통법",
                      "따릉이", "모빌리티", "지바이크", "더스윙", "PM", "피엠"],
              "without": []},
}
DEFAULT_KEYWORDS_FILE = """\
# 감시할 키워드 — 한 줄에 하나. 저장하면 실행 중에도 바로 반영됩니다.
#
# 음성 인식이 늘 틀리게 적는 표현이 있으면 '=' 뒤에 쉼표로 적어 두세요.
#   예) 킥보드 = 퀵보드, 킥보더
# 적지 않아도 발음이 한 글자 정도 어긋난 건(킥버드, 킥보도 등) 알아서 잡습니다.
# 두 글자 이하 키워드는 오탐을 막으려고 정확히 일치할 때만 잡습니다.
#
# 뜻이 여럿인 말은 문맥 조건을 붙일 수 있습니다(앞뒤 약 1분 발언 기준).
#   ! 함께: …  → 이 말들 중 하나가 같이 나올 때만 알림
#   ! 제외: …  → 키워드 바로 옆(앞뒤 15자)에 이 말이 있으면 알림 안 함
#   ! 항상     → 기본 문맥 조건을 끄고 늘 알림
#   예) 자전거 ! 함께: 공유, 대여, 도로교통법
# '피엠'과 '자전거'에는 따로 적지 않아도 기본 문맥 조건이 걸려 있습니다.

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
    global KEYWORDS, ALIASES, RULES, _kw_mtime
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
        words, aliases, rules = [], {}, {}
        for ln in text.splitlines():
            ln = ln.split("#", 1)[0].strip()
            if not ln:
                continue
            main, *opts = ln.split("!")
            head, _, rest = main.partition("=")
            head = head.strip()
            if not head:
                continue
            words.append(head)
            aliases[head] = [a.strip() for a in rest.split(",") if a.strip()]
            rule = dict(DEFAULT_RULES.get(head.replace(" ", ""), {}))
            for o in opts:
                key, _, vals = o.partition(":")
                key = key.strip()
                vals = [v.strip() for v in vals.split(",") if v.strip()]
                if key == "항상":
                    rule = {}
                elif key == "함께":
                    rule["with"] = vals
                elif key == "제외":
                    rule["without"] = vals
            if rule.get("with") or rule.get("without"):
                rules[head] = rule
        if not words:
            log("⚠️ live_keywords.txt 가 비어 있어 이전 키워드를 그대로 씁니다")
        else:
            if _kw_mtime is not None:
                log(f"🔄 키워드 갱신: {', '.join(words)}")
            KEYWORDS, ALIASES, RULES = words, aliases, rules
        _kw_mtime = mtime
    except OSError as e:
        log(f"⚠️ 키워드 파일을 읽지 못함: {e}")


def check_keywords(text, start, alternatives=(), meta=None):
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
        alert["hits"] += [h for h in fresh if h[0] not in [x[0] for x in alert["hits"]]]
        for lab in labels:
            if lab.split("(")[0] not in [x.split("(")[0] for x in alert["keywords"]]:
                alert["keywords"].append(lab)
        log(f"🚨 키워드 추가 감지: {', '.join(labels)} — 직전 알림에 합칩니다")
        return
    print("\n" + "=" * 50, flush=True)
    log(f"🚨 키워드 감지: {', '.join(labels)} — 뒤 발언을 {FOLLOW_CHUNKS}구간 더 듣고 보냅니다")
    log(f"🗣️ {text}")
    print("=" * 50 + "\n", flush=True)
    pending.append({"keywords": labels, "text": text, "start": start, "hits": list(fresh),
                    # 감지 문장과 바로 앞 문장으로 질의·답변을 가린다.
                    "terms": terms,
                    "speaker": speaker_line(" ".join(t for _, t in list(transcript)[-2:]) or text, start),
                    "link": moment_link(start, meta),
                    "title": (meta or stream_meta).get("title") or stream_title,
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


final_done = threading.Event()     # 'final' 제어 신호를 인식 스레드가 처리했다


def reset_broadcast_state():
    """다른 방송으로 갈아탈 때 이전 방송의 질의 위원·대화록·회의 기록을 비운다(인식 스레드에서 호출)."""
    global current_call
    transcript.clear()
    current_call = None
    reset_session_record()
    try:
        os.remove(state_file())
    except OSError:
        pass


def stt_worker(q):
    r = sr.Recognizer()
    # 인식 요청이 응답 없이 멈추면 스레드가 영영 서 버린다(기본값은 시간 제한 없음).
    r.operation_timeout = 30
    while True:
        item = q.get()
        if item is None:               # 스트림 끊김 신호
            flush_pending()
            continue
        if isinstance(item, dict):     # 제어 신호 — 음성 구간보다 늦게 처리돼야 순서가 맞는다
            flush_pending()
            if "reset" in item:        # 감시 대상이 바뀌었다 — 이전 방송 요약을 보내고 흔적을 지운다
                send_summary(item.get("reason", "감시 대상 변경"), item.get("meta"))
                reset_broadcast_state()
            if item.get("final"):      # 끝내기 직전 — 기다리던 알림을 모두 내보냈다고 알린다
                final_done.set()
            continue
        pcm, start, cgen, cmeta = item   # 어느 감시 대상·방송의 구간인지 함께 다닌다
        stats["chunks"] += 1
        try:
            # show_all: 1순위 문장 말고 다른 인식 후보도 받아 키워드를 찾는다.
            res = r.recognize_google(sr.AudioData(pcm, RATE, WIDTH), language="ko-KR",
                                     show_all=True)
            alts = [a["transcript"] for a in (res or {}).get("alternative", [])
                    if isinstance(a, dict) and a.get("transcript")] if isinstance(res, dict) else []
            if not alts:
                raise sr.UnknownValueError()
            text, others = alts[0], alts[1:]
            stats["texts"] += 1
            stats["last_text"] = time.time()
            stats["consec_err"] = 0
            log(text)
            record_utterance(text, start, cmeta)
            transcript.append((start, text))
            while transcript and transcript[0][0] < start - 300:
                transcript.popleft()
            update_speaker(text, start, cgen, cmeta)
            watch_session(text, start, cgen, cmeta)
            check_keywords(text, start, others, cmeta)
        except sr.UnknownValueError:
            stats["consec_err"] = 0    # 무음이거나 알아듣지 못함 — 서비스는 응답했다
        except sr.RequestError as e:
            stats["stt_errors"] += 1
            stats["consec_err"] += 1
            log(f"STT 오류: {e}")
        except Exception as e:         # 스레드가 죽으면 조용히 인식이 멈추므로 다 잡는다
            stats["stt_errors"] += 1
            stats["consec_err"] += 1
            log(f"인식 중 오류: {e}")
        advance_pending()


# -------------------- 감시 대상(방송 하나 안에서) --------------------
# 감시 프로세스 하나는 방송 하나를 듣는다. 그 안에서도 대상이 바뀔 수 있다 — 2부 자동 추적,
# 같은 주소에서 새 영상이 열림. 바뀔 때마다 세대 번호(gen)를 올려, 늦게 인식된 이전 방송
# 구간이 새 방송 상태를 건드리지 못하게 한다.
_target_lock = threading.Lock()
target = {"url": YOUTUBE_URL, "gen": 0, "src": "기본값", "keep": False}
target_changed = threading.Event()
stop_event = threading.Event()     # 끝내라는 신호(관리 프로세스의 stop, Ctrl+C)
current_proc = None                # 지금 듣고 있는 ffmpeg — 대상이 바뀌면 끊는다
YOUTUBE_RE = re.compile(r"^https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+$", re.IGNORECASE)
# 채널 단위 주소: 그 채널의 '아무' 생중계로 붙어서, 상임위가 여럿이면 엉뚱한 방송을 듣게 된다.
CHANNEL_RE = re.compile(r"youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)"
                        r"(?:/[^?#]*)?(?:[?#].*)?$", re.IGNORECASE)
STOP_WORDS = {"stop", "중지", "멈춤", "정지"}


def set_target(url, src, keep=False):
    """감시 대상을 바꾸고, 듣고 있던 ffmpeg 를 끊어 메인 루프가 곧바로 갈아타게 한다.
    keep=True 는 같은 회의의 다음 방송(2부)이라 질의 위원·회의 기록을 이어 간다는 뜻."""
    with _target_lock:
        target["url"], target["src"], target["keep"] = url, src, keep
        target["gen"] += 1
        proc = current_proc
    target_changed.set()
    if proc is not None and proc.poll() is None:
        proc.kill()


def request_stop():
    stop_event.set()
    with _target_lock:
        proc = current_proc
    target_changed.set()
    if proc is not None and proc.poll() is None:
        proc.kill()


def wait_for_change(sec):
    """sec 초 기다리되 감시 대상이 바뀌거나 끝내라는 신호가 오면 바로 돌아온다. 1초씩 끊어
    기다려서 윈도우에서도 Ctrl+C 가 바로 먹는다(시간 제한을 건 Event.wait 는 Ctrl+C 로 깨지 않는
    버전이 있다)."""
    end = time.monotonic() + sec
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return False
        if target_changed.wait(min(1.0, left)) or stop_event.is_set():
            return True


# -------------------- 원격 지정(관리 프로세스) --------------------
def parse_targets(text):
    """원격 지정 파일 → (stop, [(라벨, 주소)...], [(이유, 줄)...]).
    한 줄에 방송 하나: 주소만 쓰거나, 주소 앞뒤에 이름을 붙인다(예: '국토위 https://...').
    'stop' 줄이 있으면 전부 멈춘다."""
    stop, items, bad = False, [], []
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("﻿").strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.lower() in STOP_WORDS:
            stop = True
            continue
        m = re.search(r"https?://\S+", ln)
        if not m:
            bad.append(("invalid", ln))
            continue
        url = m.group(0)
        label = (ln[:m.start()] + " " + ln[m.end():]).strip(" \t-:|·")
        if CHANNEL_RE.search(url):
            bad.append(("channel", ln))
        elif not YOUTUBE_RE.match(url):
            bad.append(("invalid", ln))
        else:
            items.append((label, url))
    return stop, items, bad


def parse_target(text):
    """(예전 형식 호환) 첫 대상 하나만: 'stop' / 주소 / None / ('invalid'|'channel', 줄)."""
    stop, items, bad = parse_targets(text)
    if stop:
        return "stop"
    if items:
        return items[0][1]
    return bad[0] if bad else None


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "git/2.40 live-keyword-watch",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def remote_check(last_sha, path=None, branch=None):
    """('same'|'ok'|'nofile'|'nobranch', 커밋 번호, 본문). 네트워크 문제는 예외로 올린다."""
    path, branch = path or REMOTE_PATH, branch or REMOTE_BRANCH
    refs = _http_get(f"https://github.com/{REMOTE_REPO}.git/info/refs?service=git-upload-pack")
    m = re.search(r"([0-9a-f]{40}) refs/heads/" + re.escape(branch) + r"\n",
                  refs.decode("utf-8", "replace"))
    if not m:
        return "nobranch", None, None
    sha = m.group(1)
    if sha == last_sha:
        return "same", sha, None
    try:
        body = _http_get(f"https://raw.githubusercontent.com/{REMOTE_REPO}/{sha}/{path}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "nofile", sha, None
        raise
    return "ok", sha, body.decode("utf-8-sig")


def remote_poller(last_sha, last_text, apply_first, on_change):
    """원격 지정 파일을 주기적으로 확인한다. 내용이 바뀌면 on_change(본문).
    apply_first: 아직 내용을 한 번도 못 읽었을 때, 처음 읽은 내용을 적용할지(아니면 기준으로만)."""
    fail_since, missing_logged = None, False
    while True:
        time.sleep(REMOTE_POLL_SEC)
        try:
            kind, sha, text = remote_check(last_sha)
        except Exception as e:             # 네트워크 끊김·시간 초과 — 조용히 다시 시도
            if fail_since is None:
                fail_since = time.time()
                log(f"⚠️ 원격 지정 확인 실패: {short_error(e)} — 계속 다시 시도합니다.")
            continue
        if fail_since is not None:
            log("원격 지정 확인이 다시 됩니다.")
            fail_since = None
        if kind == "same":
            continue
        if kind in ("nofile", "nobranch"):
            # 커밋 번호를 기억하지 않는다 — 올린 직후 raw 가 잠깐 404 를 낼 수 있어서 다음에 다시 읽는다.
            if not missing_logged:
                log(f"⚠️ 원격 지정 파일을 찾을 수 없습니다({REMOTE_BRANCH}:{REMOTE_PATH}) — "
                    f"계속 확인합니다.")
                missing_logged = True
            continue
        last_sha = sha
        missing_logged = False
        if last_text is None and not apply_first:
            last_text = text               # 시작 때 못 읽었으면 첫 내용은 기준으로만 삼는다
        elif text != last_text:
            last_text = text
            try:
                on_change(text)
            except Exception as e:
                log(f"⚠️ 원격 지정 적용 중 오류: {short_error(e)}")
        apply_first = False


# -------------------- 스트림 --------------------
class NotLiveNow(Exception):
    """방송이 지금 송출 중이 아니다(정회로 끊겼거나, 아직 시작 전이거나, 끝났다)."""

    def __init__(self, msg, meta=None, ended=False):
        super().__init__(msg)
        self.meta = meta or {}
        self.ended = ended


def _meta_from(info, url=None):
    return {"title": info.get("title") or "", "id": info.get("id") or "",
            # 실제 송출 시작 시각(liveBroadcastDetails.startTimestamp). '방송 열기' 링크를
            # 발언 시점으로 보내는 데 쓴다.
            "start": info.get("release_timestamp"),
            "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
            "url": url or info.get("webpage_url") or ""}


def get_live_stream(url):
    """유튜브 생중계의 오디오 주소(HLS)와 방송 정보를 꺼낸다."""
    # noplaylist: 재생목록 안에서 복사한 링크(&list=...)도 그 영상 하나만 연다.
    opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    status = info.get("live_status")
    meta = _meta_from(info, url)
    if status in ("was_live", "post_live"):
        # 끝난 생중계를 그대로 열면 다시보기를 처음부터 재생해 오전 발언을 또 알린다.
        raise NotLiveNow("방송이 끝난 상태(정회로 송출이 멈췄을 수 있음)", meta, ended=True)
    if status == "is_upcoming":
        raise NotLiveNow("방송 시작 전", meta)
    if status != "is_live":
        log("⚠️ 생중계가 아닌 일반 영상입니다 — 처음부터 재생하며 듣습니다(시험용).")
    return info["url"], meta


# ---- 2부 자동 추적 ----
# 국회방송은 한 회의를 1부·2부처럼 따로 송출한다(2026-10-07 국토위: 18:53 송출 종료,
# 21시 재개). 방송이 끝나면 같은 채널의 생중계 목록에서 위원회·날짜가 같은 방송을 찾는다.
def session_key(title):
    """'[국회방송 생중계] 2부 - 2026년 국정감사 국토위 - 국토교통부 등 (26.10.7.)' → ('국토위', (26,10,7))."""
    t = re.sub(r"\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*$", "", title or "")   # yt-dlp 생중계 제목 꼬리
    date = re.search(r"\((\d{2})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?\)", t)
    comm = next((a for a in sorted(COMMITTEE_ABBR, key=len, reverse=True) if a in t), None)
    if not comm:
        m = re.search(r"([가-힣]{2,}위원회)", t)
        comm = m.group(1) if m else None
    if not (date and comm):
        return None
    return comm, tuple(int(x) for x in date.groups())


def find_followup(ref):
    """끝난 방송(ref)과 같은 회의의 다른 생중계를 찾는다. (주소, 제목) 또는 None."""
    key, channel = session_key(ref.get("title")), ref.get("channel_url")
    if not (key and channel):
        return None
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True, "playlistend": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel.rstrip("/") + "/streams", download=False)
    for e in info.get("entries") or []:
        vid, title = e.get("id"), e.get("title") or ""
        if not vid or vid == ref.get("id") or session_key(title) != key:
            continue
        if e.get("live_status") not in (None, "is_live"):
            continue
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            get_live_stream(url)
        except Exception:
            continue
        return url, title
    return None


# ---- 예고된 재개 시각 ----
_KOR_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8,
            "아홉": 9, "열": 10, "열한": 11, "열두": 12}
RESUME_AT_RE = re.compile(r"(오전|오후)?\s?(\d{1,2}|열한|열두|다섯|여섯|일곱|여덟|아홉|한|두|세|네|열)"
                          r"\s?시\s?(?:(\d{1,2})\s?분|(반))?\s?(?:부터|에)")
expected_resume = None             # {"gen", "at", "alerted"} — "9시에 감사를 계속하겠습니다"


def parse_resume_time(text, now):
    """'9시에 감사를 계속' → 앞으로 올 가장 이른 그 시각(오전·오후는 문맥으로 고른다)."""
    m = RESUME_AT_RE.search(text)
    if not m:
        return None
    ampm, h, mi, half = m.groups()
    h = int(h) if h.isdigit() else _KOR_NUM[h]
    mi = int(mi) if mi else (30 if half else 0)
    if ampm == "오전":
        hours = [h]
    elif ampm == "오후":
        hours = [h + 12 if h < 12 else h]
    else:
        hours = [h, h + 12] if h < 12 else [h]
    base = time.localtime(now)
    cands = []
    for hh in hours:
        if 0 <= hh < 24 and 0 <= mi < 60:
            ts = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, hh, mi, 0, 0, 0, -1))
            if ts >= now - 300:
                cands.append(ts)
    return min(cands) if cands else None


# -------------------- 회의 요약(마감 보고서) --------------------
session_t0 = time.time()
_stats_t0 = dict(stats)


def reset_session_record():
    global session_t0, _stats_t0
    with _ev_lock:
        events.clear()
    session_t0, _stats_t0 = time.time(), dict(stats)


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def summary_text(reason, meta=None):
    with _ev_lock:
        evs = list(events)
    m = meta or stream_meta
    title = m.get("title") or stream_title or m.get("url") or "(제목 모름)"
    calls = [e for e in evs if e["kind"] == "call"]
    alerts = [e for e in evs if e["kind"] == "alert"]
    supp = [e for e in evs if e["kind"] == "suppressed"]
    states = [e for e in evs if e["kind"] == "state"]
    follows = [e for e in evs if e["kind"] == "follow"]
    texts = stats["texts"] - _stats_t0.get("texts", 0)
    lines = [f"📋 *감시 요약* — {title}",
             f"_{reason}_ · {_hm(session_t0)}~{_hm(time.time())} · 인식 {texts}구간 · "
             f"키워드 알림 {len(alerts)}건" + (f"(문맥 조건으로 생략 {len(supp)}건)" if supp else "")]
    if watch_members:
        seen = {e["name"]: e for e in evs if e["kind"] == "watch"}
        lines.append("*관심 의원*  " + ", ".join(
            f"{n} {_hm(seen[n]['t'])} 질의" if n in seen else f"{n} 질의 없음"
            for n in sorted(watch_members)))
    if calls:
        lines.append(f"*질의 순서* ({len(calls)}명)")
        for e in calls[:40]:
            who = f"{e['name']} 위원" if e["name"] else "이름 미확인"
            heard = f" (인식: {e['raw']})" if e['raw'] != e['name'] else ""
            link = f" <{e['url']}|▶ {e['pos']}>" if e.get("pos") else ""
            lines.append(f"• {_hm(e['t'])} {who}{heard}{link}")
        if len(calls) > 40:
            lines.append(f"• … 외 {len(calls) - 40}명")
    if alerts:
        lines.append(f"*키워드 알림* ({len(alerts)}건)")
        for e in alerts[:30]:
            link = f" <{e['url']}|▶ {e['pos']}>" if e.get("pos") else ""
            lines.append(f"• {_hm(e['t'])} {', '.join(e['keywords'])} — {e['speaker']}{link}")
            lines.append(f"   > {e['excerpt']}")
    else:
        lines.append("*키워드 알림*  없음")
    if states or follows:
        lines.append("*정회·속개*")
        for e in sorted(states + follows, key=lambda x: x["t"]):
            if e["kind"] == "follow":
                lines.append(f"• {_hm(e['t'])} 다음 방송으로 이어 듣기 — {e['title']}")
            else:
                lines.append(f"• {_hm(e['t'])} {e['label']} — {e['reason']}")
    if LOG_LINK:
        lines.append(f"<{LOG_LINK}|📄 전체 발언 기록 보기>")
    return "\n".join(lines), bool(evs or texts)


def send_summary(reason, meta=None):
    text, has_any = summary_text(reason, meta)
    if not has_any:
        return
    path = os.path.join(LOG_DIR, time.strftime("live_summary_%Y-%m-%d") + label_suffix() + ".md")
    md = re.sub(r"<([^|>]+)\|([^>]+)>", r"[\2](\1)", text).replace("*", "**")
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(md + "\n\n---\n\n")
        except OSError as e:
            log(f"⚠️ 요약 파일 쓰기 실패: {e}")
    log(f"📋 감시 요약을 보냅니다({reason}) — {path}")
    send_slack(text)


# -------------------- 장애 감시·상태 보고 --------------------
idle = threading.Event()           # 이 방송 감시가 끝났거나 멈춘 상태(상태 보고를 쉰다)


def health_watchdog():
    """조용한 고장을 알린다: 회의 중 오래 인식이 없음, 음성 인식 연속 오류, 예고된 재개 시각
    경과. 그리고 HEARTBEAT_MIN 마다 상태 보고."""
    silent_alerted = err_alerted = False
    last_hb, hb_stats = time.time(), dict(stats)
    while True:
        time.sleep(30)
        try:
            now = time.time()
            with _target_lock:
                listening = current_proc is not None and current_proc.poll() is None
                gen = target["gen"]
            if listening and session_state != "정회" and not idle.is_set():
                ref = max(stats["last_text"] or 0, stats["listen_since"] or now)
                if now - ref >= SILENT_ALERT_SEC and not silent_alerted:
                    silent_alerted = True
                    send_slack(f"⚠️ *{int((now - ref) // 60)}분째 인식된 발언이 없습니다* — 방송 소리는 "
                               f"받고 있습니다(음성 인식 오류 {stats['stt_errors']}회). 정회 화면이면 "
                               f"무시하셔도 됩니다.")
            if silent_alerted and stats["last_text"] and now - stats["last_text"] < 60:
                silent_alerted = False
                send_slack("✅ 다시 발언이 인식됩니다.")
            if stats["consec_err"] >= STT_ERROR_ALERT and not err_alerted:
                err_alerted = True
                send_slack(f"⚠️ *음성 인식 오류가 {stats['consec_err']}회 연달아 났습니다* — 인터넷 연결을 "
                           f"확인해 주세요. 계속되면 구글 음성 인식이 막혔을 수 있습니다.")
            if err_alerted and stats["consec_err"] == 0:
                err_alerted = False
                send_slack("✅ 음성 인식이 다시 됩니다.")
            er = expected_resume
            if (er and er["gen"] == gen and not er["alerted"] and not listening
                    and now > er["at"] + RESUME_GRACE_SEC):
                er["alerted"] = True
                send_slack(f"⚠️ *예고된 재개 시각({_hm(er['at'])})이 {RESUME_GRACE_SEC // 60}분 지났는데 "
                           f"방송을 다시 열지 못했습니다.* 같은 회의의 다음 방송도 찾지 못했습니다. "
                           f"새 주소로 열렸다면 링크를 원격 지정해 주세요.")
            if HEARTBEAT_MIN and now - last_hb >= HEARTBEAT_MIN * 60:
                if not idle.is_set():
                    send_heartbeat(hb_stats, last_hb, listening)
                last_hb, hb_stats = now, dict(stats)
        except Exception as e:
            log(f"⚠️ 장애 감시 중 오류: {short_error(e)}")


def send_heartbeat(prev, since, listening):
    with _ev_lock:
        recent = [e for e in events if e["t"] >= since]
    calls = [e["name"] or "이름 미확인" for e in recent if e["kind"] == "call"]
    n_alert = sum(1 for e in recent if e["kind"] == "alert")
    state = "정회 중" if session_state == "정회" else ("듣는 중" if listening else "방송 대기 중")
    who = f" · 지금 {current_call[0]} 위원 질의" if current_call and current_call[0] else ""
    send_slack(f"🫀 상태 보고 {_hm(since)}~{_hm(time.time())} — {state}{who}\n"
               f"인식 {stats['texts'] - prev['texts']}구간 · 오류 {stats['stt_errors'] - prev['stt_errors']}회"
               f" · 키워드 알림 {n_alert}건"
               + (f"\n질의 순서: {' → '.join(calls)}" if calls else ""))


# -------------------- 공통 도우미 --------------------
def keep_awake(on=True):
    """윈도우가 절전으로 들어가 녹음이 멈추는 걸 막는다(정회 중 자리를 비워도)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if on:
            log("절전 방지: 켜짐(이 창이 떠 있는 동안 PC가 잠들지 않습니다)")
    except Exception as e:
        log(f"⚠️ 절전 방지 설정 실패: {e} — 전원 설정에서 절전을 꺼 주세요")


def short_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
    return msg[:200]


def put_control(q, item):
    """인식 큐에 제어 신호(None=알림 내보내기, dict=방송 바뀜·끝내기)를 넣는다. 인식이 막혀 큐가
    꽉 차 있어도 메인 루프가 멈추지 않게, 가장 오래된 음성 구간을 버리고 자리를 만든다."""
    for _ in range(30):
        try:
            q.put(item, timeout=5)
            return
        except queue.Full:
            # 제어 신호는 순서가 중요해서 건드리지 않고, 가장 오래된 음성 구간만 버린다.
            with q.mutex:
                for i, old in enumerate(q.queue):
                    if isinstance(old, tuple):
                        del q.queue[i]
                        q.not_full.notify()
                        break
            log("⚠️ 인식이 밀려 오래된 구간 하나를 버림")
    log("⚠️ 인식 스레드가 응답하지 않습니다 — 제어 신호를 건너뜁니다.")


stt_queue = queue.Queue(maxsize=20)   # 음성 구간·제어 신호 → 인식 스레드


def finalize(q, reason, wait=60):
    """끝내기 전에 기다리던 알림을 내보내고 요약을 보낸다."""
    final_done.clear()
    put_control(q, {"final": True})
    final_done.wait(wait)
    send_summary(reason)
    reset_session_record()


# -------------------- 방송 하나 감시(감시 프로세스) --------------------
def run_watch(url, src):
    """방송 하나를 듣는다. stop_event 가 켜지면 요약을 보내고 돌아온다."""
    global stream_title, stream_meta, current_proc, session_state, expected_resume
    target.update(url=url, src=src)
    q = stt_queue
    greeted = False                    # 지금 대상으로 한 번이라도 들었나
    ever_greeted = False               # 프로그램을 켠 뒤 한 번이라도 들었나
    continuing = False                 # 2부로 이어 듣는 중(인사 대신 '이어 듣기' 알림을 이미 보냄)
    waiting_since = None
    fail_notified = False
    active_gen = None
    done_gen = None                    # 이 대상은 끝났다(산회 후 방송 종료)
    active_vid = None                  # 지금 대상에서 듣고 있는 영상 id
    last_follow = 0.0

    while not stop_event.is_set():    # 바깥 루프 = 끊겼을 때 재접속, 대상이 바뀌면 갈아타기
        target_changed.clear()
        with _target_lock:
            url, gen, src, keep = target["url"], target["gen"], target["src"], target["keep"]
        if gen != active_gen:
            if active_gen is not None:
                if keep:
                    # 같은 회의의 다음 방송 — 질의 위원·회의 기록·정회 상태를 그대로 이어 간다.
                    continuing = True
                    greeted, waiting_since, fail_notified, active_vid = False, None, False, None
                    log(f"🔁 같은 회의의 다음 방송으로 이어 듣습니다 → {url}")
                else:
                    # 다른 방송: 기다리던 알림은 이전 방송 기준으로 내보내고, 요약을 보낸 뒤 비운다.
                    put_control(q, {"reset": True, "reason": "다른 방송으로 바뀜",
                                    "meta": dict(stream_meta)})
                    with _state_lock:
                        session_state = "unknown"
                    continuing = False
                    greeted, waiting_since, fail_notified, active_vid = False, None, False, None
                    log(f"🔄 감시 대상 변경 → {url} ({src})")
                record(f"\n##### 감시 대상 변경 {time.strftime('%Y-%m-%d %H:%M:%S')} → {url} ({src})")
            active_gen = gen
        if done_gen == gen:
            idle.set()
            wait_for_change(60)
            continue

        try:
            stream_url, meta = get_live_stream(url)
        except NotLiveNow as e:
            if adjourned_gen == gen:
                # 산회가 선포됐고 방송도 끝났다 — 이 방송은 여기까지.
                log("🏁 산회 선포 후 방송 종료 확인 — 이 방송 감시를 마칩니다.")
                record(f"##### 감시 종료 {time.strftime('%Y-%m-%d %H:%M:%S')}")
                send_slack("🏁 산회 선포 후 방송 종료 — 이 방송 감시를 마칩니다.")
                finalize(q, "산회 후 방송 종료")
                done_gen = gen
                return "done"
            if waiting_since is None:
                waiting_since = time.time()
                if greeted:
                    record(f"\n===== ⏸ 방송 송출 멈춤 ({hhmmss(waiting_since)}) =====")
                log(f"⏸ {e} — 1분마다 다시 확인합니다.")
                if not greeted and not fail_notified and not continuing:
                    # 켜자마자(또는 갈아타자마자) 대상이 송출 중이 아니면 슬랙이 조용해서
                    # 켜졌는지조차 알 수 없다 — 한 번은 알린다.
                    send_slack(f"⏳ *감시 대기* (v{VERSION}) — 지정된 방송이 지금 송출 중이 아닙니다({e}).\n"
                               f"`{url}`\n방송이 시작되면 자동으로 듣고, 새 링크를 원격 지정하면 "
                               f"바로 갈아탑니다.")
                    fail_notified = True
            elif (greeted and time.time() - waiting_since >= PAUSE_NOTICE_SEC
                  and session_state != "정회"):
                set_session("정회", f"방송 송출이 {int((time.time() - waiting_since) // 60)}분째 "
                                   f"멈춤(선포는 못 들었음)", waiting_since)
            elif int(time.time() - waiting_since) % 600 < 60:
                mins = int((time.time() - waiting_since) // 60)
                log(f"⏸ 재개 대기 중({mins}분째). 새 주소로 열리면 링크를 원격 지정해 주세요.")
            # 방송이 끝났으면 같은 회의의 다음 방송(2부)을 찾아 이어 듣는다.
            if e.ended and time.time() - last_follow >= FOLLOW_SEARCH_SEC:
                last_follow = time.time()
                ref = dict(e.meta)
                if stream_meta.get("id") == ref.get("id"):
                    ref = dict(stream_meta, **{k: v for k, v in ref.items() if v})
                try:
                    found = find_followup(ref)
                except Exception as fe:
                    found = None
                    log(f"다음 방송 찾기 실패: {short_error(fe)}")
                if found:
                    new_url, new_title = found
                    log(f"🔁 같은 회의의 다음 방송을 찾았습니다: {new_title}")
                    add_event("follow", time.time(), title=new_title, url=new_url)
                    send_slack(f"🔁 *같은 회의의 다음 방송으로 이어 듣습니다* — {new_title}\n{new_url}")
                    set_target(new_url, "2부 자동 추적", keep=True)
                    continue
            wait_for_change(60)
            continue
        except Exception as e:
            err = short_error(e)
            log(f"스트림 주소 추출 실패: {err} — 30초 후 재시도")
            if not fail_notified:
                send_slack(f"⚠️ 방송을 아직 열 수 없습니다 — 30초마다 다시 시도합니다.\n"
                           f"`{url}`\n_{err}_")
                fail_notified = True
            wait_for_change(30)
            continue

        with _target_lock:
            if target["gen"] != gen:       # 주소를 꺼내는 사이 대상이 또 바뀌었다
                continue
        if active_vid and meta["id"] and meta["id"] != active_vid:
            # 같은 주소인데 다른 영상이 열렸다 — 다른 방송으로 갈아탄 것과 똑같이 다룬다
            # (새 세대 번호를 받아야 이전 영상의 산회·정회가 따라오지 않는다).
            log(f"🔄 같은 주소에서 다른 방송이 열림({active_vid} → {meta['id']})")
            set_target(url, "같은 주소의 새 방송")
            continue
        idle.clear()
        active_vid = meta["id"] or active_vid
        stream_meta, stream_title = dict(meta, url=url), meta["title"]
        fail_notified = False
        expected_resume = None
        if waiting_since is not None:
            if greeted:
                log("▶ 방송 재개 — 다시 듣습니다.")
                record(f"\n===== ▶ 방송 재개 ({hhmmss(time.time())}) =====")
                if session_state == "정회":
                    set_session("진행", "방송 송출 재개")
            waiting_since = None
        if not greeted:
            record(f"\n\n##### 감시 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} — "
                   f"{stream_title or url}\n##### {url}")
            log(f"📝 발언 기록: {os.path.join(LOG_DIR, time.strftime('live_log_%Y-%m-%d') + label_suffix() + '.txt')}")
            if continuing:
                if session_state == "정회":
                    set_session("진행", "같은 회의의 다음 방송에서 재개")
            else:
                # 연결 확인용. 이게 안 오면 키워드를 기다릴 필요 없이 알림 설정부터 봐야 한다.
                if ever_greeted:
                    hello = f"🔄 *감시 대상 변경* — {stream_title or url}\n{url}"
                else:
                    hello = f"✅ 생중계 키워드 감시 시작 (v{VERSION}) — {stream_title or url}"
                if LOG_LINK:
                    hello += f"\n<{LOG_LINK}|📄 전체 발언 기록 보기>"
                send_slack(hello)
                send_telegram(hello)
            greeted = ever_greeted = True
            continuing = False
        if not stream_meta["start"]:
            log("⚠️ 방송 시작 시각을 몰라 '방송 열기'가 실시간 화면으로 연결됩니다.")

        proc = subprocess.Popen(
            # -rw_timeout: 정회 화면에서 송출이 멎어 응답이 끊기면 30초 뒤 빠져나와 재접속한다.
            ["ffmpeg", "-loglevel", "error", "-rw_timeout", "30000000", "-i", stream_url,
             "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-"],
            stdout=subprocess.PIPE)
        with _target_lock:
            current_proc = proc
            stale = target["gen"] != gen or stop_event.is_set()
        if stale:
            proc.kill()
        stats["listen_since"] = time.time()
        buf = b""
        try:
            while True:
                data = proc.stdout.read(BPS)          # 1초씩
                if not data:
                    break                             # 방송 종료·정회·주소 만료·대상 변경
                buf += data
                if len(buf) >= CHUNK_SEC * BPS:
                    start = time.time() - CHUNK_SEC   # 이 구간이 시작된 시각(수신 기준)
                    try:
                        q.put_nowait((buf, start, gen, stream_meta))
                    except queue.Full:
                        log("⚠️ 인식이 밀려 구간 하나를 건너뜀")
                    buf = buf[-OVERLAP_SEC * BPS:]    # 마지막 3초는 다음 구간에도
        finally:
            with _target_lock:
                current_proc = None
            stats["listen_since"] = None
            proc.kill()
            proc.wait()
        put_control(q, None)           # 기다리던 감지 건을 내보내라는 신호
        with _target_lock:
            switched = target["gen"] != gen
        if not switched and not stop_event.is_set():
            log("스트림 끊김 — 10초 후 재접속")
            wait_for_change(10)
    finalize(q, "감시 종료")
    return "stopped"


def _stdin_listener():
    """관리 프로세스가 보내는 'stop' 을 받는다(입력이 닫혀도 끝낸다)."""
    try:
        for line in sys.stdin:
            if line.strip() == "stop":
                break
    except Exception:
        pass
    request_stop()


def child_main(label, url):
    """감시 프로세스: 방송 하나를 맡는다. 관리 프로세스가 띄우고 끈다."""
    global SLOT_LABEL, REMOTE_ON
    SLOT_LABEL, REMOTE_ON = label, False
    threading.Thread(target=_stdin_listener, daemon=True).start()
    refresh_keywords()
    refresh_watch()
    load_roster()
    load_speaker_state()
    threading.Thread(target=stt_worker, args=(stt_queue,), daemon=True).start()
    threading.Thread(target=health_watchdog, daemon=True).start()
    log(f"📡 감시 시작 v{VERSION}: {url}")
    result = None
    while not stop_event.is_set():
        try:
            result = run_watch(url, "지정")
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:       # 예상 못 한 오류 — 30초 뒤 같은 방송으로 다시 시작한다
            log("⚠️ 감시 중 오류:\n" + traceback.format_exc())
            send_slack(f"⚠️ 감시 중 오류로 30초 뒤 다시 시작합니다 — {short_error(e)}")
            url = target["url"]
            for _ in range(30):
                if stop_event.is_set():
                    break
                time.sleep(1)
    sys.exit(0 if result in ("done", "stopped") else 1)


# -------------------- 관리 프로세스(원격 지정·여러 방송 동시 감시) --------------------
SCRIPT_PATH = os.path.abspath(__file__)


class Supervisor:
    """원격 지정 파일의 줄마다 감시 프로세스를 하나씩 띄우고, 바뀌면 끄고 켠다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.children = {}         # 키 → {"label","url","proc","stopping","finished","restart_at"}
        self.desired = {}

    @staticmethod
    def wanted(stop, items):
        if stop:
            return {}
        if len(items) == 1 and not items[0][0]:
            return {"": ("", items[0][1])}            # 하나만 듣고 이름이 없으면 예전과 같은 파일 이름
        out = {}
        for i, (label, url) in enumerate(items, 1):
            lab = label or f"방송{i}"
            while lab in out:
                lab += "'"
            out[lab] = (lab, url)
        return out

    def child_cmd(self, label, url):
        return [sys.executable, "-u", SCRIPT_PATH, "--child", label, url]

    def start_child(self, key, label, url):
        proc = subprocess.Popen(self.child_cmd(label, url), stdin=subprocess.PIPE,
                                cwd=HERE, text=True, encoding="utf-8")
        self.children[key] = {"label": label, "url": url, "proc": proc, "stopping": False,
                              "finished": False, "restart_at": None}
        log(f"▶ 감시 시작: {label or '방송'} — {url}")

    def stop_child(self, key, wait=25):
        c = self.children.pop(key, None)
        if not c:
            return
        c["stopping"] = True
        p = c["proc"]
        if p.poll() is None:
            try:
                p.stdin.write("stop\n")
                p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(wait)
            except subprocess.TimeoutExpired:
                p.kill()
        log(f"⏹ 감시 끔: {c['label'] or '방송'} — {c['url']}")

    def apply(self, desired):
        with self.lock:
            self.desired = desired
            for key in list(self.children):
                c = self.children[key]
                # 주소가 바뀌었거나, 산회로 끝난 방송을 다시 지정했으면 새로 켠다.
                if key not in desired or c["url"] != desired[key][1] or c["finished"]:
                    self.stop_child(key)
            for key, (label, url) in desired.items():
                if key not in self.children:
                    self.start_child(key, label, url)

    def on_remote(self, text):
        stop, items, bad = parse_targets(text)
        for why, ln in bad:
            reason = ("채널 주소라서(그 채널의 다른 생중계에 붙을 수 있음) — 특정 방송 링크를 주세요"
                      if why == "channel" else "유튜브 주소가 아니라서")
            log(f"⚠️ 원격 지정 무시: {ln[:80]} — {reason}")
            send_slack(f"⚠️ 원격 지정의 한 줄을 무시했습니다 — {reason}.\n`{ln[:80]}`")
        if not stop and not items:
            if bad:
                return                 # 쓸 수 있는 줄이 하나도 없으면 지금 상태를 그대로 둔다
            log("원격 지정 파일에 주소가 없습니다 — 지금 대상을 그대로 둡니다.")
            return
        if stop:
            log("⏹ 원격 지시로 모든 감시를 멈춥니다 — 새 주소를 기다립니다.")
            send_slack("⏹ *감시 중지* (원격 지시) — 새 링크를 주시면 다시 시작합니다.")
        else:
            log(f"📥 원격 지정 바뀜: {', '.join((lab + ' ' if lab else '') + u for lab, u in items)}")
        self.apply(self.wanted(stop, items))

    def watch_children(self):
        """감시 프로세스가 예기치 않게 꺼지면 15초 뒤 다시 켠다. 산회로 끝났으면 두고 본다."""
        with self.lock:
            for key, c in list(self.children.items()):
                code = c["proc"].poll()
                if code is None or c["stopping"]:
                    continue
                if code == 0:
                    if not c["finished"]:
                        c["finished"] = True
                        log(f"🏁 {c['label'] or '방송'} 감시 끝 — 새 주소가 오면 다시 시작합니다.")
                    continue
                if c["restart_at"] is None:
                    c["restart_at"] = time.time() + 15
                    send_slack(f"⚠️ {('`' + c['label'] + '` ') if c['label'] else ''}감시 프로그램이 "
                               f"예기치 않게 꺼져 15초 뒤 다시 켭니다(종료 코드 {code}).")
                elif time.time() >= c["restart_at"]:
                    self.children.pop(key)
                    self.start_child(key, c["label"], c["url"])

    def stop_all(self):
        with self.lock:
            for key in list(self.children):
                self.stop_child(key, wait=40)


def initial_targets(cli_url):
    """시작할 때 원격 지정을 읽어 처음 들을 방송을 정한다. (원하는 대상, sha, 본문, apply_first)."""
    text, sha = None, None
    apply_first = not cli_url          # 시작 때 못 읽었으면, 나중에 처음 읽은 내용을 적용할지
    if REMOTE_ON:
        try:
            kind, sha, text = remote_check(None)
            if kind != "ok":
                apply_first, text = True, None     # 파일이 아직 없다 — 생기면 그 내용을 따른다
                log("원격 지정 파일 없음 — 생기면 그때 따릅니다.")
        except Exception as e:         # 일시적 실패 — 명령줄 주소가 있으면 그게 우선이다
            log(f"⚠️ 원격 지정을 읽지 못함: {short_error(e)} — 계속 다시 시도합니다.")
    if cli_url:
        if CHANNEL_RE.search(cli_url):
            log("⚠️ 채널 주소입니다 — 그 채널의 다른 생중계에 붙을 수 있으니 특정 방송 링크를 권합니다.")
        if text is not None:
            log("ℹ️ 명령줄 주소로 시작하고, 원격 지정이 바뀌면 그쪽을 따릅니다. "
                "원격 주소로 시작하려면 주소 없이 실행하세요.")
        return {"": ("", cli_url)}, sha, text, apply_first and text is None
    if text is not None:
        stop, items, bad = parse_targets(text)
        for why, ln in bad:
            log(f"⚠️ 원격 지정의 이 줄은 쓸 수 없습니다: {ln[:80]}")
        if stop or items:
            return Supervisor.wanted(stop, items), sha, text, False
    return {"": ("", YOUTUBE_URL)}, sha, text, apply_first and text is None


def update_checker():
    """저장소에 새 버전이 올라오면 한 번 알린다(직접 바꾸지는 않는다)."""
    notified = None
    while True:
        try:
            kind, _, body = remote_check(None, path="automation/live_keyword_watch.py")
            m = re.search(r'^VERSION = "([^"]+)"', body or "", re.M) if kind == "ok" else None
            if m and m.group(1) > VERSION and m.group(1) != notified:
                notified = m.group(1)
                log(f"🆕 새 버전 {notified} 이 있습니다 — 'python {os.path.basename(SCRIPT_PATH)} --update'")
                send_slack(f"🆕 감시 프로그램 새 버전({notified})이 있습니다. PC에서 Ctrl+C 후 "
                           f"`python {os.path.basename(SCRIPT_PATH)} --update` 를 실행하고 다시 켜 주세요.")
        except Exception:
            pass
        time.sleep(UPDATE_CHECK_SEC)


def supervisor_main(cli_url):
    channels = [n for n, ok in (("슬랙", SLACK_WEBHOOK),
                                ("텔레그램", TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)) if ok]
    log(f"생중계 키워드 감시 v{VERSION}")
    log(f"알림: {', '.join(channels) if channels else '없음 — 콘솔에만 출력'}")
    if SLACK_WEBHOOK:
        log(f"슬랙 웹훅: {SLACK_WEBHOOK_SRC}에서 읽음 (…{SLACK_WEBHOOK[-6:]})")
    if SLACK_WEBHOOK and not SLACK_WEBHOOK.startswith("https://hooks.slack.com/"):
        log(f"⚠️ 슬랙 웹훅 주소 형식이 이상합니다: {SLACK_WEBHOOK[:40]}...")
    refresh_keywords()
    refresh_watch()
    log(f"키워드: {', '.join(KEYWORDS)}  (live_keywords.txt — 실행 중 고쳐도 반영)")
    log(f"관심 의원: {', '.join(sorted(watch_members)) or '없음'}  (live_watch_members.txt)")
    log(f"📝 발언 기록·요약: {LOG_DIR}")
    log(f"기록 공유 링크: {'있음 — 알림에 붙습니다' if LOG_LINK else '없음(live_log_link.txt)'}")
    desired, sha, text, apply_first = initial_targets(cli_url)
    sup = Supervisor()
    if REMOTE_ON:
        threading.Thread(target=remote_poller, args=(sha, text, apply_first, sup.on_remote),
                         daemon=True).start()
        log(f"원격 지정: 켜짐 — {REMOTE_REPO} [{REMOTE_BRANCH}] {REMOTE_PATH} 를 "
            f"{REMOTE_POLL_SEC}초마다 확인")
    else:
        log("원격 지정: 꺼짐(live_remote.txt)")
    threading.Thread(target=update_checker, daemon=True).start()
    log("끝내려면 Ctrl+C. 정회로 방송이 멈춰도 꺼지지 않고 재개를 기다립니다.")
    keep_awake()
    sup.apply(desired)
    try:
        while True:
            time.sleep(5)
            sup.watch_children()
            if not REMOTE_ON and sup.children and all(
                    c["finished"] for c in sup.children.values()):
                log("🏁 모든 감시가 끝났습니다.")
                break
    except KeyboardInterrupt:
        log("종료 중 — 요약을 보내고 끕니다(최대 40초)…")
        sup.stop_all()
    finally:
        keep_awake(False)


# -------------------- 업데이트·자동 시작 --------------------
def self_update():
    """저장소의 최신 파일로 이 파일을 바꾼다(이전 파일은 .bak 으로 남긴다)."""
    kind, sha, body = remote_check(None, path="automation/live_keyword_watch.py")
    if kind != "ok":
        print(f"업데이트 실패: 저장소에서 파일을 찾지 못함({kind})")
        return 1
    try:
        compile(body, SCRIPT_PATH, "exec")
    except SyntaxError as e:
        print(f"업데이트 중단: 받은 파일에 문법 오류({e})")
        return 1
    m = re.search(r'^VERSION = "([^"]+)"', body, re.M)
    new_ver = m.group(1) if m else "?"
    if body.encode("utf-8") == open(SCRIPT_PATH, "rb").read():
        print(f"이미 최신입니다(v{VERSION}).")
        return 0
    backup = SCRIPT_PATH + ".bak"
    with open(backup, "wb") as f:
        f.write(open(SCRIPT_PATH, "rb").read())
    tmp = SCRIPT_PATH + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, SCRIPT_PATH)
    print(f"업데이트 완료: v{VERSION} → v{new_ver} (이전 파일: {os.path.basename(backup)})")
    print(f"다시 켜세요: python {os.path.basename(SCRIPT_PATH)}")
    return 0


def startup_bat_path():
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        "live_keyword_watch.bat")


def install_startup(on=True):
    """윈도우에 로그인하면 감시 프로그램이 자동으로 켜지게(또는 끄게) 한다."""
    if os.name != "nt":
        print("윈도우에서만 쓸 수 있습니다.")
        return 1
    path = startup_bat_path()
    if not on:
        try:
            os.remove(path)
            print(f"자동 시작을 껐습니다({path} 삭제).")
        except OSError:
            print("자동 시작이 설정돼 있지 않습니다.")
        return 0
    with open(path, "w", encoding="cp949", errors="replace") as f:
        f.write("@echo off\r\n"
                f'cd /d "{HERE}"\r\n'
                f'start "생중계 키워드 감시" cmd /k ""{sys.executable}" "{SCRIPT_PATH}""\r\n')
    print(f"자동 시작을 켰습니다 — 윈도우에 로그인하면 새 창에서 감시가 시작됩니다.\n  {path}")
    print(f"끄려면: python {os.path.basename(SCRIPT_PATH)} --uninstall-startup")
    return 0


if __name__ == "__main__":
    args = [a.strip() for a in sys.argv[1:]]
    if args[:1] == ["--child"] and len(args) >= 3:
        try:
            child_main(args[1], args[2])
        except KeyboardInterrupt:
            # Ctrl+C 는 같은 창의 관리 프로세스와 감시 프로세스에 함께 간다 — 기다리던 알림과
            # 요약을 보내고 끈다(관리 프로세스는 최대 40초 기다린다).
            log("Ctrl+C — 기다리던 알림과 요약을 보내고 끕니다.")
            request_stop()
            finalize(stt_queue, "Ctrl+C 로 종료", wait=20)
            sys.exit(0)
    elif args[:1] == ["--update"]:
        sys.exit(self_update())
    elif args[:1] == ["--install-startup"]:
        sys.exit(install_startup(True))
    elif args[:1] == ["--uninstall-startup"]:
        sys.exit(install_startup(False))
    else:
        supervisor_main(args[0] if args else None)
