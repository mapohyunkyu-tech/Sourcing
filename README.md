# MarketScout 시즌 AI v3

- API 1회에 최대 5품목 수집
- 품목별 즉시 SQLite 저장
- 429 발생 즉시 중단
- 새 Client ID/Secret 저장 후 이어받기
- 자동 무한반복 없음: 실행당 호출 수 직접 지정
- 완료 품목 자동 건너뛰기
- 실패 품목 재시도
- DB 백업/복원

## 배포
압축을 풀어 GitHub 저장소 루트에 모든 파일을 덮어쓴 뒤 Streamlit Cloud에서 Reboot app 하세요.
`*.pyc`, `__pycache__`는 올리지 마세요.
