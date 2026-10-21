package com.rescuebridge.agent;

import java.net.InetSocketAddress;
import java.net.Socket;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public final class TlsUtil {
    private TlsUtil() {}

    public static Socket connect(Config c, int timeoutMs) throws Exception {
        if (!c.tls) {
            Socket s = new Socket();
            s.connect(new InetSocketAddress(c.relayHost, c.relayPort), timeoutMs);
            s.setKeepAlive(true);
            return s;
        }

        SSLSocketFactory factory;
        if (c.tlsVerify.equals("system")) {
            factory = (SSLSocketFactory) SSLSocketFactory.getDefault();
        } else {
            TrustManager[] trust = new TrustManager[]{new X509TrustManager() {
                public void checkClientTrusted(X509Certificate[] chain, String authType) {}
                public void checkServerTrusted(X509Certificate[] chain, String authType) {}
                public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            }};
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, trust, new SecureRandom());
            factory = ctx.getSocketFactory();
        }

        Socket tcp = new Socket();
        tcp.connect(new InetSocketAddress(c.relayHost, c.relayPort), timeoutMs);
        tcp.setKeepAlive(true);
        SSLSocket ssl = (SSLSocket) factory.createSocket(tcp, c.relayHost, c.relayPort, true);
        if (c.tlsVerify.equals("system")) {
            SSLParameters params = ssl.getSSLParameters();
            params.setEndpointIdentificationAlgorithm("HTTPS");
            ssl.setSSLParameters(params);
        }
        ssl.startHandshake();

        if (c.tlsVerify.equals("pin")) {
            X509Certificate leaf = (X509Certificate) ssl.getSession().getPeerCertificates()[0];
            String got = hex(MessageDigest.getInstance("SHA-256").digest(leaf.getEncoded()));
            String expected = c.certSha256.replaceAll("[^0-9A-Fa-f]", "").toUpperCase();
            if (!MessageDigest.isEqual(got.getBytes(), expected.getBytes())) {
                ssl.close();
                throw new SecurityException("Relay TLS certificate pin mismatch");
            }
        }
        return ssl;
    }

    private static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte v : b) sb.append(String.format("%02X", v));
        return sb.toString();
    }
}
