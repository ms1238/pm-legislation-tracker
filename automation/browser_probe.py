# -*- coding: utf-8 -*-
"""브라우저로 의안 상세 페이지를 띄워서, 자바스크립트가 실제로 부르는 주소를 본다.

여기까지 온 경위:
  - 국회 Open API 에는 검토보고서가 없다.
  - 상세 페이지 HTML 에는 제안이유·제안자·의안원문·심사경과가 하나도 없다.
    내용은 전부 자바스크립트가 받아 채운다.
  - 페이지에 적힌 /bill/bi/common/findBillDetail.do 는 통하지만 431자짜리
    의안 기본정보뿐이고 문서 목록이 없다.
  - 형제 주소 이름을 13개 추측해 눌러 봤지만 전부 404 였다(downloadDtlZip.do 만 400).

그래서 추측을 그만두고 직접 본다. 이 스크립트는 진단 전용이다 — 매일 도는 일에
브라우저를 띄울 생각은 없다. 여기서 주소를 알아내면 그다음부터는 평범한 HTTP 로
긁는다.

    python automation/browser_probe.py <billId>
"""
import re
import sys

TARGET = "https://likms.assembly.go.kr/bill/billDetail.do?billId=%s"
TERMS = ["검토보고서", "검토보고", "심사보고서", "비용추계"]


def main(argv):
    if len(argv) < 2:
        print("billId 를 달라")
        return 1
    bill_id = argv[1]
    from playwright.sync_api import sync_playwright

    calls = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        def on_response(resp):
            u = resp.url
            if "likms.assembly.go.kr" not in u:
                return
            if any(u.endswith(ext) for ext in (".js", ".css", ".png", ".jpg", ".gif", ".woff", ".woff2", ".ico")):
                return
            calls.append((resp.request.method, resp.status, u))

        page.on("response", on_response)
        # networkidle 로 기다리면 60초를 넘긴다 — 이 페이지는 무언가를 계속 부른다.
        # 뼈대만 받고 고정 시간 기다린다. 타임아웃이 나도 그때까지 잡힌 주소는 쓸모가
        # 있으니 예외로 죽지 않는다.
        try:
            page.goto(TARGET % bill_id, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("!! goto 실패(%s) — 그때까지 잡힌 것만 본다" % type(e).__name__)
        page.wait_for_timeout(10000)

        try:
            html = page.content()
            text = page.inner_text("body")
        except Exception as e:
            print("!! 페이지 내용을 못 읽었다(%s)" % e)
            html, text = "", ""

        print("=== 페이지가 부른 주소 (정적파일 제외) ===")
        for method, status, u in calls:
            print("  %-5s %s  %s" % (method, status, u))

        print("\\n=== 그려진 글에 검토보고서류가 있나 ===")
        for t in TERMS:
            n = text.count(t)
            print("  %-8s %d회" % (t, n))

        print("\\n=== 검토보고서 언저리의 링크 ===")
        found = False
        for t in TERMS:
            for mo in list(re.finditer(re.escape(t), html))[:3]:
                at = mo.start()
                chunk = html[max(0, at - 700):at + 700]
                hrefs = re.findall(r'''(?:href|onclick)\\s*=\\s*(?:"([^"]+)"|'([^']+)')''', chunk)
                hrefs = [a or b for a, b in hrefs]
                hrefs = [h for h in hrefs if h not in ("#", "javascript:;")]
                if hrefs:
                    found = True
                    print("  [%s] %s" % (t, " | ".join(hrefs[:5])[:300]))
        if not found:
            print("  (없음)")

        print("\\n=== 문서로 보이는 글자가 있는 행 ===")
        for line in text.splitlines():
            line = line.strip()
            if line and any(t in line for t in TERMS):
                print("  " + line[:140])

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
