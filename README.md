# MLO01-FINAL-3TEAM

[VoC 모니터링 실행·모델 연결 가이드](monitoring/README.md)

- 팀명 : MLO01-FINAL-3TEAM
- 프로젝트 기간 : 26.11.13 \~ 27.01.07
- 프로젝트 주제 : VoC 분석 모델의 무중단 배포 및 실시간 모니터링 시스템
- 팀원 : 김민서, 신동엽, 안길찬, 이인건, 이형인

## 티켓 목록 실행

저장소 루트에서 수집기 설정과 Django 서명 키를 준비한다. 준비 명령은 기존 비밀번호·토큰·키를 재사용하며, 기존 설치에서 한 번 실행하면 필요한 Django 키를 추가한다. 새 환경은 [수집기 가이드](src/collector/README.md)에 따라 Gmail 인증도 진행한다.

```bash
python3 src/collector/prepare.py
docker compose up -d --build ticket-desk db-worker classify embed
```

브라우저에서 [전체 티켓](http://127.0.0.1:18082/tickets/) 또는 [내게 할당된 티켓](http://127.0.0.1:18082/tickets/?scope=mine)을 연다. Django가 로그인한 직원의 회사와 `ticket.read` 권한을 확인한 뒤 PostgreSQL 조회 결과를 HTML로 렌더링한다. 예전 화면 번호 북마크와 로컬 HTML 진입 링크도 지원한다.

사업자·직원·고객의 실제 매핑은 관리자가 등록한다. 등록한 수집 연결에서 `customer_inquiry`로 확인된 Silver 메시지가 들어오면 티켓번호와 첫 문의 메시지를 한 트랜잭션으로 생성한다. 수집 기록의 `message_kind`가 `unknown`이면 확인 전까지 업무 목록에서 대기한다. [회사·권한·채널 연결과 Silver 적재 가이드](src/collector/README.md)

분류 워커는 Stable API 주소를 설정하면 대기한 문의를 처리한다. 감성과 카테고리를 모두 저장하면 상담사에게 티켓을 공개한다.
세 번째 호출까지 실패한 티켓은 `분류 실패`로 공개한다. 임베딩 워커는 별도로 벡터를 저장한다. [팀 모델 연결](monitoring/README.md#팀-모델-연결)

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

## Django 화면과 소스 구조

| 위치 | 역할 |
| --- | --- |
| `src/backend/apps/accounts` | 로그인·로그아웃·직원 세션 |
| `src/backend/apps/preferences`, `assignment` | 신속답변·개인 화면·메신저·자동 배정 설정 |
| `src/backend/apps/tickets` | 티켓 목록·내 티켓·필터·상세·고객 대화 |
| `src/backend/apps/workspace`, `collaboration`, `analytics` | 업무 홈·받은 요청·VoC 분석의 시연 화면 |
| `src/backend/templates`, `src/backend/static` | 공통 레이아웃·탐색·정적 파일 |
| `src/collector`, `src/infra/channel_probe` | Discord·Gmail 수집, Flink 정제, PostgreSQL 적재 및 채널 점검 |

`src/workers/`와 `src/ml/`은 후속 비동기 추론·학습 구현 위치다. 데이터 규격과 기존 적재 코드는 [VoC 데이터 계약](src/contracts/README.md)에 정리했다.

업무 앱의 `presentation/`에서 요청과 폼을 검증하고, `services.py`에서 권한과 업무 규칙을, `repository.py`에서 데이터 조회를 담당한다. 앱별 `templates/`와 `static/`이 페이지와 브라우저 상호작용을 제공한다. 스키마는 기존 Compose 초기화 서비스가 관리한다.

티켓 목록·상세는 PostgreSQL에서 조회한다. 업무 홈·수신함·분석 화면의 예시 데이터는 화면에 시연용으로 표시한다. [화면 설계](docs/화면설계.md)

### 개발 환경

저장소 루트에서 가상환경을 만들고 실행한다. `DATABASE_URL`은 업무 스키마가 초기화된 PostgreSQL 접속 문자열이다. 로컬 예시는 macOS·Linux 셸 기준이다.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r src/backend/requirements.txt
export DATABASE_URL='postgresql://<user>:<password>@<host>:5432/<dbname>'
export DJANGO_SECRET_KEY="$(python -c 'from secrets import token_urlsafe; print(token_urlsafe(64))')"
python src/backend/manage.py collectstatic --noinput
python -m uvicorn config.asgi:application --app-dir src/backend --host 127.0.0.1 --port 8000
```

웹 화면은 동일 출처에서 Django CSRF 토큰을 포함한 폼을 전송한다. JSON 클라이언트는 먼저 `GET /login/`으로 받은 CSRF 쿠키를 보관하고, 로그인·로그아웃 POST에 현재 쿠키값을 `X-CSRFToken` 헤더로 보낸다. 로그인 성공 시 CSRF 토큰이 교체된다. 세션은 8시간 동안 유효하며 로그아웃하면 폐기된다.

### Task 9 설정

`/settings/`에서 신속답변과 개인 화면 설정을 연다. 회사 관리자는 직원별 업무 권한, 메신저 수신 채널과 자동 배정 정책을 관리한다. `init-db-topics`가 `settings_schema.sql`을 적용한다. 이미 PostgreSQL과 Kafka가 실행 중인 환경에서는 아래 명령으로 스키마를 적용하고 Django를 갱신한다.

```bash
docker compose build init-db-topics ticket-desk
docker compose run --rm --no-deps init-db-topics
docker compose up -d --no-deps --no-build ticket-desk
```

DB 운영자가 회사와 직원 이메일을 확인해 첫 관리자에게 `staff_user.role = 'admin'`을 지정한다. 대시보드 접근은 설정 화면에서 직원에게 부여한다.

Task 10은 배정 트랜잭션에서 `apps.assignment.services.get_policy(cursor, company_uuid, staff_uuid)`를 호출해 `enabled`, `batch_size`, `refill_mode`를 읽을 수 있다. 설정을 저장하기 전에는 자동 배정 꺼짐, 1건, 전체 소진 후 재배정이 기본값이다.

| 엔드포인트 | 용도 |
| --- | --- |
| `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` | 로그인·본인 확인·로그아웃 |
| `GET /api/tickets`, `GET /api/tickets/options` | 티켓 검색·복수 필터·접수 기간·정렬·페이지, 회사별 필터 선택지 |
| `GET /api/tickets/{ticket_uuid}` | 티켓과 고객 대화 이력 조회 |

### Task 8 팔로업 백엔드

`init-db-topics`가 `followup_schema.sql`을 적용한다. 기존 Docker 환경에서는 위의 빌드·초기화 명령을 다시 실행한다. API 변경 요청에는 로그인 세션과 `X-CSRFToken`이 필요하다.

| 엔드포인트 | 용도 |
| --- | --- |
| `GET /api/follow-up/tracks` | 내 추적 티켓 조회 |
| `POST /api/follow-up/tracks/{ticket_uuid}`, `DELETE /api/follow-up/tracks/{ticket_uuid}` | 내 추적 등록·해제 |
| `POST /api/tickets/{ticket_uuid}/memos` | 티켓 메모 등록. `body`를 전달한다. |
| `GET /api/follow-up/notifications` | 추적 이후 다른 상담사의 메모와 고객 후속 문의 목록 및 알림 건수 조회 |
| `GET /api/follow-up/contacts` | `state=scheduled\|completed`, `source=ticket\|chat`, `q`로 연락 기록 조회. 기본 상태는 `scheduled`이다. |
| `POST /api/follow-up/contacts` | `ticket_uuid` 또는 `chat_uuid`, `assignee_uuid`, `reason`, `scheduled_at`으로 연락 등록 |
| `GET /api/follow-up/contacts/{contact_uuid}` | 단일 연락 기록과 원본 링크 조회 |
| `POST /api/follow-up/contacts/{contact_uuid}/complete` | `contacted_at`과 `result`로 완료 처리. 실제 처리자는 로그인한 직원으로 기록한다. |

연락 시각은 ISO 8601 값을 사용한다. 연락 기록은 등록자와 지정 담당자가 조회할 수 있으며, 티켓·채팅 조회 권한을 각각 적용한다. 채팅 원본 링크는 1:1 상담과 메시지를 반환하는 `GET /api/chats/{chat_uuid}`로 연결된다. 종료된 Gmail 스레드의 후속 문의 알림 조건은 팀 합의가 필요하며, 현재는 원본 티켓이 답변완료 상태면 해당 고객 알림을 제외한다.

### 테스트

앞서 만든 가상환경에서 아래 명령을 실행한다. `DATABASE_URL`을 데이터베이스를 생성할 수 있는 **테스트 전용** 접속 문자열로 먼저 설정한다. 티켓 테스트는 임시 데이터베이스를 생성해 삭제하고, 로그인 조회 테스트는 임시 스키마를 만들어 정리한다.

```bash
export VOC_TICKET_TEST_ADMIN_DSN="$DATABASE_URL"
export VOC_TEST_DATABASE_URL="$DATABASE_URL"
export VOC_TASK9_TEST_DSN="$DATABASE_URL"
python src/backend/manage.py test tests apps.workspace
```

## Docker 상시 VOC 수집

일반 Discord 문의와 VOC 라벨 메일을 계속 수집해 PostgreSQL Bronze와 Silver에 적재한다. [실행·검증 가이드](src/collector/README.md)

## Discord·Gmail 채널 수신 테스트

[실제 메시지 수신과 공통 분류 입력 검증](src/infra/channel_probe/README.md)
