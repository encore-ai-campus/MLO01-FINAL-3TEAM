# VoC 데이터 계약

Flink와 PostgreSQL 워커가 사용하는 Silver v4 필드와 Discord·Gmail 예시는 [수집기 가이드](../collector/README.md)에 있다. [수집 스키마](../collector/schema.sql)는 버전별 메시지와 재처리 이력을 정의하고, [업무 스키마](../collector/ticket_schema.sql)는 티켓, 모델 입력 버전과 분석 요청의 키를 정의한다.

[통합 테스트](../collector/test_silver_v4_integration.py)는 동일한 합성 메시지를 Kafka와 PostgreSQL에서 읽어 Silver 필드와 값을 비교한다.
