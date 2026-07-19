# MarketScout 시즌 AI v2

- 네이버 DataLab 3년 원자료를 SQLite에 품목별 저장
- 같은 품목은 저장 데이터를 우선 사용하여 API 호출 절감
- 분석 결과 영구 저장(동일 Streamlit 컨테이너 내)
- DB 백업 다운로드 / 업로드 복원
- Streamlit Cloud 재배포나 컨테이너 교체 전에는 DB 백업 권장

## 실행
`streamlit run app.py`

## API
설정에서 `NAVER Developers 데이터랩`을 선택하고 Client ID / Secret을 저장합니다.
월별 전체 분석은 아직 저장되지 않은 품목만 API를 호출하므로, 처음 대량 구축 시 일일 1000회 한도를 고려해 카테고리별로 나누어 실행하세요.
