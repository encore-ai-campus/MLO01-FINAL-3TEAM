package voc;

import java.time.DateTimeException;
import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeFormatterBuilder;
import java.util.Set;
import java.util.UUID;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.flink.table.functions.ScalarFunction;

/** Map one immutable Bronze message to the 19 Silver v4 fields or a redacted review record. */
public class NormalizeMessage extends ScalarFunction {
    static final ObjectMapper JSON = new ObjectMapper();
    static final int VERSION = 4;
    private static final DateTimeFormatter UTC_MICROS = new DateTimeFormatterBuilder().appendInstant(6).toFormatter();
    private static final Set<String> KINDS = Set.of("customer_inquiry", "staff_reply", "other", "unknown");

    /** Only our fixed field names and validation reasons may go on the review stream. */
    static class InvalidMessage extends IllegalArgumentException {
        InvalidMessage(String reason) { super(reason); }
    }

    static String text(JsonNode node, String key) {
        JsonNode value = node.path(key);
        return value.isTextual() ? value.textValue() : "";
    }

    private static String required(JsonNode node, String key) {
        String value = node.path(key).textValue();
        if (value == null || value.isBlank()) throw new InvalidMessage("missing " + key);
        return value;
    }

    static void optional(ObjectNode out, String key, String value) {
        if (value == null || value.isBlank()) out.putNull(key);
        else out.put(key, value);
    }

    public String eval(String payload) {
        String eventId = null;
        try {
            JsonNode event = JSON.readTree(payload);
            String id = required(event, "event_id");
            try {
                if (!UUID.fromString(id).toString().equalsIgnoreCase(id)) throw new IllegalArgumentException();
            } catch (IllegalArgumentException ex) {
                throw new InvalidMessage("invalid event_id");
            }
            eventId = id;
            ObjectNode out = JSON.createObjectNode().put("schema_version", VERSION).put("event_id", id);
            for (String key : new String[]{"company_id", "channel", "connection_id", "source_message_id"})
                out.put(key, required(event, key));
            out.put("received_at", timestamp(required(event, "received_at"), false));

            JsonNode version = event.get("body_version");
            if (version != null && (!version.isIntegralNumber() || !version.canConvertToInt() || version.intValue() < 1))
                throw new InvalidMessage("invalid body_version");
            out.put("body_version", version == null ? 1 : version.intValue());
            String verifiedKind = event.hasNonNull("verified_message_kind") ? required(event, "verified_message_kind") : "unknown";
            if (!KINDS.contains(verifiedKind)) throw new InvalidMessage("invalid message kind");

            JsonNode raw = event.path("raw");
            switch (text(out, "channel")) {
                case "discord" -> discord(out, raw, event, verifiedKind);
                case "gmail" -> gmail(out, raw, verifiedKind);
                default -> throw new InvalidMessage("unsupported channel");
            }
            if (text(out, "body_text").isBlank()) throw new InvalidMessage("empty text body");
            return out.toString();
        } catch (InvalidMessage ex) {
            return review(eventId, ex.getMessage());
        } catch (Exception ex) {
            return review(eventId, "invalid source format");
        }
    }

    private static String review(String eventId, String reason) {
        return JSON.createObjectNode().put("event_id", eventId).put("schema_version", VERSION).put("error", reason).toString();
    }

    private static String timestamp(String value, boolean epochMillis) {
        try {
            Instant instant = epochMillis ? Instant.ofEpochMilli(Long.parseLong(value)) : Instant.parse(value);
            String formatted = UTC_MICROS.format(instant);
            if (formatted.charAt(4) != '-' || formatted.startsWith("0000")) throw new DateTimeException("outside date range");
            return formatted;
        } catch (DateTimeException | NumberFormatException ex) {
            throw new InvalidMessage("invalid timestamp");
        }
    }

    private static void occurredAt(ObjectNode out, JsonNode message, String key, boolean epochMillis) {
        if (message.hasNonNull(key)) out.put("occurred_at", timestamp(message.path(key).asText(""), epochMillis));
        else out.putNull("occurred_at");
    }

    private static void checkId(ObjectNode out, JsonNode message) {
        if (!required(message, "id").equals(text(out, "source_message_id"))) throw new InvalidMessage("source ID mismatch");
    }

