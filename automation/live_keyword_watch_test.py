# -*- coding: utf-8 -*-
"""live_keyword_watch.py 시험 — 유튜브·슬랙·구글 음성 인식 없이 돈다(ffmpeg 는 필요).

    python automation/live_keyword_watch_test.py

가짜 방송(사인파 wav 를 ffmpeg -re 로 실시간 재생)과 가짜 음성 인식(정해 둔 문장을 차례로
돌려줌)으로 감시 프로그램 전체 흐름을 돌린다. 슬랙으로 갈 메시지를 모아 확인한다.
2026-10-07 국토위 실제 발언 표현(감사중지 선포, '치료해' 오인식, 박상준 PM 등)을 쓴다.
"""
import json, os, subprocess, sys, tempfile, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="lkw_test_")
os.environ["LIVE_LOG_DIR"] = os.path.join(TMP, "logs")
import live_keyword_watch as m  # noqa: E402

m.HERE = TMP                                      # 키워드·관심 의원·상태 파일을 임시 폴더에
m.KEYWORDS_FILE = os.path.join(TMP, "live_keywords.txt")
m.WATCH_FILE = os.path.join(TMP, "live_watch_members.txt")
m.ROSTER_CACHE = os.path.join(TMP, "roster.json")
m.EXTRA_MEMBERS_FILE = os.path.join(TMP, "none.txt")
SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "member_snapshot.json")
m._http_get = lambda url, headers=None: open(SNAPSHOT, "rb").read()

SENT = []
m.send_slack = lambda msg: SENT.append(msg)
FAILS = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def wav(path, sec):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=f=440:d={sec}",
                    "-ar", "16000", path], check=True)
    return path


TITLE1 = "[국회방송 생중계] 2026년 국정감사 국토위 - 국토교통부 등 (26.10.7.) 2026-10-07 14:02"
TITLE2 = "[국회방송 생중계] 2부 - 2026년 국정감사 국토위 - 국토교통부 등 (26.10.7.)"


