# 주식 자동매매 프로그램 — 맥/윈도우 왔다갔다 작업 가이드

## 매번 작업 시작할 때 (맥이든 윈도우든 공통)

```bash
git pull origin claude/stock-auto-trading-program-04olvt
```

관심종목/보유종목/매매일지는 `webapp/app.db`(SQLite)에 저장되고, 이 파일도 git으로 같이
동기화된다. **pull을 안 하고 시작하면 지난번 다른 컴퓨터에서 추가한 관심종목/매매일지가
안 보인다.**

## 매번 작업 끝낼 때 (공통)

대시보드에서 관심종목을 추가했거나 매매일지를 기록했다면, 코드를 수정하지 않았어도
DB 파일이 바뀐 것이므로 커밋 & 푸시해야 다음 컴퓨터에서 보인다.

```bash
git add trading-bot/webapp/app.db   # 코드도 고쳤으면 나머지 파일도 같이 add
git commit -m "매매일지/관심종목 업데이트"
git push origin claude/stock-auto-trading-program-04olvt
```

서버(uvicorn)는 `Ctrl+C`로 꺼두고 나서 진행하는 게 안전하다 (DB 파일이 쓰기 중일 때
커밋하는 걸 피하기 위해).

## 컴퓨터를 처음 세팅할 때 (맥/윈도우 각각 최초 1회)

가상환경(`venv`)은 컴퓨터마다 새로 만들어야 한다 (git에 안 올라가고, 맥/윈도우 서로
호환도 안 됨).

**맥**
```bash
cd ~/my-first-web-site/trading-bot
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**윈도우 (PowerShell)**
```powershell
cd D:\my-first-web-site\trading-bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
(스크립트 실행이 막히면 `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` 한 번 실행)

## 서버 실행 (공통, venv 활성화된 상태에서)

```bash
python -m uvicorn webapp.server:app --reload
```
브라우저에서 http://127.0.0.1:8000 접속.

## 컴퓨터마다 안 따라가는 것 (직접 챙겨야 함)

- **`.env` (KIS 앱키/시크릿)**: 보안상 git에 절대 안 올림. 맥/윈도우 양쪽에 KIS Developers에서
  받은 값을 각각 직접 입력해야 한다 (`.env.example`을 `.env`로 복사 후 채우기). 비밀번호
  관리자 같은 곳에 안전하게 보관해두고 필요할 때마다 옮겨 적는 걸 추천.
- **`venv/`**: 컴퓨터마다 새로 생성 (위 참고).
- **`webapp/krx_symbols_cache.json`**: 종목 검색용 캐시. 없으면 자동으로 소수 종목만
  뜨는 폴백 목록을 쓴다. 아쉬우면 각 컴퓨터에서 한 번씩 새로 받으면 됨 (파이썬 콘솔에서
  `from webapp.krx_symbols import refresh_symbol_cache; refresh_symbol_cache()`).
