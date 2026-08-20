# -*- coding: utf-8 -*-
"""입법예고 목록 조회 API를 찾는 진단 스크립트.

왜 중요한가:
  지금 감시는 "무슨 번호가 새로 올라왔나"를 알아내려고 웹 목록 화면을 긁는다.
  화면 구조에 기대는 유일한 부분이고, 실제로 제명 검색 결과 페이지에서 행을
  못 뽑는 문제가 났다. 목록 조회 API가 있으면 그 부분이 통째로 없어진다.

  상세 조회는 이미 확인됐다.
      https://www.lawmaking.go.kr/rest/ogLmPpMod/{ogLmPpSeq}/{mappingLbicId}/{announceType}.xml?OC=...
  같은 경로에 경로변수 없이 부르는 형태가 목록이라고 하므로, 그 변형들을 던져
  응답 형태로 판별한다.

성격: 진단 도구다. 상태 파일을 쓰지 않고 슬랙도 보내지 않는다. 손으로만 실행한다.
주의: lawmaking.go.kr 은 개발 컨테이너에서 막혀 있어 러너에서만 돈다.
      OC는 LAWMAKING_OC 환경변수로 받고, 출력할 때 가린다(이 저장소는 퍼블릭이다).
"""
import os, re, sys, time, urllib.request, urllib.parse

OC = os.environ.get("LAWMAKING_OC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (compatible; pm-legislation-tracker/1.0)"}
TIMEOUT = 25
REST = "https://www.lawmaking.go.kr/rest/ogLmPpMod"


def redact(t):
    return t.replace(OC, "***OC***") if (OC and t) else t


def fetch(url, tries=2):
    print("\n>>> GET %s" % redact(url))
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                print("    HTTP %s | %s | %d bytes"
                      % (resp.status, resp.headers.get("Content-Type", ""), len(raw)))
                return resp.status, raw.decode("utf-8", "replace")
        except Exception as e:
            print("    시도 %d/%d 실패: %r" % (attempt, tries, e))
            if attempt < tries:
                time.sleep(2)
    return None, ""


def describe(body):
    """응답이 목록인지 아닌지 눈으로 판별할 수 있게 요약한다."""
    if not body:
        return
    head = body.lstrip()[:1]
    if head == "<" and "<!DOCTYPE" in body[:200].upper():
        print("    → HTML 화면이 왔다(목록 API가 아니다)")
        return
    tags = []
    for t in re.findall(r"<([A-Za-z_][\w.\-]*)[ >]", body):
        if t not in tags:
            tags.append(t)
    print("    태그(%d종): %s" % (len(tags), ", ".join(tags[:40])))
    for tag in ["totalCnt", "totalCount", "resultCount", "numOfRows", "page"]:
        m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), body)
        if m:
            print("    %s = %s" % (tag, m.group(1).strip()))
    seqs = re.findall(r"<ogLmPpSeq>(\d+)</ogLmPpSeq>", body)
    names = re.findall(r"<lsNm>(.*?)</lsNm>", body, re.S)
    print("    ogLmPpSeq %d개: %s" % (len(seqs), seqs[:10]))
    print("    lsNm %d개: %s" % (len(names), [n.strip()[:40] for n in names[:5]]))
    if len(seqs) > 1:
        print("    *** 여러 건이 왔다 — 목록 조회로 보인다 ***")
    print("    앞부분: %s" % redact(re.sub(r"\s+", " ", body))[:600])


def main():
    if not OC:
        print("LAWMAKING_OC 가 없다. 종료.")
        return 1
    print("목록 조회 API 탐색 시작")

    oc = urllib.parse.quote(OC)
    bike = urllib.parse.quote("자전거")
    # 문서에 실린 형태:
    #   ogLmPpMod.xml?OC=..&lsClsCd=AA0103&diff=0     법령종류 총리령 + 진행중
    #   ogLmPpMod.html?OC=..&cptOfiOrgCd=1613000&lsNm=건축법   부처 + 예고명 포함
    # 우리에게 중요한 건 diff(진행중만)와 lsNm(예고명 부분일치)이다.
    candidates = [
        ("전체", "%s.xml?OC=%s" % (REST, oc)),
        ("진행중만 diff=0", "%s.xml?OC=%s&diff=0" % (REST, oc)),
        ("문서 예제(총리령+진행중)", "%s.xml?OC=%s&lsClsCd=AA0103&diff=0" % (REST, oc)),
        ("예고명 '자전거'", "%s.xml?OC=%s&lsNm=%s" % (REST, oc, bike)),
        ("예고명 '도로교통'+진행중", "%s.xml?OC=%s&lsNm=%s&diff=0"
         % (REST, oc, urllib.parse.quote("도로교통"))),
        ("예고명 '킥보드'(본문검색 되는지)", "%s.xml?OC=%s&lsNm=%s"
         % (REST, oc, urllib.parse.quote("킥보드"))),
    ]
    for label, url in candidates:
        print("\n### %s" % label)
        st, body = fetch(url, tries=2)
        if st is None:
            continue
        describe(body)
        time.sleep(0.4)

    print("\n진단 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