def test_units():
    print("[단위]")
    stop, items, bad = m.parse_targets("국토위 https://www.youtube.com/watch?v=A\nhttps://youtu.be/B 행안위\n"
                                       "https://www.youtube.com/@NATV/live\n# 메모")
    check("여러 줄 원격 지정", items == [("국토위", "https://www.youtube.com/watch?v=A"),
                                     ("행안위", "https://youtu.be/B")] and bad[0][0] == "channel")
    check("하나·이름 없음은 예전 파일 이름", m.Supervisor.wanted(False, [("", "u")]) == {"u": ("", "u", "")})
    vid = "Jgedf-zK1eA"
    check("영상 id 추출", {m.video_key(u) for u in [
        f"https://www.youtube.com/watch?v={vid}", f"https://youtu.be/{vid}?si=x",
        f"https://www.youtube.com/live/{vid}", f"https://m.youtube.com/watch?feature=share&v={vid}&t=30"]} == {vid})
    A, B = "https://youtu.be/" + "A" * 11, "https://youtu.be/" + "B" * 11
    w = m.Supervisor.wanted(False, [("국토 위!", A), ("", A + "?t=3"), ("", B), ("국토위", "https://youtu.be/" + "C" * 11)])
    check("같은 방송 중복 제거·이름 정리·겹치는 이름", w == {
        "A" * 11: ("국토위", A, "국토위"), "B" * 11: ("방송2", B, ""),
        "C" * 11: ("국토위2", "https://youtu.be/" + "C" * 11, "국토위")}, str(w))
    m.SLOT_LABEL = "국토/위 2부"
    check("기록 파일 이름 정리", m.label_suffix() == "_국토위2부")
    m.SLOT_LABEL = ""
    bad = os.path.join(TMP, "bad.txt")
    with open(bad, "wb") as f:
        f.write("킥보드".encode("utf-8")[:-1] + b"\xff\xfe")
    try:
        m._read_text(bad)
        check("깨진 글자 파일도 읽음", True)
    except Exception as e:
        check("깨진 글자 파일도 읽음", False, repr(e))
    with open(m.state_file(), "w", encoding="utf-8") as f:
        json.dump({"name": "염태영", "t": time.time(), "vid": "X" * 11}, f)
    m.current_call = None
    m.load_speaker_state("https://youtu.be/" + "Y" * 11)
    check("다른 방송의 질의 위원은 이어받지 않음", m.current_call is None)
    m.load_speaker_state("https://youtu.be/" + "X" * 11)
    check("같은 방송이면 이어받음", (m.current_call or ("",))[0] == "염태영")
    m.current_call = None
    os.remove(m.state_file())
    check("stop 은 전부 멈춤", m.Supervisor.wanted(*m.parse_targets("u https://youtu.be/x\nstop")[:2]) == {})
    check("1부·2부 같은 회의", m.session_key(TITLE1) == m.session_key(TITLE2) == ("국토위", (26, 10, 7)))
    check("다른 날은 다른 회의", m.session_key(TITLE1) != m.session_key(TITLE1.replace("26.10.7.", "26.10.8.")))

    def at(h, mi):
        return time.mktime((2026, 10, 7, h, mi, 0, 0, 0, -1))
    r = m.parse_resume_time("9시에 감사를 계속하겠습니다 감사 중지를 선포합니다", at(18, 48))
    check("재개 예고 9시 → 21:00", r == at(21, 0))
    r = m.parse_resume_time("그러면 오후 5시 20분에 감사를 계속하도록", at(16, 49))
    check("오후 5시 20분 → 17:20", r == at(17, 20))
    m.load_roster()
    m.refresh_watch()
    meta = {"title": TITLE1}
    fixes = {n: m.snap_name(n, meta) for n in ["윤종호", "허용", "김해", "목용", "복귀한", "전기요금", "장종태"]}
    check("명단 이름 보정", fixes == {"윤종호": "윤종오", "허용": "허영", "김해": "김미애", "목용": "모경종",
                                   "복귀한": "복기왕", "전기요금": None, "장종태": "장종태"}, str(fixes))
    check("명단이 빈약한 위원회는 보정 안 함", m.snap_name("김해", {"title": "국정감사 행안위"}) is None)
    m.refresh_keywords()
    check("PM 예타 책임자는 제외", not m.rule_passes("피엠", "PM", "박상준 pm 하고 서울대 장순 박사라는 분이 용역을"))
    check("PM 주차·견인은 알림", m.rule_passes("피엠", "PM", "PM 주차 문제 견인"))


def run_child(script, follow=None, seconds=60, resumed=False):
    """감시 프로세스(run_watch)를 스레드로 돌린다. script: [(대상 키, 파일, 제목, 끝나면 NotLiveNow)]."""
    m.stop_event.clear()
    m.target.update(url="https://www.youtube.com/watch?v=A", gen=0, keep=False)
    streams = {"A": {"file": wav(os.path.join(TMP, "a.wav"), 46), "title": TITLE1, "ended": False},
               "B": {"file": wav(os.path.join(TMP, "b.wav"), 40), "title": TITLE2, "ended": False}}

    def fake_stream(url):
        key = url.rsplit("=", 1)[-1]
        s = streams[key]
        meta = {"title": s["title"], "id": key, "start": time.time() - 3600,
                "channel_url": "https://www.youtube.com/channel/UCtest", "url": url}
        if s["ended"]:
            raise m.NotLiveNow("방송이 끝난 상태", meta, ended=True)
        s["ended"] = True                   # 한 번 듣고 나면 끝난 방송
        return s["file"], meta
    m.get_live_stream = fake_stream
    m.find_followup = follow or (lambda ref: None)
    real_popen = subprocess.Popen
    m.subprocess.Popen = lambda cmd, **kw: real_popen(cmd[:cmd.index("-i")] + ["-re"] + cmd[cmd.index("-i"):], **kw)
    lines = iter(script)
    m.sr.Recognizer.recognize_google = lambda self, a, language, show_all=False: {
        "alternative": [{"transcript": next(lines, "음")}]}
    m.FOLLOW_SEARCH_SEC = 0
    real_wait = m.wait_for_change
    m.wait_for_change = lambda sec: real_wait(min(sec, 2))
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", m.run_watch(m.target["url"], "지정", resumed)),
                         daemon=True)
    t.start()
    t.join(seconds)
    m.request_stop()
    t.join(30)
    m.wait_for_change = real_wait
    m.subprocess.Popen = real_popen
    return out.get("r")


