#!/usr/bin/env bash
# 라이브 cron 1회 부트스트랩 — 실행 순서:
#   1. gh auth login        (인터랙티브, 1회만)
#   2. bash scripts/bootstrap_live_cron.sh
#
# 동작:
#   - .env 의 7개 키를 GitHub Actions secrets로 설정
#   - 현재 브랜치 푸시 확인
#   - daily_pick workflow 첫 수동 실행
#   - 완료 시 Telegram 도착 확인 안내
#
# 멱등: secrets 동일 값이면 update만, 다르면 덮어씀.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

if [ ! -f .env ]; then
  echo "✗ .env 파일이 없음. 먼저 .env에 키를 채워두세요." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "✗ gh CLI 미인증. 'gh auth login' 먼저 실행." >&2
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "Repo: $REPO  default=$DEFAULT_BRANCH  current=$CURRENT_BRANCH"

# 1) secrets 설정
echo
echo "[1/3] GitHub Actions secrets 설정"

set_secret() {
  local key="$1"
  local val
  # cut으로 등호 이후 추출 → quote 제거 → leading/trailing whitespace 제거
  val=$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//' | awk '{$1=$1};1' || true)
  if [ -z "${val:-}" ]; then
    echo "  ⊘ $key  (.env에 없음, 스킵)"
    return 0
  fi
  printf '%s' "$val" | gh secret set "$key" --body -
  echo "  ✓ $key (len=${#val})"
}

for k in POLYGON_API_KEY ANTHROPIC_API_KEY FINNHUB_API_KEY \
         TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_ALERT_CHAT_ID \
         SEC_USER_AGENT TOSS_TOP30_TICKERS; do
  set_secret "$k"
done

# 2) 현재 브랜치 원격 동기화 확인
echo
echo "[2/3] 브랜치 동기화"
git fetch origin "$CURRENT_BRANCH" 2>&1 | head -5 || true
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null || echo "")
if [ "$LOCAL" != "$REMOTE" ]; then
  echo "  → push 필요"
  git push origin "$CURRENT_BRANCH"
else
  echo "  ✓ 이미 동기화됨 ($LOCAL)"
fi

# 3) workflow 첫 수동 실행
echo
echo "[3/3] daily_pick workflow 수동 트리거 (ref=$CURRENT_BRANCH)"
gh workflow run daily_pick.yml --ref "$CURRENT_BRANCH"
echo "  → 30초 대기 후 run 상태 확인…"
sleep 30
RUN_ID=$(gh run list --workflow=daily_pick.yml --limit=1 --json databaseId -q '.[0].databaseId')
echo "  run_id=$RUN_ID"
gh run watch "$RUN_ID" --interval 30 || true

# 결과
echo
echo "=== 완료 후 점검 ==="
echo "  - Telegram에 watchlist 도착 확인"
if [ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]; then
  echo "  ⚠ cron schedule(매일 09:00 KST)은 default branch($DEFAULT_BRANCH)의 workflow만 실행함"
  echo "    이 브랜치 ($CURRENT_BRANCH) 가 default가 아니면 PR로 merge 또는"
  echo "    Settings → General → Default branch 변경이 필요"
fi
echo "  - Actions 탭: gh run view $RUN_ID --web"
