FROM flink:2.1.3-scala_2.12-java17 AS flinkbase
FROM eclipse-temurin:17-jdk AS build
COPY --from=flinkbase /opt/flink/lib /flink-libs
WORKDIR /src
COPY *.java ./
RUN mkdir -p /classes && javac -encoding UTF-8 -cp '/flink-libs/*' -d /classes *.java && jar cf /app.jar -C /classes .

FROM flinkbase
ADD --checksum=sha256:11cbbcd1163c4fae0d23f9e2c55b4ff57a719a39fe96fc780001cd21a4e5ba15 --chmod=0644 https://repo.maven.apache.org/maven2/org/apache/flink/flink-sql-connector-kafka/5.0.0-2.1/flink-sql-connector-kafka-5.0.0-2.1.jar /opt/flink/lib/flink-sql-connector-kafka.jar
RUN mkdir -p /opt/flink/usrlib /opt/flink/checkpoints && chown flink:flink /opt/flink/checkpoints
COPY --from=build /app.jar /opt/flink/usrlib/voc-standardizer.jar