    private static void discord(ObjectNode out, JsonNode raw, JsonNode event, String verifiedKind) {
        boolean gateway = text(event, "origin").equals("discord_gateway");
        if (gateway && (!text(raw, "t").equals("MESSAGE_CREATE") || raw.path("op").asInt(-1) != 0))
            throw new InvalidMessage("invalid Discord event");
        JsonNode message = gateway ? raw.path("d") : raw;
        checkId(out, message);
        String id = text(out, "source_message_id");
        String room = required(message, "channel_id");
        String guild = text(out, "connection_id");
        JsonNode context = event.path("context");
        if (!matchesIfPresent(context, "channel_id", room) || !matchesIfPresent(message, "guild_id", guild))
            throw new InvalidMessage("Discord scope mismatch");
        out.put("source_channel_id", room);
        optional(out, "source_channel_name", context.path("channel_name").textValue());

        JsonNode messageType = message.path("type");
        if (message.hasNonNull("type") && (!messageType.isIntegralNumber() || !messageType.canConvertToInt()))
            throw new InvalidMessage("invalid Discord event");
        int type = messageType.asInt(0);
        JsonNode reference = message.path("message_reference");
        JsonNode refType = reference.path("type");
        boolean defaultReference = !reference.hasNonNull("type") || (refType.isIntegralNumber() && refType.canConvertToInt() && refType.intValue() == 0);
        // A crosspost, pin or forwarded message can carry a reference without being a reply.
        boolean reply = type == 19 || (!message.hasNonNull("type") && reference.hasNonNull("message_id") && defaultReference);
        String parent = reply && defaultReference ? reference.path("message_id").textValue() : null;
        boolean thread = context.path("is_thread").asBoolean(false);
        if (id.equals(parent) || (reply && !thread && (parent == null || parent.isBlank())))
            throw new InvalidMessage("invalid Discord reply");
        optional(out, "reply_to_message_id", parent);
        if (thread) out.put("source_conversation_id", room);
        else if (!reply) out.put("source_conversation_id", id);
        else {
            // known_conversation_root is supplied by the relay after a scoped lookup of the direct parent.
            boolean sameRoom = matchesIfPresent(reference, "channel_id", room) && matchesIfPresent(reference, "guild_id", guild);
            String root = sameRoom ? event.path("known_conversation_root").textValue() : null;
            if (id.equals(root)) throw new InvalidMessage("invalid Discord reply");
            optional(out, "source_conversation_id", root);
        }

        JsonNode author = message.path("author");
        optional(out, "sender_ref", author.path("id").textValue());
        String name = text(message.path("member"), "nick");
        if (name.isBlank()) name = text(author, "global_name");
        if (name.isBlank()) name = text(author, "username");
        optional(out, "sender_display_name", name);
        boolean automated = author.path("bot").asBoolean() || author.path("system").asBoolean()
            || !text(message, "webhook_id").isBlank() || (type != 0 && type != 19);
        out.put("message_kind", automated ? "other" : verifiedKind);
        out.putNull("title");
        out.put("body_text", text(message, "content").strip());
        occurredAt(out, message, "timestamp", false);
        out.putObject("source_metadata");
    }

    private static boolean matchesIfPresent(JsonNode node, String key, String expected) {
        return !node.hasNonNull(key) || expected.equals(node.path(key).textValue());
    }

    private static void gmail(ObjectNode out, JsonNode raw, String verifiedKind) {
        checkId(out, raw);
        out.put("source_conversation_id", required(raw, "threadId"));
        out.putNull("source_channel_id");
        out.putNull("source_channel_name");
        out.putNull("reply_to_message_id");
        JsonNode part = raw.path("payload");
        var senders = MailBody.addresses(part, "From");
        var sender = senders.size() == 1 ? senders.get(0) : null;
        optional(out, "sender_ref", sender == null ? null : sender.getAddress());
        optional(out, "sender_display_name", sender == null ? null : sender.getPersonal());
        optional(out, "title", MailBody.subject(part));
        out.put("body_text", MailBody.extract(part).text());
        occurredAt(out, raw, "internalDate", true);

        ObjectNode metadata = out.putObject("source_metadata");
        var labels = metadata.putArray("label_ids");
        boolean draft = false;
        boolean sent = false;
        for (JsonNode label : raw.path("labelIds")) {
            if (!label.isTextual() || label.textValue().isBlank()) continue;
            labels.add(label.textValue());
            draft |= label.textValue().equals("DRAFT");
            sent |= label.textValue().equals("SENT");
        }
        var recipients = metadata.putArray("to_addresses");
        MailBody.addresses(part, "To").forEach(address -> recipients.add(address.getAddress()));
        boolean self = sender != null && sender.getAddress().equalsIgnoreCase(text(out, "connection_id"));
        if (draft || MailBody.automated(part)) out.put("message_kind", "other");
        else if (sent || self) out.put("message_kind", "staff_reply");
        else out.put("message_kind", verifiedKind);
    }
}
