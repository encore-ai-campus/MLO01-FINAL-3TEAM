# Discord·Gmail 실제 메시지 포맷 테스트

테스트 문장 한 개를 Discord와 Gmail에서 각각 받아 원본 JSON, 공통 메시지 레코드, 분류기 입력을 비교한다. 한 RUN은 한 회사의 독립된 첫 문의를 각 채널에서 한 건씩 비교하는 단위다. 같은 문의를 두 채널에 보내도 두 티켓으로 보존한다.

| 실험 | 필요한 접근 | 캡처하는 데이터 | 성공을 확인할 위치 |
| --- | --- | --- | --- |
| Discord 웹훅 게시 | 테스트 채널의 Incoming Webhook URL | 직접 보낸 요청과 `wait=true`로 Discord가 반환한 Message 객체 | Discord 채널 화면과 `discord-webhook/raw.json` |
| Discord 고객 메시지 수신 | 테스트 서버에 설치한 봇, Bot Token, 채널 ID | 사람이 보낸 Gateway `MESSAGE_CREATE` 전체 JSON. 실제 메시지는 `d` 아래에 있다. | `discord/raw.json` |
| Gmail 수신 | Gmail API가 활성화된 프로젝트, Desktop OAuth 클라이언트, 테스트 사서함 승인 | `users.messages.get(format=full)`의 Message 객체. 제목과 본문은 `payload` 아래에 있다. | `gmail/raw.json` |

## 1. 로컬 환경과 공통 표식 준비

Mac의 zsh에서 아래 명령을 실행한다. 새 체크아웃에서는 Python 3.10 이상으로 가상환경을 한 번 생성한다. 이미 `.venv-channel`이 준비된 PC에서는 활성화부터 시작한다.

```bash
cd /Users/2eelogan/project/mlo-01-final-team3
git switch feature/ticket_list
python3 -m venv .venv-channel
source .venv-channel/bin/activate
python -m pip install -r tools/channel_probe/requirements.txt
mkdir -p .local/channel-probe
```

아래 표식은 이번 테스트 동안 한 번만 만든다. 같은 터미널에서 Discord → Gmail 순서로 실행하면 값이 유지된다. 다른 터미널을 쓸 때는 출력된 RUN을 그대로 복사한다.

```bash
RUN="VOC-$(date -u +%Y%m%dT%H%M%SZ)"
echo "$RUN"
```

## 2. 지금 보유한 Discord 웹훅 URL로 게시 확인

1. 웹훅이 연결된 테스트 채널을 Discord에서 연다. 다음 명령을 실행하면 이 채널에 테스트 메시지 한 건이 실제로 게시된다.
2. 터미널이 `DISCORD_WEBHOOK_URL`을 물으면 URL을 붙여넣고 Enter를 누른다. 입력 중 문자는 화면에 표시되지 않는다.

```bash
python -m tools.channel_probe discord-webhook --run "$RUN"
```

3. 채널에 `[RUN값] 결제 버튼을 누르면 오류가 납니다.`가 **웹훅 이름으로** 올라왔는지 확인한다. `request.json`은 발송 전 형식이고 `raw.json`은 Discord가 ID·채널·시각을 부여한 게시 결과다.

```bash
jq '{content,allowed_mentions}' ".local/channel-probe/runs/$RUN/discord-webhook/request.json"
jq '{id,channel_id,webhook_id,timestamp,content}' ".local/channel-probe/runs/$RUN/discord-webhook/raw.json"
```

4. 여기까지가 웹훅 URL로 확인할 수 있는 범위다. 고객이 Discord에 직접 작성한 글을 수신하려면 다음 단계로 진행한다. 게시 결과는 별도의 폴더에 두며 최종 수신 검증에는 포함하지 않는다.

## 3. 실제 Discord 고객 메시지 한 건 받기

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 테스트용 애플리케이션을 선택하거나 만든다. **Bot → Privileged Gateway Intents → Message Content Intent**를 켜고 저장한다. 같은 **Bot** 화면에서 Bot Token을 준비한다. 이미 팀에서 운영하는 봇이 있다면 먼저 해당 봇 담당자와 확인한다.
2. **Installation → Guild Install**에서 `bot` 스코프와 `View Channels` 권한을 지정한다. 제공되는 설치 링크를 열어 테스트 서버에 설치한다. 서버 설치 권한이 없다면 서버 관리자에게 봇 설치와 해당 채널 열람 권한을 요청한다. 이 수신 실험은 봇에게 메시지 발송이나 관리자 권한을 부여할 필요가 없다.
3. Discord 앱의 **사용자 설정 → 고급 → 개발자 모드**를 켠다. 테스트 텍스트 채널을 우클릭해 **채널 ID 복사**를 선택한다. 문의 스레드에서 시험할 경우에는 봇이 볼 수 있는 해당 스레드의 ID를 사용한다.
4. 아래에서 숫자를 복사한 ID로 바꾼다. 토큰 입력 요청에는 **Bot Token**을 넣는다. 명령은 기본 120초 동안 기다리며 `--timeout 300`처럼 바꿀 수 있다.

