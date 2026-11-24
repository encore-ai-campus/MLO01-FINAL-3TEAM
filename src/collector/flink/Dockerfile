FROM flink:2.1.3-scala_2.12-java17 AS flinkbase
FROM eclipse-temurin:17-jdk AS build
COPY --from=flinkbase /opt/flink/lib /flink-libs
ADD --checksum=sha256:f48001755a4a591bb1b602965337fcf316d794c43783052f88ede05130539358 --chmod=0644 https://repo.maven.apache.org/maven2/org/eclipse/angus/jakarta.mail/2.0.5/jakarta.mail-2.0.5.jar /flink-libs/jakarta-mail.jar
ADD --checksum=sha256:c9db52100ce6c8aac95cc39075f95720d2e561b11f8051b81c121ad4effd7004 --chmod=0644 https://repo.maven.apache.org/maven2/jakarta/activation/jakarta.activation-api/2.1.4/jakarta.activation-api-2.1.4.jar /flink-libs/jakarta-activation.jar
ADD --checksum=sha256:11cbbcd1163c4fae0d23f9e2c55b4ff57a719a39fe96fc780001cd21a4e5ba15 --chmod=0644 https://repo.maven.apache.org/maven2/org/apache/flink/flink-sql-connector-kafka/5.0.0-2.1/flink-sql-connector-kafka-5.0.0-2.1.jar /flink-libs/flink-sql-connector-kafka.jar
WORKDIR /src
COPY *.java ./
RUN mkdir -p /classes && javac -encoding UTF-8 -cp '/flink-libs/*' -d /classes *.java && jar cf /app.jar -C /classes .
COPY test/NormalizeMessageTest.java test/
RUN javac -encoding UTF-8 -cp '/classes:/flink-libs/*' -d /tests test/NormalizeMessageTest.java && java -cp '/tests:/classes:/flink-libs/*' voc.NormalizeMessageTest

FROM flinkbase
RUN mkdir -p /opt/flink/usrlib /opt/flink/checkpoints && chown flink:flink /opt/flink/checkpoints
COPY --from=build /app.jar /opt/flink/usrlib/voc-standardizer.jar
COPY --from=build /flink-libs/jakarta-*.jar /flink-libs/flink-sql-connector-kafka.jar /opt/flink/lib/
