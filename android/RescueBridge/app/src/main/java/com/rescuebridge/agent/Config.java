package com.rescuebridge.agent;

import org.json.JSONException;
import org.json.JSONObject;

public final class Config {
    public int version = 1;
    public String relayHost = "";
    public int relayPort = 8443;
    public boolean tls = true;
    public String tlsVerify = "pin"; // system | pin | none
    public String certSha256 = "";
    public String deviceId = "";
    public String token = "";
    public String localAdbHost = "127.0.0.1";
    public int localAdbPort = 5555;

    public static Config fromJson(String raw) throws JSONException {
        JSONObject o = new JSONObject(raw);
        Config c = new Config();
        c.version = o.optInt("version", 1);
        c.relayHost = o.optString("relayHost", "").trim();
        c.relayPort = o.optInt("relayPort", 8443);
        c.tls = o.optBoolean("tls", true);
        c.tlsVerify = o.optString("tlsVerify", "pin").trim().toLowerCase();
        c.certSha256 = o.optString("certSha256", "").replaceAll("[^0-9A-Fa-f]", "").toUpperCase();
        c.deviceId = o.optString("deviceId", "").trim();
        c.token = o.optString("token", "").trim();
        c.localAdbHost = o.optString("localAdbHost", "127.0.0.1").trim();
        c.localAdbPort = o.optInt("localAdbPort", 5555);
        c.validate();
        return c;
    }

    public JSONObject toJsonObject() throws JSONException {
        JSONObject o = new JSONObject();
        o.put("version", version);
        o.put("relayHost", relayHost);
        o.put("relayPort", relayPort);
        o.put("tls", tls);
        o.put("tlsVerify", tlsVerify);
        o.put("certSha256", certSha256);
        o.put("deviceId", deviceId);
        o.put("token", token);
        o.put("localAdbHost", localAdbHost);
        o.put("localAdbPort", localAdbPort);
        return o;
    }

    public String toJson() throws JSONException { return toJsonObject().toString(); }

    public void validate() {
        if (relayHost.isEmpty()) throw new IllegalArgumentException("Relay host is required");
        if (relayPort < 1 || relayPort > 65535) throw new IllegalArgumentException("Invalid relay port");
        if (deviceId.isEmpty() || deviceId.length() > 128) throw new IllegalArgumentException("Invalid device ID");
        if (token.length() < 20) throw new IllegalArgumentException("Token is missing/too short");
        if (localAdbHost.isEmpty()) throw new IllegalArgumentException("Local ADB host is required");
        if (localAdbPort < 1 || localAdbPort > 65535) throw new IllegalArgumentException("Invalid local ADB port");
        if (!tlsVerify.equals("system") && !tlsVerify.equals("pin") && !tlsVerify.equals("none"))
            throw new IllegalArgumentException("tlsVerify must be system, pin, or none");
        if (tls && tlsVerify.equals("pin") && certSha256.length() != 64)
            throw new IllegalArgumentException("Pinned TLS mode requires a 64-hex-character SHA-256 certificate fingerprint");
    }
}
