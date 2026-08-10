#!/bin/bash
# 지정한 종료 시각까지 몇 분 간격으로 `python main.py live`를 반복 실행한다.
# 사용법: ./run_live_window.sh HH:MM [간격(분), 기본 5] [main.py에 넘길 추가 옵션...]
# 예:    ./run_live_window.sh 22:15 3 --all-overseas --max-positions 999
#
# 반드시 trading-bot 디렉터리에서, venv를 활성화한 상태로 직접 실행할 것.
# 언제든 Ctrl+C로 멈출 수 있다.

set -euo pipefail

END_TIME="${1:?종료 시각(HH:MM)을 첫 번째 인자로 주세요}"
INTERVAL_MIN="${2:-5}"
shift $(( $# >= 2 ? 2 : 1 ))
EXTRA_ARGS=("$@")

END_EPOCH=$(date -j -f "%H:%M" "$END_TIME" +%s 2>/dev/null || date -d "$END_TIME" +%s)
NOW_EPOCH=$(date +%s)
if [ "$END_EPOCH" -le "$NOW_EPOCH" ]; then
  END_EPOCH=$((END_EPOCH + 86400))  # 자정을 넘기는 경우 다음날로 취급
fi

echo "[run_live_window] ${END_TIME}까지, ${INTERVAL_MIN}분 간격으로 실행합니다. (Ctrl+C로 중단)"

while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
  echo ""
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 실행 ====="
  python main.py live "${EXTRA_ARGS[@]}"
  sleep $((INTERVAL_MIN * 60))
done

echo "[run_live_window] 종료 시각(${END_TIME}) 도달, 반복 종료합니다."