def test_flow():
    print("[흐름: 키워드·문맥 조건·관심 의원·2부 이어 듣기·요약]")
    threading.Thread(target=m.stt_worker, args=(m.stt_queue,), daemon=True).start()
    SENT.clear()
    script = [
        "다음은 목용 위원님 치료해 주시기 바랍니다",                 # 호명(오인식) → 모경종
        "박상준 pm 하고 서울대 박사라는 분이 용역을 했더라고요",       # PM 예타 → 문맥 조건으로 생략
        "다음은 염태영 위원님 질의해 주시기 바랍니다 장관님",          # 관심 의원
        "공유 킥보드 무단 방치 문제 대책이 있으십니까",                 # 키워드
        "9시에 감사를 계속하겠습니다 감사 중지를 선포합니다",            # 정회 + 재개 예고
        "의석을 정돈해 주시기 바랍니다 국정감사를 계속하도록 하겠습니다",  # 2부에서 속개
        "킥보드 주차 견인 문제",
    ]
    calls = {"n": 0}

    def follow(ref):
        calls["n"] += 1
        return ("https://www.youtube.com/watch?v=B", TITLE2) if ref.get("id") == "A" else None
    run_child(script, follow=follow, seconds=95)
    heads = [s.splitlines()[0] for s in SENT]
    joined = "\n".join(SENT)
    check("시작 알림(버전 포함)", any(h.startswith("✅") and f"v{m.VERSION}" in h for h in heads), str(heads))
    check("관심 의원 알림", any("관심 의원 질의 시작: 염태영" in h for h in heads), str(heads))
    check("킥보드 알림", any(h.startswith("🚨") and "킥보드" in h for h in heads), str(heads))
    check("PM 예타는 알림 안 함", not any(h.startswith("🚨") and "피엠" in h for h in heads), str(heads))
    check("정회 알림", any("정회" in h for h in heads), str(heads))
    check("2부 이어 듣기 알림", any("다음 방송으로 이어 듣습니다" in h for h in heads), str(heads))
    check("2부에서 '감시 시작' 인사를 다시 보내지 않음",
          sum(1 for h in heads if h.startswith("✅")) == 1 and not any(h.startswith("🔄") for h in heads), str(heads))
    check("속개 알림", any("속개" in h or "회의 진행 중" in h for h in heads[heads.index(next(
        h for h in heads if "이어 듣습니다" in h)):]), str(heads))
    summ = [s for s in SENT if s.startswith("📋")]
    check("요약 보고서", bool(summ), str(heads))
    if summ:
        s = summ[-1]
        check("요약: 이름 보정 표시", "모경종 위원 (인식: 목용)" in s, s)
        check("요약: 관심 의원 질의", "염태영" in s and "질의 없음" not in s, s)
        check("요약: 문맥 생략 집계", "생략 1건" in s, s)
        check("요약: 2부 이어 듣기 기록", "다음 방송으로 이어 듣기" in s, s)
    files = os.listdir(os.environ["LIVE_LOG_DIR"])
    check("요약 파일", any(f.startswith("live_summary_") for f in files), str(files))
    check("발언 기록 파일", any(f.startswith("live_log_") for f in files), str(files))
    print("   슬랙:", " | ".join(h[:40] for h in heads))
    del joined


