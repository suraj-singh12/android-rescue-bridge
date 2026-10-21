package com.rescuebridge.agent;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

public final class Protocol {
    private Protocol() {}

    public static void line(OutputStream out, String value) throws IOException {
        out.write(value.getBytes(StandardCharsets.UTF_8));
        out.write('\n');
        out.flush();
    }

    /** Reads exactly through newline without a buffered wrapper that could consume later binary ADB bytes. */
    public static String line(InputStream in, int max) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        while (buf.size() <= max) {
            int b = in.read();
            if (b == -1) throw new IOException("Connection closed");
            if (b == '\n') return buf.toString(StandardCharsets.UTF_8.name()).trim();
            if (b != '\r') buf.write(b);
        }
        throw new IOException("Control line too long");
    }

    public static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buffer = new byte[128 * 1024];
        while (true) {
            int n = in.read(buffer);
            if (n < 0) return;
            if (n == 0) continue;
            out.write(buffer, 0, n);
            out.flush();
        }
    }
}
