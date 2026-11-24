# MLO01-FINAL-2TEAM

- 팀명 : MLO01-FINAL-2TEAM
- 프로젝트 기간 : 26.11.13 \~ 27.01.07
- 프로젝트 주제 : VoC 분석 모델의 무중단 배포 및 실시간 모니터링 시스템
- 팀원 : 김민서, 신동엽, 안길찬, 이인건, 이형인

## 로그인 백엔드

페이지별 프론트엔드와 백엔드는 `src/pages/<번호>-<페이지>/`에 함께 둔다. 로그인 화면과 CSS는 `src/pages/01-LoginPage/`에 있고 로그인 API와 테스트는 그 아래 `backend/`에 있다. React는 `src/main.jsx`에서 `src/App.jsx`를 렌더링하고 `App`에서 로그인 화면을 불러온다. 독립 실행 시안은 `01-LoginPage.html`에 있다.

### 로컬 실행

저장소 루트의 PowerShell에서 실행한다. `DATABASE_URL`에 팀 PostgreSQL 접속 정보를 설정한다. DB 계정에는 연결할 스키마의 `staff_user`와 `company` 조회 권한이 필요하다.

```powershell
python -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -r src/pages/01-LoginPage/backend/requirements.txt
$env:DATABASE_URL = 'postgresql://<user>:<password>@<host>:5432/<dbname>'
& $python -m uvicorn 'src.pages.01-LoginPage.backend.main:create_app' --factory
```

로그인은 `staff_user.email`로 직원을 찾고 `company_uuid`로 연결된 `company.email_domain`이 입력 이메일의 도메인과 일치하는지 확인한다. `staff_user.password_hash`에는 Argon2id 해시를 저장한다. 두 테이블은 PostgreSQL에서 소문자 이름으로 조회하며 연결의 `search_path`에서 보여야 한다. 대소문자를 무시했을 때 이메일이 중복되면 인증을 거부한다. 앱은 팀 DB 테이블을 생성하거나 수정하지 않는다.

별도 터미널에서 저장소 루트 기준으로 아래 명령을 실행한 뒤 `http://localhost:5173`에 접속한다. React 설정과 빌드 결과는 `React/`에 있고, 앱 소스는 `src/`에 있다. Vite는 `/api` 요청을 `http://127.0.0.1:8000`으로 프록시한다.

```powershell
npm --prefix React ci
npm --prefix React run dev
```

`npm --prefix React run build`로 `React/dist/`를 생성한다. 정적 파일을 배포할 때는 같은 도메인의 `/api`를 백엔드에 연결한다.

### React 연동 계약

`POST /api/auth/login`에 `Content-Type: application/json`으로 `email`과 `password` 문자열을 보낸다.

| HTTP 상태 | 응답 | 화면 처리 |
| --- | --- | --- |
| 200 | `{"email":"user01@example.com"}` | 로그인 성공 표시 또는 `onLoginSuccess` 호출 |
| 422 | `detail.code`가 `INVALID_EMAIL`, `PASSWORD_REQUIRED` 또는 `INVALID_REQUEST` | `detail.message` 표시 |
| 401 | `detail.code`가 `INVALID_CREDENTIALS` | 두 입력란에 실패 상태 표시, `detail.message` 안내 |
| 503 | `detail.code`가 `AUTH_UNAVAILABLE` | 재시도 안내 |

이메일은 `@` 앞에 영문·숫자 5~15자와 점이 포함된 도메인을 받는다. 등록 여부는 대소문자 구분 없이 확인한다. 미등록 이메일과 비밀번호 불일치에는 같은 실패 응답을 반환한다. 이 API의 200 응답은 자격 증명 검증 결과이며 세션이나 토큰을 발급하지 않는다.

업무 홈 경로가 생기면 `src/App.jsx`에서 `LoginPage`의 `onLoginSuccess` 콜백에 화면 이동을 연결한다. 콜백은 `{ email }`을 받는다. 콜백 없이 사용하면 로그인 화면에서 성공 메시지를 표시한다.

### 테스트

```powershell
& $python -m pip install -r src/pages/01-LoginPage/backend/requirements-test.txt
& $python -m unittest discover -s src/pages/01-LoginPage/backend/tests -v
```

PostgreSQL 통합 테스트를 포함하려면 스키마를 생성할 수 있는 테스트 전용 DB의 접속 정보를 `VOC_TEST_DATABASE_URL`에 넣고 같은 테스트 명령을 실행한다. 테스트가 생성한 스키마는 종료 시 제거한다.