def test_health():
    print("[장애 감시]")
    SENT.clear()
    m.SILENT_ALERT_SEC = 1
    m.STT_ERROR_ALERT = 2
    real_sleep = time.sleep
    ticks = {"n": 0}

    def fake_sleep(sec):
        ticks["n"] += 1
        if ticks["n"] > 3:
            raise SystemExit
        real_sleep(0.05)
    m.time.sleep = fake_sleep

    class P:
        def poll(self):
            return None
    m.current_proc = P()
    with m._state_lock:
        m.session_state = "진행"
    m.idle.clear()
    m.stats.update(last_text=time.time() - 100, listen_since=time.time() - 100, consec_err=3,
                   last_bytes=time.time())
    m.expected_resume = {"gen": m.target["gen"], "at": time.time() - 3600, "alerted": False}
    try:
        m.health_watchdog()
    except SystemExit:
        pass
    m.time.sleep = real_sleep
    m.current_proc = None
    heads = [s.splitlines()[0] for s in SENT]
    check("무인식 경고", any("인식된 발언이 없습니다" in h for h in heads), str(heads))
    check("음성 인식 연속 오류 경고", any("음성 인식 오류가" in h for h in heads), str(heads))
    check("재개 예고 경과 경고는 듣는 중이면 안 보냄", not any("예고된 재개" in h for h in heads), str(heads))

    killed = []

    class Stuck:
        def poll(self):
            return None

        def kill(self):
            killed.append(1)
    m.current_proc = Stuck()
    m.stats["last_bytes"] = time.time() - m.STALL_KILL_SEC - 5
    ticks["n"] = 2
    m.time.sleep = fake_sleep
    try:
        m.health_watchdog()
    except SystemExit:
        pass
    m.time.sleep = real_sleep
    m.current_proc = None
    check("소리가 끊긴 채 멈추면 다시 접속", killed == [1])


def test_robust():
    print("[인식 스레드: 분석 단계 오류에도 계속]")
    seen = []
    real_ws, real_ck = m.watch_session, m.check_keywords

    def boom(*a):
        raise RuntimeError("시험용 오류")
    m.watch_session = boom
    m.check_keywords = lambda text, *a: seen.append(text)
    m.sr.Recognizer.recognize_google = lambda self, a, language, show_all=False: {
        "alternative": [{"transcript": "공유 킥보드"}]}
    pcm = b"\0" * (m.BPS * 2)
    for _ in range(2):
        m.stt_queue.put((pcm, time.time(), m.target["gen"], {"title": "", "id": "", "url": ""}))
    m.final_done.clear()
    m.stt_queue.put({"final": True})
    ok = m.final_done.wait(20)
    m.watch_session, m.check_keywords = real_ws, real_ck
    check("한 단계가 실패해도 다음 단계·다음 구간을 처리", ok and seen == ["공유 킥보드"] * 2, str(seen))

    SENT.clear()
    with m._state_lock:
        m.session_state = "정회"
    run_child(["정회 중입니다"], seconds=8, resumed=True)
    heads = [x.splitlines()[0] for x in SENT]
    check("오류 뒤 다시 시작하면 시작 인사·속개 알림을 보내지 않음",
          not any(h.startswith(("✅", "🔄", "⏳", "▶")) for h in heads), str(heads))


