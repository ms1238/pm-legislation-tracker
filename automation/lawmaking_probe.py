# -*- coding: utf-8 -*-
"""국민참여입법센터에 '본문까지 뒤지는 검색'이 있는지 확인하는 진단 스크립트.

왜 이걸 따로 보는가:
  키워드로 한 번 검색해서 걸린 것만 보면 제일 싸다. 그런데 목록 화면의 검색
  필드(lsNm)는 법령 제명만 본다 — 실측으로 '킥보드' 0건, '이동장치' 0건이었다.
  본문을 훑는 검색이 어딘가에 따로 있다면 감시 방식을 통째로 그쪽으로 바꾸는 게
  맞다. 그래서 (1) lsNm이 정말 제목만 보는지 못 박고, (2) 사이트 통합검색을 찾는다.

성격: 진단 도구다. 상태 파일을 쓰지 않고 슬랙도 보내지 않는다. 손으로만 실행한다.
주의: lawmaking.go.kr 은 개발 컨테이너에서 막혀 있어 러너에서만 돈다.
"""
import os, re, sys, time, urllib.request, urllib.parse

UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25
HOST = "https://opinion.lawmaking.go.kr"
LIST_URL = HOST + "/gcom/ogLmPp"


def fetch(url, tries=2):
    print("\n>>> GET %s" % url)
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                print("    HTTP %s | %d bytes" % (resp.status, len(raw)))
                return raw.decode("utf-8", "replace")
        except Exception as e:
            print("    시도 %d/%d 실패: %r" % (attempt, tries, e))
            if attempt < tries:
                time.sleep(2)
    return None


def strip_tags(html):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def count_of(html):
    m = re.search(r"전체\s*([\d,]+)\s*건", strip_tags(html or ""))
    return m.group(1) if m else "?"


def rows_of(html):
    body = re.search(r"<tbody[^>]*>(.*?)</tbody>", html or "", re.S | re.I)
    if not body:
        return []
    return re.findall(r'href="/gcom/ogLmPp/(\d+)"[^>]*title="([^"]*)"', body.group(1))


def step1_is_title_only():
    """lsNm이 제목만 보는지 못 박는다.

    '행정절차법'은 거의 모든 예고 본문에 나오지만(제41조에 따라 공고) 제명에는
    안 나온다. 여기서 0건이면 제목 검색이 확실하다.
    """
    print("\n" + "=" * 70)
    print("STEP 1. 목록 검색(lsNm)이 제목만 보는가")
    print("=" * 70)
    for kw, why in [("행정절차법", "본문에는 거의 다 나오고 제명에는 없는 말"),
                    ("개정이유", "본문 머리말에 늘 나오는 말"),
                    ("자전거", "제명에 실제로 있는 말(대조군)")]:
        html = fetch("%s?lsNm=%s&finishIncludeYn=Y" % (LIST_URL, urllib.parse.quote(kw)))
        print("    '%s' (%s) → 전체 %s건" % (kw, why, count_of(html)))
        time.sleep(0.4)


def step2_find_search():
    """사이트 통합검색을 찾는다. 폼과 링크를 있는 그대로 본다."""
    print("\n" + "=" * 70)
    print("STEP 2. 통합검색 입구 찾기")
    print("=" * 70)
    html = fetch(HOST + "/")
    if not html:
        return
    for m in re.finditer(r"<form[^>]*>", html, re.I):
        print("    form: %s" % m.group(0)[:220])
    names = sorted(set(re.findall(r'<input[^>]*\bname=["\']([^"\']+)["\']', html)))
    print("    첫 화면 input name: %s" % ", ".join(names))
    hrefs = sorted({h for h in re.findall(r'href="([^"]+)"', html)
                    if re.search(r"search|Search|srch|totl|통합", h)})
    print("    검색으로 보이는 링크: %s" % (hrefs[:20] or "(없음)"))


def step3_try_search_urls():
    """흔한 통합검색 주소를 찔러 본다. '킥보드'가 걸리는 곳이 있으면 그게 답이다."""
    print("\n" + "=" * 70)
    print("STEP 3. 통합검색 주소 후보 실측 ('킥보드')")
    print("=" * 70)
    kw = urllib.parse.quote("킥보드")
    for path in ["/gcom/search?query=%s", "/gcom/totalSearch?query=%s",
                 "/search?query=%s", "/gcom/srch?srchWrd=%s",
                 "/gcom/ogLmPp?lmPpCts=%s", "/gcom/ogLmPp?srchCts=%s"]:
        url = HOST + (path % kw)
        html = fetch(url, tries=1)
        if html is None:
            continue
        found = rows_of(html)
        text = strip_tags(html)
        print("    → 전체 %s건 | 행 %d개 | '킥보드' 본문 포함: %s"
              % (count_of(html), len(found), "킥보드" in text))
        for no, title in found[:5]:
            print("       · %s %s" % (no, title))
        time.sleep(0.4)


def step4_search_table():
    """제명 검색 결과 페이지의 표 구조를 본다.

    감시 쪽에서 lsNm 검색이 전부 0행으로 나왔다. 전체 건수는 1건이라고 하면서
    행이 안 잡히니, 결과 표가 평소 목록과 다른 자리에 있는 것으로 보인다.
    """
    print("\n" + "=" * 70)
    print("STEP 4. 제명 검색 결과 페이지의 표 구조")
    print("=" * 70)
    html = fetch("%s?lsNm=%s" % (LIST_URL, urllib.parse.quote("자전거")))
    if not html:
        return
    print("    전체 %s건" % count_of(html))
    tbodies = re.findall(r"<tbody[^>]*>.*?</tbody>", html, re.S | re.I)
    print("    tbody %d개" % len(tbodies))
    for i, tb in enumerate(tbodies):
        links = re.findall(r'href="(/gcom/ogLmPp/\d+)"', tb)
        print("      [%d] %d자, ogLmPp 링크 %d개" % (i, len(tb), len(links)))
        if not links:
            print("          앞부분: %s" % strip_tags(tb)[:200])
    all_links = re.findall(r'href="/gcom/ogLmPp/(\d+)"[^>]*title="([^"]*)"', html)
    print("    페이지 전체에서 찾은 상세 링크 %d개: %s" % (len(all_links), all_links[:5]))
    loose = re.findall(r'href="/gcom/ogLmPp/(\d+)"', html)
    print("    title 속성 없이 링크만: %d개 %s" % (len(loose), loose[:8]))


def main():
    step1_is_title_only()
    step2_find_search()
    step3_try_search_urls()
    step4_search_table()
    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
