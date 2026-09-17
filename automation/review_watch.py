# -*- coding: utf-8 -*-
"""의안 검토보고서 감시.

검토보고서는 소관위 수석전문위원이 심사 전에 쓰는 문서다. 위원회·정부의 입장이
처음 문서로 드러나는 자리라 본회의 표결보다 훨씬 이른 신호다 — GR 관점에서는
'소관위 심사중'이라는 한 줄보다 이쪽이 훨씬 쓸모 있다.

그런데 국회 Open API 의안 쪽에는 이 문서가 없다. 진행단계(ALLBILL)에도, 의안
목록에도, 제안이유(BPMBILLSUMMARY)에도 없다. 그래서 의안정보시스템(likms)
상세 페이지에서 읽는다.

페이지 구조는 우리 것이 아니라 언제든 바뀐다. 그래서 두 가지를 지킨다.

  1. '문서가 없다'와 '못 읽었다'를 절대 섞지 않는다. 페이지에 '검토보고서'라는
     말은 있는데 링크를 못 뽑았으면 그건 0건이 아니라 파싱 실패다. 조용히 0건을
     보고하면 검토보고서가 안 나온 것과 구분이 안 되고, 그게 이 트래커가 도로교통법을
     한 달 넘게 놓친 것과 같은 종류의 실패다.
  2. 파싱은 순수 함수(parse_documents)로 떼어 둔다. 가짜 HTML 로 검증한다
     (automation/review_watch_test.py). 사이트가 바뀌어 깨지면 그 검증이 먼저 운다.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

LIKMS_BASE = "https://likms.assembly.go.kr"
BILL_DETAIL = LIKMS_BASE + "/bill/billDetail.do?billId=%s"
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 20
# 남의 사이트를 30여 번 연달아 두드리는 일이라 간격을 둔다.
DELAY_SEC = 0.4

# 찾는 문서. 앞의 둘이 본체고, 비용추계서는 재정 부담을 어떻게 봤는지가 나와서
# 규제 강도를 가늠할 때 같이 본다.
REPORT_TERMS = ["검토보고서", "검토보고", "심사보고서", "비용추계서", "비용추계"]

# 문서 링크로 볼 만한 것. filegate 가 국회 파일 서버고, 나머지는 페이지 구조가
# 바뀌었을 때를 대비한 그물이다.
LINK_HINTS = ["filegate", "fileGate", ".pdf", ".hwp", "openBillFile", "FileGate"]

# 속성값은 큰따옴표로 감싸고 그 안에 작은따옴표가 들어가는 일이 흔하다
# (onclick="openBillFile('/filegate/...')"). 그래서 따옴표 종류별로 따로 받는다 —
# [^"'] 하나로 묶으면 자바스크립트 호출이 통째로 안 잡힌다.
_LINK_RE = re.compile(r'''(?:href|onclick|onClick)\s*=\s*(?:"([^"]{4,400})"|'([^']{4,400})')''')
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# 링크를 term 주변 어디까지 찾아 볼지. 표 한 행이 보통 이 안에 들어간다.
WINDOW = 900


def text_of(html):
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def mentions(html):
    """페이지에 검토보고서류 말이 나오기는 하는가. 파싱 실패를 가려내는 데 쓴다."""
    return [t for t in REPORT_TERMS if t in html]


def _absolute(url):
    url = url.strip()
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return LIKMS_BASE + url
    # javascript:openBillFile('/filegate/servlet/FileGate?bookId=...','...') 같은 꼴에서
    # 경로만 건져 낸다.
    m = re.search(r"""['"](/[^'"]+)['"]""", url)
    if m:
        return LIKMS_BASE + m.group(1)
    return ""


def parse_documents(html):
    """(문서 목록, 파싱실패여부).

    문서 목록은 [{"title":…, "url":…, "term":…}]. 파싱실패는 '페이지에 말은 있는데
    링크를 못 뽑았다'는 뜻이다 — 그때는 빈 목록과 함께 True 가 온다.
    """
    said = mentions(html)
    if not said:
        return [], False                 # 말 자체가 없다 = 아직 문서가 없다

    links = [(mo.start(), mo.group(1) or mo.group(2) or "") for mo in _LINK_RE.finditer(html)]
    out, seen = [], set()
    for term in said:
        for mo in re.finditer(re.escape(term), html):
            at = mo.start()
            near = [(abs(pos - at), pos, raw) for pos, raw in links
                    if abs(pos - at) <= WINDOW and any(h in raw for h in LINK_HINTS)]
            if not near:
                continue
            near.sort()
            url = _absolute(near[0][2])
            if not url or url in seen:
                continue
            seen.add(url)
            # 링크 주변 글자를 제목으로 삼는다. 표 안이라 보통 문서 이름이 붙어 있다.
            around = text_of(html[max(0, at - 120):at + 160])
            out.append({"title": around[:120] or term, "url": url, "term": term})
    return out, (not out)


def fetch_bill_page(bill_id, opener=None):
    url = BILL_DETAIL % urllib.parse.quote(str(bill_id))
    req = urllib.request.Request(url, headers=UA)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def check_reports(snapshot, limit=None, fetch=None, log=print):
    """추적 의안의 검토보고서를 확인하고 새로 뜬 것을 changes 로 돌려준다.

    심사가 굴러가는 의안만 본다 — 폐기·철회된 건에는 새 문서가 붙지 않는다.
    """
    fetch = fetch or fetch_bill_page
    seen_all = snapshot.setdefault("seen_report_urls", {})
    changes, unread, checked = [], [], 0

    for no, info in sorted(snapshot.get("bills", {}).items()):
        stage = info.get("stage", "")
        if "폐기" in stage or "철회" in stage or "공포" in stage:
            continue
        bill_id = info.get("bill_id")
        if not bill_id:
            continue
        if limit is not None and checked >= limit:
            break
        checked += 1
        html = fetch(bill_id)
        time.sleep(DELAY_SEC)
        if html is None:
            unread.append(no)
            continue
        docs, parse_failed = parse_documents(html)
        if parse_failed:
            # 말은 있는데 링크를 못 뽑았다. 이건 '없음'이 아니다.
            log("!! [%s] 페이지에 %s 는 있는데 링크를 못 뽑았다 — 파싱이 깨졌을 수 있다"
                % (no, ", ".join(mentions(html))))
            unread.append(no)
            continue
        known = set(seen_all.get(no, []))
        fresh = [d for d in docs if d["url"] not in known]
        for d in fresh:
            changes.append({"type": "new_report", "bill_no": no, "name": info.get("name", ""),
                             "title": d["title"], "url": d["url"], "term": d["term"]})
        if docs:
            seen_all[no] = sorted(known | set(d["url"] for d in docs))
    return changes, checked, unread


def main(argv):
    """손으로 확인할 때: python automation/review_watch.py [의안 수]

    상태 파일은 읽기만 하고 쓰지 않는다. 슬랙도 보내지 않는다.
    """
    if len(argv) > 2 and argv[1] == "--dump":
        return dump(argv[2])
    limit = int(argv[1]) if len(argv) > 1 else 5
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "snapshot.json"), encoding="utf-8") as f:
        snapshot = json.load(f)
    # 이번엔 무엇이 걸리는지 통째로 보려고 기억을 비우고 돌린다(파일은 안 쓴다).
    snapshot["seen_report_urls"] = {}
    changes, checked, unread = check_reports(snapshot, limit=limit)
    print("의안 %d건 확인, 문서 %d건, 못 읽음 %d건" % (checked, len(changes), len(unread)))
    for c in changes:
        print("  [%s] %s\n      %s\n      %s" % (c["bill_no"], c["name"], c["title"][:100], c["url"]))
    if unread:
        print("  못 읽은 의안: %s" % ", ".join(unread))
        print("  (페이지를 못 받았거나, 말은 있는데 링크를 못 뽑은 경우다 — 위 경고를 보라)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


def dump(bill_id):
    """의안정보시스템 페이지가 실제로 무엇을 담고 있는지 찍어 본다.

    파싱이 안 될 때 추측으로 정규식을 고치지 않으려고 만들었다. 검토보고서가
    첫 페이지에 없고 다른 주소에서 따로 오는 구조일 수 있어서, 그 주소 후보를
    같이 뽑는다.
    """
    html = fetch_bill_page(bill_id)
    if html is None:
        print("페이지를 못 받았다")
        return 1
    print("길이 %d자" % len(html))
    print("\n[걸린 말과 그 언저리]")
    for t in REPORT_TERMS:
        for mo in list(re.finditer(re.escape(t), html))[:2]:
            at = mo.start()
            print("  %s @%d: …%s…" % (t, at, text_of(html[max(0, at - 150):at + 150])[:220]))

    print("\n[billId 를 달고 있는 다른 주소들]")
    subs = sorted(set(re.findall(r'''["'](/[^"']*\.do[^"']*billId[^"']*)["']''', html)))
    for u in subs[:25]:
        print("  " + u)
    if not subs:
        print("  (없음)")

    print("\n[자바스크립트 함수 호출 후보]")
    calls = sorted(set(re.findall(r"(\w*(?:[Ff]ile|[Dd]oc|[Rr]eport|[Pp]opup)\w*)\s*\(", html)))
    for c in calls[:30]:
        print("  " + c)

    print("\n[탭·메뉴로 보이는 글자]")
    tabs = re.findall(r'''<a[^>]*>\s*([^<>]{2,30}(?:보고서|원문|정보|심사|자료|추계)[^<>]{0,20})\s*</a>''', html)
    for t in sorted(set(tabs))[:20]:
        print("  " + " ".join(t.split()))
    return 0
