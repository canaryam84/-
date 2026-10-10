"""삭제 요청(GitHub 이슈) 본문에서 대상을 꺼내 blocklist.txt에 추가합니다.

이슈 내용은 환경변수 ISSUE_BODY로 받습니다(워크플로 안에 직접 넣지 않아 안전).
사람의 확인 없이 바로 반영하며, 결과 메시지를 GITHUB_OUTPUT의 message로 남깁니다.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKLIST = ROOT / "blocklist.txt"


def target_from(body):
    # 이슈 양식은 '### 삭제할 종목명 또는 기사 링크' 아래 줄에 값이 들어옴
    m = re.search(r"###[^\n]*삭제할[^\n]*\n+(.+?)(?:\n###|\Z)", body, re.S)
    value = (m.group(1) if m else body).strip().splitlines()
    value = value[0].strip() if value else ""
    value = re.sub(r"[\x00-\x1f#]", "", value)[:300].strip()
    return "" if value in ("", "_No response_") else value


def main():
    target = target_from(os.environ.get("ISSUE_BODY", ""))
    if not target:
        msg = "삭제할 종목명이나 기사 링크를 찾지 못했어요. 양식에 맞춰 다시 요청해 주세요."
    else:
        lines = BLOCKLIST.read_text("utf-8").splitlines() if BLOCKLIST.exists() else []
        if target.lower() not in {l.split("#")[0].strip().lower() for l in lines}:
            with BLOCKLIST.open("a", encoding="utf-8") as f:
                f.write(target + "\n")
        msg = f"'{target}' 항목을 사이트에서 숨김 처리했어요. 몇 분 안에 반영돼요."
    print(msg)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"message={msg}\n")


if __name__ == "__main__":
    main()
