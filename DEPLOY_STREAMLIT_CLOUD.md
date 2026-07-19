# Streamlit Cloud 배포

저장소 최상위에 아래 파일을 **전부 함께** 올리세요.

- app.py
- database.py
- engine.py
- settings_store.py
- agri_fish_item_master_v1.db
- item_master_seed_v1.csv
- requirements.txt

기존 저장소에 같은 이름의 파일이 있으면 모두 덮어써야 합니다. `app.py`만 교체하면 새 app.py가 예전 database.py의 함수를 찾지 못해 ImportError가 발생합니다.

Streamlit Cloud 설정:

- Main file path: `app.py`
- Python: 3.11 또는 3.12 권장

배포 후 Reboot app 또는 Clear cache를 실행하세요.
