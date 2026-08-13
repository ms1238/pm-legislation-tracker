# -*- coding: utf-8 -*-
"""
슬랙 채널로 임의의 메시지를 보내는 스크립트.

일일 감지(cloud_check_updates.py)와 별개로, 사람이 페이지를 직접 수정한 뒤
"이렇게 바꿨다"고 채널에 알릴 때 쓴다. GitHub Actions의 notify.yml에서
workflow_dispatch로 실행하며, 메시지 본문은 SLACK_MESSAGE 환경변수로 받는다.

로컬/개발 컨테이너에서는 슬랙·국회 도메인이 막혀 있을 수 있으므로
러너에서 실행하는 것을 전제로 한다.
"""
import json, os, sys, urllib.request

ARTIFACT_URL = "https://claude.ai/code/artifact/80fc7afc-0941-46aa-bd9f-b89e687e3c08"
GITHUB_PAGES_URL = "https://ms1238.github.io/pm-legislation-tracker/"


def send(webhook_url, text):
    payload = {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                # 공개 페이지가 주 링크 — 자동 배포라 항상 최신이다.
                {"type": "button", "text": {"type": "plain_text", "text": "🌐 트래커 열기"}, "url": GITHUB_PAGES_URL, "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "🛴 아티팩트(수동 갱신)"}, "url": ARTIFACT_URL},
            ]},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    message = os.environ.get("SLACK_MESSAGE", "").strip()
    if not webhook:
        print("SLACK_WEBHOOK_URL 환경변수가 없습니다. 종료.")
        sys.exit(1)
    if not message:
        print("SLACK_MESSAGE 환경변수가 비어 있습니다. 종료.")
        sys.exit(1)
    status = send(webhook, message)
    print("슬랙 전송 완료 (HTTP %s), %d자" % (status, len(message)))


if __name__ == "__main__":
    main()