```bash
CHANNEL_ID="복사한_숫자_채널_ID"
python -m tools.channel_probe discord-receive --run "$RUN" --channel-id "$CHANNEL_ID"
```

5. 터미널에 `READY`가 뜨면 **표시된 Discord 채널 링크**를 연다. 그 아래 문장 한 줄을 대괄호까지 복사해 **본인 Discord 계정**으로 그 채널의 새 메시지로 보낸다. 예를 들어 RUN이 `VOC-test01`이면 `[VOC-test01] 결제 버튼을 누르면 오류가 납니다.`를 보낸다. 수신기는 일치한 한 건을 저장하고 종료한다. 지정 채널에서 표식이 다른 글, 텍스트가 비어 있는 글, 봇·웹훅 글이 도착하면 터미널이 사유별로 한 번씩 `건너뜀`을 표시한다.
6. 원본에서 `t`가 `MESSAGE_CREATE`, `d.channel_id`가 지정 채널, `d.content`가 보낸 문장인지 확인한다.

```bash
jq '{event:.t, message_id:.d.id, channel:.d.channel_id, author:.d.author.id, text:.d.content}' ".local/channel-probe/runs/$RUN/discord/raw.json"
```

## 4. Gmail 받은편지함에서 실제 메일 한 통 받기

1. [Google Cloud Console](https://console.cloud.google.com/)에서 테스트용 프로젝트를 선택하고 **API 및 서비스 → 라이브러리 → Gmail API → 사용**을 선택한다.
2. **Google Auth Platform → Branding / Audience**에서 OAuth 동의를 구성한다. 개인 Gmail을 사용할 때는 **External**로 설정하고 테스트 상태의 **Test users**에 메일을 받을 계정을 추가한다. 조직 내부 앱은 조직 정책에 맞는 계정을 사용한다. **Data Access**에서 Gmail 메일 읽기 `https://www.googleapis.com/auth/gmail.readonly`를 선택한다.
3. **Clients → Create client → Desktop app**을 선택하고 클라이언트 JSON을 내려받는다. Finder에서 이름을 `credentials.json`으로 바꿔 아래 명령의 대상 폴더에 넣는다. CLI는 이 위치에서 Desktop app 파일을 읽는다.

```bash
mkdir -p .local/channel-probe
open .local/channel-probe
```

4. 다음 명령을 실행한다. 첫 실행에는 브라우저에서 **테스트 수신 사서함**으로 로그인하고 읽기 권한을 승인한다. OAuth 응답은 Mac의 `127.0.0.1` 임시 포트로 돌아온다. 이후에는 같은 폴더의 `token.json`으로 승인을 재사용한다.

```bash
python -m tools.channel_probe gmail --run "$RUN" --timeout 180
```

5. 다른 메일 계정에서 승인한 테스트 사서함으로 아래와 같이 **새 메일 한 통**을 보낸다. 제목에는 터미널이 출력한 실제 RUN을 대괄호까지 넣는다. 본문은 Discord 문장과 정확히 같게 쓰고 자동 서명은 뺀다. 이미 받은편지함에 도착했다면 그 뒤에 명령을 실행해도 된다.

| 항목 | 예시 (`RUN=VOC-test01`) |
| --- | --- |
| 수신자 | 4단계에서 승인한 테스트 Gmail 주소 |
| 제목 | `[VOC-test01]` |
| 본문 | `결제 버튼을 누르면 오류가 납니다.` |

6. 도구는 받은편지함에서 RUN을 검색하고 제목의 정확한 표식을 확인한 뒤 일치하는 메일의 `format=full` JSON을 받는다. 이번 경로에는 외부 웹훅 서버나 Google Cloud Pub/Sub 구성이 필요하지 않다.

```bash
jq '{id,threadId,labelIds,mimeType:.payload.mimeType,parts:[.payload.parts[]?.mimeType]}' ".local/channel-probe/runs/$RUN/gmail/raw.json"
```

## 5. 파일과 합격 기준 확인

두 채널을 같은 RUN으로 수신한 뒤 실행한다. `jq`가 없는 PC에서는 `python -m json.tool --no-ensure-ascii 파일경로`로 파일을 열 수 있다.

```bash
DIR=".local/channel-probe/runs/$RUN"
python -m tools.channel_probe verify --run "$RUN" --expect "결제 버튼을 누르면 오류가 납니다."
jq . "$DIR/discord/classifier-input.json" "$DIR/gmail/classifier-input.json"
```

| 파일 | 확인 항목 |
| --- | --- |
| `raw.json` | 제공자가 준 JSON 전체. Discord의 `op/t/s/d` 구조와 Gmail의 `id/threadId/payload` 구조를 직접 비교한다. |
| `capture.json` | 수신 방법, 연결 식별자, 수신 시각과 RUN. |
| `standard.json` | 두 채널이 같은 키를 쓴다. 원본 메시지·대화 ID, 발신자, UTC 시각, 제목·본문, 티켓 번호 및 원본 참조를 확인한다. |
| `classifier-input.json` | 네 키 `schema_version`, `ticket_id`, `body_version`, `analysis_text`. 숫자·문자열의 자료형도 두 채널에서 일치해야 한다. |

`verify`는 저장된 원본으로 결과를 다시 계산하고, 다른 채널의 티켓 번호가 서로 다른지 확인한다. 위 명령에서 **PASS 두 줄과 종료 코드 0**이면 수신·표준화 포맷 테스트가 통과한 것이다. 서로 다른 문장으로 포맷만 점검할 때는 `--expect`를 생략한다. `[RUN]`은 시험용 표식이므로 모델 텍스트에서 제거된다. 실제 OAuth 파일, 토큰, 원본, 결과는 Git에서 제외한 `.local/` 안에 보관한다.

## 6. 결과가 비었을 때

| 증상 | 확인할 내용 |
| --- | --- |
| 웹훅 HTTP 401·404, 시간 초과 | 채널에 연결된 최신 URL인지 확인한다. 응답을 잃어도 이미 게시됐을 수 있으니 채널에서 먼저 확인하고, 재시험에는 새 RUN을 쓴다. |
| Discord에서 `건너뜀` 표시 | 지정 채널의 새 글 이벤트는 도착했다. 표시된 사유를 읽고, 본인 계정으로 `[RUN] 본문`을 새 글로 보낸다. |
| Discord에서 `새 메시지 이벤트가 0건` 표시 | 수신기를 다시 실행하고 `READY`의 채널 링크에서 새 메시지를 보낸다. 스레드에 보내려면 해당 스레드 ID로 실행한다. |
| Gmail `403` 또는 `access_denied` | Gmail API, 테스트 사용자 등록, 승인한 계정 및 조직의 OAuth 정책을 확인한다. 계정을 바꾸려면 로컬 `token.json`을 별도로 옮기고 다시 승인한다. |
| Gmail 조회 0건 | 스팸함·보낸편지함이 아닌 테스트 받은편지함에 있는지, 제목 맨 앞의 대괄호와 RUN이 일치하는지 확인한다. |
| 본문 불일치 | 자동 서명·이전 메일 인용·추가 제목을 확인한다. 일반 텍스트와 HTML이 함께 있으면 일반 텍스트를 한 번 쓰고, HTML만 있으면 텍스트로 변환한다. 본문 누락·분리 저장된 텍스트 파트는 `review-…/raw.json`에 보관한다. |

## 프로토타입에 연결할 때

이번 시험은 각 채널의 새 문의 한 건과 `body_version=1`을 검증한다. 일반 채널의 후속 답글 연결, 중복 이벤트의 영속 처리, 본문 버전 증가는 Silver 티켓 적재 단계에서 구현한다. Discord는 현재 `MESSAGE_CREATE` 소비 경로를 지속 실행 수집기에 연결할 수 있다. Gmail 실시간 수신은 `watch`와 Pub/Sub 알림을 받은 뒤 저장된 커서로 `history.list`를 조회하고 해당 메일의 `messages.get(format=full)`을 가져오면 같은 변환 규칙을 쓸 수 있다.

원본은 계획한 `voc.raw.discord`와 `voc.raw.gmail`로 발행하고, Bronze 적재 확인 후 표준화와 Silver 적재를 거쳐 공통 분류 요청을 만든다. 이 도구가 검증하는 범위는 수신 원본과 분류 입력까지다. Kafka·Flink·모델 추론의 실행 검증은 통합 단계에서 한다.

자격 증명 없이 변환과 수신 필터를 점검하는 자동 테스트도 있다. 아래 명령의 성공은 외부 계정으로 실행한 `verify` 결과와 구분한다.

```bash
python -m unittest -v tools.channel_probe.test_probe
```
