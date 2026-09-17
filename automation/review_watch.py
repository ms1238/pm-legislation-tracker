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

    print("\n[의안 내용이 서버에서 그려지기는 하나]")
    for probe in ["제안이유", "의안접수정보", "제안자", "소관위원회", "의안원문", "심사경과"]:
        print("  %-10s %s" % (probe, "있음" if probe in html else "없음"))

    print("\n[페이지가 부르는 .do 주소들]")
    dos = sorted(set(re.findall(r'''["\'](/[\w/]+\.do)''', html)))
    for u in dos[:30]:
        print("  " + u)
    if not dos:
        print("  (없음)")

    print("\n[ajax/fetch 로 보이는 주소 문자열]")
    urls = sorted(set(re.findall(r'''(?:url|action)\s*[:=]\s*["\']([^"\']{4,120})["\']''', html)))
    for u in urls[:30]:
        print("  " + u)
    if not urls:
        print("  (없음)")

    print("\n[script src]")
    for u in sorted(set(re.findall(r'''<script[^>]+src=["\']([^"\']+)["\']''', html)))[:20]:
        print("  " + u)

    print("\n[탭·메뉴로 보이는 글자]")
    tabs = re.findall(r'''<a[^>]*>\s*([^<>]{2,30}(?:보고서|원문|정보|심사|자료|추계)[^<>]{0,20})\s*</a>''', html)
    for t in sorted(set(tabs))[:20]:
        print("  " + " ".join(t.split()))
    return 0

# 실측으로 확인된 것(2026-09-17):
#   GET /bill/bi/common/findBillDetail.do?billId=...  → HTTP 200 JSON
#   다만 431자짜리 의안 기본정보뿐이고 문서 목록은 없다. POST 는 307 로 막힌다.
BI_BASE = "https://likms.assembly.go.kr/bill/bi"


