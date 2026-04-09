# bandi-shuttle-viewer Vercel Bundle

이 폴더의 파일들을 `bandi-shuttle-viewer` 저장소 루트에 올리면 됩니다.

포함 파일:
- `build_shuttle_webapp.py`
- `shuttle_schedule_parser.py`
- `build_for_vercel.py`
- `vercel.json`
- `반디로고.png`
- `등송영표 3월.xlsx`
- `등송영표 4월.xlsx`

운영 규칙:
- 월별 운행표 파일은 `등송영표 n월.xlsx` 형식으로 저장소 루트에 추가
- Vercel은 `build_for_vercel.py`를 실행해서 가장 최신 월 파일을 기준으로 `webapp/index.html`을 생성
- `build_shuttle_webapp.py` 내부에서 같은 규칙의 월 파일들을 모두 묶어 일정 번들을 만듦

GitHub 업로드:
1. `bandi-shuttle-viewer` 저장소의 `Code` 탭으로 이동
2. `Add file` -> `Upload files`
3. 이 폴더 안의 파일들을 저장소 루트에 업로드
4. `Commit changes`

Vercel:
1. 저장소 import
2. Framework Preset: `Other`
3. 프로젝트 설정은 `vercel.json`이 대신함

주의:
- 현재 1차 배포는 정적 뷰어 중심입니다.
- 브라우저 내 수정은 `localStorage`에 저장됩니다.
- 서버 기반 export/API는 다음 단계에서 별도 옮겨야 합니다.
