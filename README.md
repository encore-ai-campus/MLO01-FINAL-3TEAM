# MLO01-FINAL-3TEAM

- 팀명 : MLO01-FINAL-3TEAM
- 프로젝트 기간 : 26.11.13 \~ 27.01.07
- 프로젝트 주제 : VoC 분석 모델의 무중단 배포 및 실시간 모니터링 시스템
- 팀원 : 김민서, 신동엽, 안길찬, 이인건, 이형인

## 티켓 목록 실행

저장소 루트에서 수집기 설정과 Django 서명 키를 준비한다. 준비 명령은 기존 비밀번호·토큰·키를 재사용하며, 기존 설치에서 한 번 실행하면 필요한 Django 키를 추가한다. 새 환경은 [수집기 가이드](src/collector/README.md)에 따라 Gmail 인증도 진행한다.

```bash
python3 src/collector/prepare.py
docker compose build db-worker ticket-desk
docker compose up -d --no-build ticket-desk db-worker
```

브라우저에서 [전체 티켓](http://127.0.0.1:18082/tickets/) 또는 [내게 할당된 티켓](http://127.0.0.1:18082/tickets/?scope=mine)을 연다. Django가 로그인한 직원의 회사와 `ticket.read` 권한을 확인한 뒤 PostgreSQL 조회 결과를 HTML로 렌더링한다. 예전 화면 번호 북마크와 로컬 HTML 진입 링크도 지원한다.

사업자·직원·고객의 실제 매핑은 관리자가 등록한다. 등록한 수집 연결에서 `customer_inquiry`로 확인된 Silver 메시지가 들어오면 티켓번호와 첫 문의 메시지를 한 트랜잭션으로 생성한다. 수집 기록의 `message_kind`가 `unknown`이면 확인 전까지 업무 목록에서 대기한다. [회사·권한·채널 연결과 Silver 적재 가이드](src/collector/README.md)

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
| `src/backend/apps/followup` | 티켓 추적·후속 연락·알림 이력 |
| `src/backend/apps/workspace`, `collaboration`, `analytics` | 업무 홈·받은 요청·VoC 분석의 시연 화면 |
| `src/backend/templates`, `src/backend/static` | 공통 레이아웃·탐색·정적 파일 |
| `src/collector`, `src/infra/channel_probe` | Discord·Gmail 수집, Flink 정제, PostgreSQL 적재 및 채널 점검 |

`src/workers/classification/`은 Silver에서 파생된 분류 작업을 처리하고, `src/ml/serving/`은 실제 모델 추론 API를 제공한다. 고정 응답 모델은 카나리 시연의 fixture에서 실행한다. [VoC 데이터 계약](src/contracts/README.md) · [Mac에서 Kubernetes 카나리 시연하기](src/infra/canary-demo/README.md)

업무 앱의 `presentation/`에서 요청과 폼을 검증하고, `services.py`에서 권한과 업무 규칙을, `repository.py`에서 데이터 조회를 담당한다. 앱별 `templates/`와 `static/`이 페이지와 브라우저 상호작용을 제공한다. 스키마는 기존 Compose 초기화 서비스가 관리한다.

티켓 목록·상세는 PostgreSQL에서 조회한다. 업무 홈·수신함·분석 화면의 예시 데이터는 화면에 시연용으로 표시한다. [화면 설계](docs/화면설계.md)

### 티켓 상세의 AI 요약

상담사가 배정된 티켓에서 상담 권한이 있는 직원이 답변 작성란 위의 **AI 요약**을 선택하면 Django가 고객 문의 본문을 로컬 Ollama에 보낸다. 요약은 해당 화면에만 표시된다. Mac에서 Ollama를 실행하고 저장소 루트에서 아래 명령으로 모델을 한 번 등록한다.

```sh
ollama create voc-ticket-summary:qwen2.5-1.5b -f src/ml/ticket-summary/Modelfile
```

Mac에서 직접 실행한 Django는 기본적으로 `http://127.0.0.1:11434`에 연결한다. Docker Desktop의 Compose 및 Mac 시연 배포에서는 `http://host.docker.internal:11434`를 쓴다. 다른 구성에서는 웹 서비스의 `VOC_OLLAMA_URL`로 주소를 설정한다.

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
| `GET /api/tickets/{ticket_uuid}/quick-replies`, `GET /api/tickets/{ticket_uuid}/collaboration-options` | 현재 카테고리 신속답변과 이관·검토 선택지 |
| `POST /api/tickets/{ticket_uuid}/messages`, `POST /api/tickets/{ticket_uuid}/transfer`, `POST /api/tickets/{ticket_uuid}/review-requests` | 고객 답변·내부 메모와 상담 협업 |
| `GET /api/ticket-requests?kind=transfer` 또는 `?kind=review` | 로그인한 직원이 받은 티켓 요청 |
| `GET /api/ticket-requests/{action_uuid}/attachment` | 수신 직원의 개별 참고 자료 다운로드 |

### 티켓 상세·답변 설정

티켓 목록에서 상세로 이동할 때 기존 티켓 UUID를 사용한다. 상담 기능에는 `ticket.read`와 설정 화면의 `ticket.handle` 권한이 모두 필요하다. 기존 Compose 설치에서는 `docker compose build init-db-topics ticket-desk` 후 초기화 서비스로 `src/collector/ticket_schema.sql`을 적용하고, 새 이미지로 `ticket-desk`를 띄운다. 검토 요청을 받는 사내 채널과 직원은 `ticket_review_channel`, `ticket_review_channel_member`에 등록한다. 수신함은 `collab_request`를 사용하고 신속답변은 설정 화면의 `quick_reply`를 사용한다.

발송 자격 증명은 배포 환경에서 `TASK3_REPLY_CONNECTIONS` JSON으로 주입한다. 키는 회사 UUID → `gmail` 또는 `discord` → 기존 Silver `connection_id`다. Gmail 사서함에는 `client_id`, `client_secret`, 발송 범위가 있는 `refresh_token`을 설정한다. Discord 연결에는 답글 권한이 있는 `bot_token`을 설정한다. 수집용 Gmail 읽기 토큰은 계속 수집에 사용한다. 회신 경로는 티켓에 연결된 Bronze·Silver에서 가져온다.

JSON 메시지 요청에는 `message_type` (`reply` 또는 `memo`), `body`, 동작마다 생성한 UUID인 `request_key`를 보낸다. 재호출에는 같은 키를 쓴다. 응답의 `message.delivery_status`가 `sent`면 답변완료, `failed`면 기존 상태 유지, `unknown` 또는 `sending`이면 중복 발송 방지를 위해 채널에서 전송 여부를 확인할 때까지 추가 발송을 막는다. CSV 이관을 적재할 때는 `ticket.csv_import_kind='migration'`과 원문 메시지를 연결하면 답변 처리 결과가 `internal_only`로 저장된다. 홈페이지 CSV 샘플은 별도 값인 `homepage_sample`로 구분한다. 두 CSV 유형의 티켓 번호는 기존 수집 티켓과 같은 회사별 카운터에서 할당된다.

### Task 8 팔로업

공통 사이드바에서 `/follow-up/`을 연다. Django가 팔로업 화면을 렌더링하고 `collectstatic`으로 CSS와 JavaScript를 수집한다.

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

### 모델 추론 및 티켓 배정

M5의 `linux/arm64` CPU용 이미지, Stable·Canary 모델 설정과 `/v1/predict` 호출 규격은 [모델 서버 실행 가이드](src/ml/serving/README.md)에 있습니다. 추론 서버는 티켓 DB에 접근하지 않습니다.

기존 설치에서는 `init-db-topics`로 `ticket_schema.sql`을 다시 적용하고 `ticket-desk`를 갱신합니다. 관리자가 `/settings/assignment/category-routes/`에서 회사의 카테고리별 담당 부서를 등록합니다. 직원별 자동 배정은 기존 `/settings/assignment/`의 활성화 여부, 배정 건수, 보충 시점을 사용합니다. 상담 담당자는 티켓 상세의 카테고리 변경 화면에서 분류를 수정할 수 있습니다.

분류 워커는 카테고리 응답의 업무 코드를 먼저 `category_uuid`로 변환합니다. 카테고리 결과는 `POST /api/internal/ticket-assignments/category-result`에 `company_uuid`, `event_id`, `preprocess_version`, `body_version`, `category_uuid`를 보내 적용합니다. 판단을 보류하면 `category_uuid`에 JSON `null`을 보냅니다. 분류 결과 저장과 배정을 한 트랜잭션으로 처리하므로 카테고리 열을 별도로 갱신하는 처리는 이 호출로 교체합니다. 감성 결과는 워커가 기존 계약대로 저장합니다.

정상 응답의 `outcome`은 `applied` 또는 `review_required`입니다. 같은 작업 버전과 카테고리를 재전송하면 `already_applied`를 반환합니다. 이전 버전이나 수동 수정된 카테고리에는 각각 `stale_result`, `manual_preserved`를 반환합니다. 같은 작업 버전에 이미 적용한 값과 다른 카테고리가 들어오면 `409`를 반환합니다. 워커가 `running`이나 `failed` 상태를 따로 기록할 때는 현재 작업 버전과 `category_manually_set=false` 조건을 지킵니다.

`ticket-desk`와 분류 워커에 같은 `VOC_ASSIGNMENT_WORKER_TOKEN`을 주입하고, 워커는 `Authorization: Bearer <token>` 헤더로 호출합니다. Compose 내부 주소는 `http://ticket-desk:8000`입니다. 별도 서비스 이름을 쓰는 환경은 그 이름을 `DJANGO_ALLOWED_HOSTS`에 포함합니다.

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
