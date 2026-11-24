package voc;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashSet;
import java.util.Set;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;

/** Build-time regression checks against the same UDF and libraries shipped to Flink. */
public class NormalizeMessageTest {
    private static final String ID = "f52b9455-1119-5212-91c1-21a84f0b23ed";
    private static final Set<String> FIELDS = Set.of(
        "schema_version", "event_id", "company_id", "channel", "connection_id", "source_message_id",
        "source_conversation_id", "source_channel_id", "source_channel_name", "reply_to_message_id",
        "message_kind", "sender_ref", "sender_display_name", "title", "body_text", "body_version",
        "occurred_at", "received_at", "source_metadata");

    private static ObjectNode event(String channel, String raw) throws Exception {
        var event = NormalizeMessage.JSON.createObjectNode().put("event_id", ID).put("company_id", "test")
            .put("channel", channel).put("connection_id", channel.equals("gmail") ? "desk@example.test" : "001")
            .put("source_message_id", "00Ab").put("received_at", "2026-11-24T17:00:01.123456789+09:00");
        event.set("raw", NormalizeMessage.JSON.readTree(raw));
        return event;
    }

    private static JsonNode output(ObjectNode event) throws Exception {
        return NormalizeMessage.JSON.readTree(new NormalizeMessage().eval(event.toString()));
    }

    private static void check(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }

    private static JsonNode accepted(ObjectNode event) throws Exception {
        var out = output(event);
        Set<String> fields = new HashSet<>();
        out.fieldNames().forEachRemaining(fields::add);
        check(fields.equals(FIELDS), "unexpected fields: " + fields);
        check(out.path("schema_version").asInt() == 4, "schema version");
        check(out.path("received_at").asText().equals("2026-11-24T08:00:01.123456Z"), "received_at precision/offset");
        return out;
    }

    private static void field(JsonNode out, String key, String expected) {
        check(expected == null ? out.path(key).isNull() : expected.equals(out.path(key).asText()), key);
    }

    private static void discord() throws Exception {
        ObjectNode event = event("discord", """
            {"id":"00Ab","channel_id":"02","content":"  text  ","author":{"id":"003","global_name":"Global","username":"user"},
             "member":{"nick":"Nickname"},"timestamp":"2026-11-24T08:00:02Z"}
            """);
        ObjectNode raw = (ObjectNode) event.path("raw");
        ObjectNode context = event.putObject("context").put("channel_id","02").put("channel_name","support");
        context.put("verified_message_kind", "customer_inquiry");
        var out = accepted(event);
        field(out, "source_conversation_id", "00Ab");
        field(out, "source_channel_name", "support");
        field(out, "sender_display_name", "Nickname");
        field(out, "message_kind", "unknown");
        field(out, "title", null);
        field(out, "occurred_at", "2026-11-24T08:00:02.000000Z");
        check(out.path("source_metadata").isObject() && out.path("source_metadata").isEmpty(), "Discord metadata");

        raw.remove("member");
        raw.remove("timestamp");
        event.put("verified_message_kind", "customer_inquiry").put("body_version", 2);
        raw.put("type", 19);
        var reference = raw.putObject("message_reference").put("message_id", "parent").put("channel_id","02");
        out = accepted(event);
        field(out, "message_kind", "customer_inquiry");
        field(out, "sender_display_name", "Global");
        field(out, "reply_to_message_id", "parent");
        field(out, "source_conversation_id", null);
        field(out, "occurred_at", null);
        check(out.path("body_version").asInt() == 2, "body_version from relay");

        event.put("known_conversation_root", "root");
        field(accepted(event), "source_conversation_id", "root");
        reference.remove("message_id");
        field(output(event), "error", "invalid Discord reply");
        reference.put("message_id", "00Ab");
        field(output(event), "error", "invalid Discord reply");
        reference.put("message_id", "parent");
        reference.put("channel_id", "other-room");
        field(accepted(event), "source_conversation_id", null);
        reference.put("channel_id", "02").put("guild_id", "other-guild");
        field(accepted(event), "source_conversation_id", null);
        context.put("is_thread", true);
        field(accepted(event), "source_conversation_id", "02");

        context.put("is_thread", false);
        raw.put("type", 0);
        reference.put("type", 1);
        out = accepted(event);
        field(out, "reply_to_message_id", null);
        field(out, "source_conversation_id", "00Ab");
        raw.put("webhook_id", "hook");
        field(accepted(event), "message_kind", "other");
        raw.remove("webhook_id");
        ((ObjectNode)raw.path("author")).put("bot", true);
        field(accepted(event), "message_kind", "other");
        var gateway = event.put("origin", "discord_gateway").putObject("raw").put("op", 0).put("t","MESSAGE_CREATE");
        gateway.set("d", raw);
        field(accepted(event), "body_text", "text");
    }