def _get(url, referer=None):
    head = dict(UA)
    head["Accept"] = "application/json, text/plain, */*"
    head["X-Requested-With"] = "XMLHttpRequest"
    if referer:
        head["Referer"] = referer
    req = urllib.request.Request(url, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except Exception as e:
        return None, str(e), b""


def probe_info(bill_ids):
    """브라우저가 실제로 부르는 주소를 그대로 두드린다.

    2026-09-17 브라우저 관찰로 확인:
      POST /bill/bi/bill/detail/billInfo.do   ← 페이지가 내용을 받는 곳 (GET 아님)
      상세 페이지는 /bill/bi/billDetailPage.do?billId=...&currMenuNo=2600044 로 간다.

    같은 관찰에서 더 중요한 것도 나왔다. 2219714(소관위 접수)는 브라우저로 띄워도
    검토보고서가 0회다 — 아직 없는 의안이다. 검토보고서는 상정 단계에서 나오므로
    심사가 진행된 의안으로 확인해야 한다. 그래서 여러 건을 받는다.
    """
    import json as _json
    for bid in bill_ids:
        print("\n=== %s ===" % bid)
        for ctype, body in (("application/json", _json.dumps({"billId": bid}).encode()),
                            ("application/x-www-form-urlencoded", ("billId=%s" % bid).encode())):
            head = dict(UA)
            head["Accept"] = "application/json, text/plain, */*"
            head["X-Requested-With"] = "XMLHttpRequest"
            head["Content-Type"] = ctype
            head["Referer"] = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=%s" % bid
            req = urllib.request.Request(
                "https://likms.assembly.go.kr/bill/bi/bill/detail/billInfo.do", data=body, headers=head)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    st = resp.status
            except Exception as e:
                print("  POST %-34s → %s" % (ctype.split("/")[-1], e))
                continue
            said = [t for t in REPORT_TERMS if t in raw]
            print("  POST %-34s → HTTP %s, %d자, 검토보고서류: %s"
                  % (ctype.split("/")[-1], st, len(raw), ", ".join(said) or "없음"))
            try:
                data = _json.loads(raw)
                def keys(o, pre=""):
                    if isinstance(o, dict):
                        for k, v in o.items():
                            if isinstance(v, (dict, list)):
                                yield from keys(v, pre + k + ".")
                            else:
                                yield pre + k
                    elif isinstance(o, list) and o:
                        yield from keys(o[0], pre + "[].")
                ks = sorted(set(keys(data)))
                print("     필드 %d개: %s" % (len(ks), ", ".join(ks)[:600]))
            except Exception:
                print("     JSON 아님: %s" % " ".join(raw[:200].split()))
            if said:
                i = raw.find(said[0])
                print("     언저리: …%s…" % " ".join(raw[max(0, i - 300):i + 400].split()))
            time.sleep(0.5)
            break     # 먼저 통한 방식만 본다
    return 0


def probe_docs(bill_id):
    """문서 목록을 주는 주소를 찾는다.

    findBillDetail.do 는 의안 기본정보만 준다(실측). 문서는 다른 데서 온다.
    형제 주소를 눌러 보고 어느 것이 JSON 을 주는지, 그 안에 검토보고서가 있는지 본다.
    이름을 추측해서 코드에 박지 않으려고 여기서 먼저 확인한다.
    """
    ref = BILL_DETAIL % bill_id
    print("[findBillDetail 전문]")
    st, ct, raw = _get("%s/common/findBillDetail.do?billId=%s" % (BI_BASE, bill_id), ref)
    body = raw.decode("utf-8", "replace")
    print("  HTTP %s %s\n  %s" % (st, ct, body))

    print("\n[형제 주소 눌러 보기]")
    names = [
        "common/findBillDocList.do", "common/findBillDoc.do", "common/findDocList.do",
        "bill/detail/findBillDtlDocList.do", "bill/detail/findDocList.do",
        "bill/detail/findBillDetailDoc.do", "bill/detail/billDocList.do",
        "bill/detail/findBillDtl.do", "bill/detail/findBillStep.do",
        "common/findBillStepList.do", "common/findBillRelatedDoc.do",
        "dwld/findDocBndlList.do", "bill/detail/downloadDtlZip.do",
    ]
    for n in names:
        url = "%s/%s?billId=%s" % (BI_BASE, n, bill_id)
        st, ct, raw = _get(url, ref)
        if st is None:
            print("  %-42s → %s" % (n, ct[:60]))
            continue
        body = raw.decode("utf-8", "replace")
        said = [t for t in REPORT_TERMS if t in body]
        mark = "★" if said else " "
        print("  %s %-40s → HTTP %s %s %d바이트%s" % (mark, n, st, ct.split(";")[0], len(raw),
                                                    (" | " + ", ".join(said)) if said else ""))
        if said or (len(raw) > 200 and "json" in ct.lower()):
            print("      %s" % " ".join(body[:400].split()))
        time.sleep(0.4)
    return 0


def probe_api(bill_id):
    """상세 페이지가 axios 로 부르는 데이터 엔드포인트를 두드려 본다.

    상세 페이지 HTML 에는 제안이유·제안자·의안원문·심사경과가 하나도 없다(실측).
    내용은 전부 자바스크립트가 받아 채운다. 그 주소가 페이지 안에 적혀 있었다:
      /bi/common/findBillDetail.do          ← 의안 상세 데이터
      /bill/bi/bill/detail/downloadDtlZip.do ← 문서 묶음 내려받기

    호출 방식(GET/POST, form/JSON)은 적혀 있지 않아 여기서 하나씩 재 본다.
    """
    import json as _json
    targets = [
        ("GET  form", "https://likms.assembly.go.kr/bi/common/findBillDetail.do?billId=%s" % bill_id, None, None),
        ("POST form", "https://likms.assembly.go.kr/bi/common/findBillDetail.do",
         ("billId=%s" % bill_id).encode(), "application/x-www-form-urlencoded"),
        ("POST json", "https://likms.assembly.go.kr/bi/common/findBillDetail.do",
         _json.dumps({"billId": bill_id}).encode(), "application/json"),
        ("GET  form", "https://likms.assembly.go.kr/bill/bi/common/findBillDetail.do?billId=%s" % bill_id, None, None),
    ]
    for label, url, body, ctype in targets:
        head = dict(UA)
        head["Accept"] = "application/json, text/plain, */*"
        head["X-Requested-With"] = "XMLHttpRequest"
        head["Referer"] = BILL_DETAIL % bill_id
        if ctype:
            head["Content-Type"] = ctype
        req = urllib.request.Request(url, data=body, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                code = resp.status
        except Exception as e:
            print("  %s %-70s → %s" % (label, url[:70], e))
            continue
        said = [t for t in REPORT_TERMS if t in raw]
        print("  %s %-70s → HTTP %s, %d자, 검토보고서류: %s"
              % (label, url[:70], code, len(raw), ", ".join(said) or "없음"))
        print("     첫 300자: %s" % " ".join(raw[:300].split()))
        if said:
            i = raw.find(said[0])
            print("     언저리: …%s…" % " ".join(raw[max(0, i - 200):i + 300].split()))
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--api":
        sys.exit(probe_api(sys.argv[2]))
    if len(sys.argv) > 2 and sys.argv[1] == "--docs":
        sys.exit(probe_docs(sys.argv[2]))
    if len(sys.argv) > 2 and sys.argv[1] == "--info":
        sys.exit(probe_info(sys.argv[2].split(",")))
    sys.exit(main(sys.argv))
