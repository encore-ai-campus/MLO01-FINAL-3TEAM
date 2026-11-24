package voc;

import java.time.Instant;
import org.apache.flink.table.functions.ScalarFunction;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;

/** Normalize an immutable Bronze envelope. A business error produces a review record. */
public class NormalizeMessage extends ScalarFunction {
    static final ObjectMapper JSON = new ObjectMapper();

    static String text(JsonNode node, String key) {
        return node.path(key).asText("");
    }

    private static String required(JsonNode node, String key) {
        String value = text(node, key);
        if (value.isBlank()) throw new IllegalArgumentException("missing " + key);
        return value;
    }

    static void optional(ObjectNode out, String key, String value) {
        if (value == null || value.isBlank()) out.putNull(key);
        else out.put(key, value);
    }

    public String eval(String payload) {
        ObjectNode out = JSON.createObjectNode();
        try {
            JsonNode event = JSON.readTree(payload);
            for (String key : new String[]{"event_id","company_id","channel","connection_id","source_message_id","received_at"})
                out.put(key, required(event,key));
            out.put("schema_version", 1);
            out.put("raw_ref", required(event, "event_id"));
            JsonNode raw = event.path("raw");
            switch (required(event, "channel")) {
                case "discord" -> discord(out, raw, event);
                case "gmail" -> gmail(out, raw);
                default -> throw new IllegalArgumentException("unsupported channel");
            }
            if (text(out,"body_text").isBlank()) throw new IllegalArgumentException("empty text body");
        } catch (IllegalArgumentException ex) {
            // Only allow our fixed validation messages onto the review topic.
            String reason = ex.getMessage();
            if (reason == null || !reason.matches("(missing [a-z_]+|unsupported channel|empty text body|source ID mismatch|invalid MIME text|detached text part|invalid timestamp|invalid Discord event)"))
                reason = "invalid field";
            out.put("error", reason);
        } catch (Exception ex) {
            out.put("error","invalid source format");
        }
        return out.toString();
    }

    private static void discord(ObjectNode out, JsonNode raw, JsonNode event) {
        boolean gateway = text(event,"origin").equals("discord_gateway");
        if (gateway && (!text(raw,"t").equals("MESSAGE_CREATE") || raw.path("op").asInt(-1) != 0))
            throw new IllegalArgumentException("invalid Discord event");
        JsonNode message = gateway ? raw.path("d") : raw;
        String id = required(message,"id");
        if (!id.equals(text(out,"source_message_id"))) throw new IllegalArgumentException("source ID mismatch");
        String room = required(message,"channel_id");
        boolean thread = event.path("context").path("is_thread").asBoolean(false);
        out.put("source_conversation_id", thread ? room : id);
        optional(out, "reply_to_message_id", text(message.path("message_reference"), "message_id"));
        optional(out, "sender_ref", text(message.path("author"),"id"));
        out.put("title", "");
        out.put("body_text", text(message,"content").strip());
        out.put("occurred_at", iso(required(message,"timestamp")));
    }

    private static String iso(String timestamp) {
        try { return Instant.parse(timestamp).toString(); }
        catch (RuntimeException ex) { throw new IllegalArgumentException("invalid timestamp"); }
    }

    private static void gmail(ObjectNode out, JsonNode raw) {
        if (!required(raw,"id").equals(text(out,"source_message_id"))) throw new IllegalArgumentException("source ID mismatch");
        out.put("source_conversation_id", required(raw,"threadId"));
        out.putNull("reply_to_message_id");
        JsonNode part = raw.path("payload");
        String sender = MailBody.header(part,"From").strip();
        int left = sender.lastIndexOf('<'), right = sender.indexOf('>', left + 1);
        optional(out, "sender_ref", left >= 0 && right > left ? sender.substring(left+1, right) : sender);
        out.put("title", MailBody.header(part,"Subject").strip());
        out.put("body_text", MailBody.extract(part).text());
        try { out.put("occurred_at", Instant.ofEpochMilli(Long.parseLong(required(raw,"internalDate"))).toString()); }
        catch (RuntimeException ex) { throw new IllegalArgumentException("invalid timestamp"); }
    }
}
