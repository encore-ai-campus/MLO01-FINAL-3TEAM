# MLO01-FINAL-2TEAM

- 팀명 : MLO01-FINAL-2TEAM
- 프로젝트 기간 : 26.11.13 \~ 27.01.07
- 프로젝트 주제 : VoC 분석 모델의 무중단 배포 및 실시간 모니터링 시스템
- 팀원 : 김민서, 신동엽, 안길찬, 이인건, 이형인

## 티켓 목록 실행

저장소 루트에서 기존 수집기 설정을 사용해 다음 명령을 실행한다. 초기화 서비스가 Bronze·Silver 및 업무 테이블을 준비한다. 같은 주소에서 React 화면과 FastAPI를 제공한다.

```bash
docker compose build db-worker ticket-desk
docker compose up -d --no-build ticket-desk db-worker
```

브라우저에서 [전체 티켓](http://127.0.0.1:18082/#03-01) 또는 [내게 할당된 티켓](http://127.0.0.1:18082/#03-02)을 연다. 회사 직원 계정으로 로그인하면 `ticket.read` 권한과 소속 회사에 맞는 티켓이 보인다. 직원에게 할당된 티켓은 로그인 세션의 직원 ID로 조회한다. 기능 브랜치의 `03-TicketListPage.html` 파일로 진입해도 같은 앱이 열린다.

사업자·직원·고객의 실제 매핑은 관리자가 등록한다. 등록한 수집 연결에서 `customer_inquiry`로 확인된 Silver 메시지가 들어오면 티켓번호와 첫 문의 메시지를 한 트랜잭션으로 생성한다. 수집 기록의 `message_kind`가 `unknown`이면 확인 전까지 업무 목록에서 대기한다. [회사·권한·채널 연결과 Silver 적재 가이드](collector/README.md)

새 로컬 DB에서 첫 직원을 등록하려면 회사와 도메인을 먼저 구성한다. 관리자가 아래 명령에 새 직원의 비밀번호를 입력하면 Argon2id 해시가 출력된다.

```bash
docker compose exec ticket-desk python -c "from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass('새 직원 비밀번호: ')))"
docker compose exec postgres psql -U voc -d voc
```

빈 DB에서는 psql에서 첫 회사와 산업 도메인을 등록한다. 기존 회사가 있다면 아래 INSERT를 건너뛰고 `SELECT company_uuid,name,email_domain FROM public.company;`로 소속 회사 UUID를 확인한다.

```sql
\set ON_ERROR_STOP on
\prompt '산업 도메인 이름: ' industry
\prompt '회사 이름: ' company_name
\prompt '회사 이메일 도메인(@ 제외): ' mail_domain
BEGIN;
INSERT INTO public.domain(name) VALUES (:'industry') RETURNING domain_uuid \gset
INSERT INTO public.company(name,email_domain,domain_uuid)
VALUES (:'company_name',lower(:'mail_domain'),:'domain_uuid') RETURNING company_uuid;
COMMIT;
```

다음 명령으로 첫 직원을 등록한다. 로그인 이메일은 등록한 회사의 이메일 도메인과 일치해야 한다. 기존 직원은 수집 가이드의 권한 부여 단계부터 진행한다.

```sql
\prompt '소속 회사 UUID: ' company_id
\prompt '회사 이메일: ' email
\prompt '직원 이름: ' staff_name
\prompt 'Argon2id 해시: ' password_hash
INSERT INTO public.staff_user(company_uuid,email,name,password_hash)
VALUES (:'company_id'::uuid,lower(:'email'),:'staff_name',:'password_hash');
```

## 로그인 백엔드

페이지별 프론트엔드와 백엔드는 `src/pages/<번호>-<페이지>/`에 함께 둔다. 로그인 화면과 CSS는 `src/pages/01-LoginPage/`에 있고 로그인 API와 테스트는 그 아래 `backend/`에 있다. React는 `src/main.jsx`에서 `src/App.jsx`를 렌더링하고 `App`에서 로그인 화면을 불러온다. 독립 실행 시안은 `01-LoginPage.html`에 있다. 로그인 후 `App`이 티켓 목록·상세로 연결한다.

### 로컬 실행

저장소 루트의 PowerShell에서 실행한다. `DATABASE_URL`에 팀 PostgreSQL 접속 정보를 설정한다. 먼저 Compose 초기화로 업무 스키마를 준비한다. API 계정은 로그인·권한·티켓 테이블을 조회하고 `staff_session`의 세션을 생성·정리할 수 있어야 한다.

```powershell
python -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -r src/pages/01-LoginPage/backend/requirements.txt
$env:DATABASE_URL = 'postgresql://<user>:<password>@<host>:5432/<dbname>'
& $python -m uvicorn 'src.pages.01-LoginPage.backend.main:create_app' --factory
```

로그인은 `staff_user.email`로 직원을 찾고 `company_uuid`로 연결된 `company.email_domain`이 입력 이메일의 도메인과 일치하는지 확인한다. `staff_user.password_hash`에는 Argon2id 해시를 저장한다. 두 테이블은 PostgreSQL에서 소문자 이름으로 조회하며 연결의 `search_path`에서 보여야 한다. 대소문자를 무시했을 때 이메일이 중복되면 인증을 거부한다. 스키마 초기화는 Compose의 `init-db-topics` 서비스가 수행한다.

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

이메일은 `@` 앞에 영문·숫자 5~15자와 점이 포함된 도메인을 받는다. 등록 여부는 대소문자 구분 없이 확인한다. 미등록 이메일과 비밀번호 불일치에는 같은 실패 응답을 반환한다. 인증에 성공하면 8시간 유효한 HttpOnly 세션 쿠키를 발급한다. 세션은 같은 출처의 API 요청에 전달되고 로그아웃하면 폐기된다.

`GET /api/auth/me`로 세션을 확인하고 `POST /api/auth/logout`으로 종료한다. `GET /api/tickets`는 검색·복합 필터·접수 기간·정렬·페이지를 적용한 목록, `GET /api/tickets/options`는 회사별 필터 선택지, `GET /api/tickets/{ticket_uuid}`는 선택한 티켓과 고객 대화를 반환한다.

### 테스트

```powershell
& $python -m pip install -r src/pages/01-LoginPage/backend/requirements-test.txt
& $python -m unittest discover -s src/pages/01-LoginPage/backend/tests -v
```

PostgreSQL 통합 테스트를 포함하려면 스키마를 생성할 수 있는 테스트 전용 DB의 접속 정보를 `VOC_TEST_DATABASE_URL`에 넣고 같은 테스트 명령을 실행한다. 테스트가 생성한 스키마는 종료 시 제거한다.

## Docker 상시 VOC 수집

일반 Discord 문의와 VOC 라벨 메일을 계속 수집해 PostgreSQL Bronze와 Silver에 적재한다. [실행·검증 가이드](collector/README.md)

## Discord·Gmail 채널 수신 테스트

[실제 메시지 수신과 공통 분류 입력 검증](tools/channel_probe/README.md)