def test_supervisor():
    print("[관리 프로세스: 여러 방송·교체·재시작]")
    dummy = os.path.join(TMP, "dummy_child.py")
    with open(dummy, "w", encoding="utf-8") as f:
        f.write("import sys, time\n"
                "label, url = sys.argv[-2], sys.argv[-1]\n"
                "if url.endswith('E' * 11): sys.exit(3)\n"
                "if url.endswith('D' * 11): sys.exit(0)\n"
                "for line in sys.stdin:\n"
                "    if line.strip() == 'stop': break\n"
                "sys.exit(0)\n")
    sup = m.Supervisor()
    sup.child_cmd = lambda label, url: [sys.executable, dummy, label, url]
    SENT.clear()
    A, B, C, D, E = ("https://www.youtube.com/watch?v=" + x * 11 for x in "ABCDE")
    labels = lambda: {k[0]: c["label"] for k, c in sup.children.items()}
    sup.on_remote(f"국토위 {A}\n행안위 {B}")
    check("두 방송 동시 시작", labels() == {"A": "국토위", "B": "행안위"}, str(labels()))
    pa = sup.children["A" * 11]["proc"]
    sup.on_remote(f"행안위 {C}\n국토위 {A}")
    check("줄 순서를 바꾸고 한 줄만 교체 → 그 방송만 교체", sup.children["A" * 11]["proc"] is pa
          and labels() == {"A": "국토위", "C": "행안위"}, str(labels()))
    sup.on_remote(f"{B}\n국토위 {A}\n행안위 {C}")
    check("이름 없는 줄을 위에 추가해도 듣던 방송은 그대로", sup.children["A" * 11]["proc"] is pa
          and labels() == {"A": "국토위", "B": "방송1", "C": "행안위"}, str(labels()))
    sup.on_remote(f"{C}\n{B}")
    check("자동 이름이 밀려도 끊지 않고, 새 방송과 파일 이름이 겹치지 않음",
          labels() == {"B": "방송1", "C": "행안위"}, str(labels()))
    sup.on_remote(f"{D}\n{B}")
    time.sleep(1.5)
    sup.watch_children()
    pd = sup.children["D" * 11]["proc"]
    check("산회로 끝난 방송 표시", sup.children["D" * 11]["finished"])
    sup.on_remote(f"{D}\n{B}\n국토위 {A}")
    check("다른 줄만 바뀌면 끝난 방송을 다시 켜지 않음", sup.children["D" * 11]["proc"] is pd)
    sup.on_remote(f"국토위2부 {D}\n{B}\n국토위 {A}")
    check("끝난 방송의 줄을 고치면 다시 켬", sup.children["D" * 11]["proc"] is not pd
          and sup.children["D" * 11]["label"] == "국토위2부")
    sup.on_remote(f"{E}")
    time.sleep(1.5)
    sup.watch_children()
    check("예기치 않은 종료 알림", any("예기치 않게 꺼져 15초" in s for s in SENT), str(SENT))
    sup.children["E" * 11]["restart_at"] = 0
    sup.watch_children()                      # 다시 켬
    time.sleep(1.5)
    sup.watch_children()                      # 또 바로 죽음 → 간격 두 배
    c = sup.children["E" * 11]
    check("켜자마자 또 죽으면 재시작 간격을 늘림", sup.crashes["E" * 11] == 2
          and 25 < c["restart_at"] - time.time() <= 30, str(sup.crashes))
    sup.on_remote("stop")
    check("stop → 전부 멈춤", not sup.children)
    sup.on_remote("https://www.youtube.com/@NATV/live")
    check("채널 주소 거부 알림", any("채널 주소" in s for s in SENT), str(SENT))
    sup.stop_all()
    check("한 번만 켜지게 잠금", m.single_instance() and m.single_instance() is False
          if os.name != "nt" else True)


def test_update():
    print("[업데이트]")
    fake = os.path.join(TMP, "live_keyword_watch.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write('VERSION = "old"\n')
    new_body = 'VERSION = "2099.01.01-1"\nprint("hi")\n'
    m.SCRIPT_PATH = fake
    m.remote_check = lambda last, path=None, branch=None: ("ok", "sha", new_body)
    check("업데이트 실행", m.self_update() == 0 and open(fake, encoding="utf-8").read() == new_body
          and os.path.exists(fake + ".bak"))
    m.remote_check = lambda last, path=None, branch=None: ("ok", "sha", "def broken(:\n")
    check("문법 오류 파일은 거부", m.self_update() == 1 and open(fake, encoding="utf-8").read() == new_body)


if __name__ == "__main__":
    test_units()
    test_flow()
    test_health()
    test_robust()
    test_supervisor()
    test_update()
    print("\n" + ("모두 통과" if not FAILS else f"실패 {len(FAILS)}건: {', '.join(FAILS)}"))
    sys.exit(1 if FAILS else 0)