    private static void gmail() throws Exception {
        ObjectNode event = event("gmail", """
            {"id":"00Ab","threadId":"0ABC","internalDate":"1","labelIds":["INBOX","VOC"],"payload":{
             "mimeType":"multipart/alternative","headers":[
               {"name":"From","value":"=?UTF-8?B?7ZmN6ri464+Z?= <Sender@example.test>"},
               {"name":"To","value":"Team: \\"Doe, Jo\\" <jo@example.test>, other@example.test;"},
               {"name":"to","value":"<desk+voc@example.test>"},
               {"name":"Subject","value":"=?UTF-8?Q?Hello_=E2=9C=93?="}],
             "parts":[{"mimeType":"text/html","body":{"data":"PGI-b3RoZXI8L2I-"}},
                      {"mimeType":"text/plain","body":{"data":"cGxhaW4"}}]}}
            """);
        var out = accepted(event);
        field(out, "sender_ref", "Sender@example.test");
        field(out, "sender_display_name", "홍길동");
        field(out, "title", "Hello ✓");
        field(out, "source_conversation_id", "0ABC");
        field(out, "source_channel_id", null);
        field(out, "source_channel_name", null);
        field(out, "reply_to_message_id", null);
        field(out, "body_text", "plain");
        field(out, "occurred_at", "1970-01-01T00:00:00.001000Z");
        field(out, "message_kind", "unknown");
        check(out.at("/source_metadata/label_ids").size() == 2, "label ids");
        check(out.at("/source_metadata/to_addresses").equals(NormalizeMessage.JSON.readTree(
              "[\"jo@example.test\",\"other@example.test\",\"desk+voc@example.test\"]")), "RFC recipients and repeated headers");

        var raw = (ObjectNode) event.path("raw");
        raw.remove("internalDate");
        raw.putArray("labelIds").add("SENT");
        event.put("verified_message_kind", "customer_inquiry");
        out = accepted(event);
        field(out, "occurred_at", null);
        field(out, "message_kind", "staff_reply");
        raw.withArray("labelIds").add("DRAFT");
        field(accepted(event), "message_kind", "other");
        raw.remove("labelIds");
        var headers = ((ObjectNode) raw.path("payload")).putArray("headers");
        headers.addObject().put("name", "From").put("value", "Desk <DESK@example.test>");
        headers.addObject().put("name", "Auto-Submitted").put("value", "no; param=1");
        out = accepted(event);
        field(out, "title", null);
        field(out, "message_kind", "staff_reply");
        check(out.at("/source_metadata/to_addresses").isEmpty(), "absent recipients");
        headers.addObject().put("name", "Auto-Submitted").put("value", "auto-replied; owner=desk");
        field(accepted(event), "message_kind", "other");

        String html = "<div title='<style>'>line  1<style>.private {color:red}</style></div>"
            + "<!-- <style> --><table><tr><td>A</td><td>B</td></tr></table><script>hidden()</script>";
        raw.putObject("payload").put("mimeType", "text/html").putObject("body")
            .put("data", Base64.getUrlEncoder().encodeToString(html.getBytes(StandardCharsets.UTF_8)));
        field(accepted(event), "body_text", "line 1\nA\nB");
    }

    private static void review() throws Exception {
        ObjectNode event = event("discord", """
            {"id":"00Ab","channel_id":"02","content":"private text","timestamp":"private header"}
            """);
        var out = output(event);
        check(out.size() == 3 && out.path("schema_version").asInt() == 4, "redacted review");
        field(out, "event_id", ID);
        field(out, "error", "invalid timestamp");
        event.put("body_version", 0);
        field(output(event), "error", "invalid body_version");
        event.put("event_id", "private invalid identity");
        field(output(event), "event_id", null);
    }

    public static void main(String[] args) throws Exception {
        discord();
        gmail();
        review();
    }
}
