package com.rescuebridge.agent;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public class BridgeService extends Service {
    public static final String ACTION_START = "com.rescuebridge.agent.START";
    public static final String ACTION_STOP = "com.rescuebridge.agent.STOP";
    private static final String CHANNEL = "rescue_bridge_active";
    private static final int NOTIFICATION_ID = 41051;

    private static volatile String status = "Stopped";
    public static String getStatus() { return status; }

    private final AtomicBoolean running = new AtomicBoolean(false);
    private ExecutorService executor;
    private volatile Socket relaySocket;
    private volatile Socket localSocket;

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel c = new NotificationChannel(CHANNEL, "Remote rescue", NotificationManager.IMPORTANCE_LOW);
        c.setDescription("Shown while Rescue Bridge is accepting authorized remote support connections.");
        nm.createNotificationChannel(c);
        executor = Executors.newSingleThreadExecutor();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopBridge("Stopped by owner");
            return START_NOT_STICKY;
        }
        if (running.compareAndSet(false, true)) {
            setStatus("Starting…");
            Notification n = notification(status);
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            } else {
                startForeground(NOTIFICATION_ID, n);
            }
            executor.submit(this::runLoop);
        }
        return START_NOT_STICKY; // intentionally not silently resurrected after reboot/process death
    }

    private void runLoop() {
        int backoff = 2;
        try {
            Config cfg = SecureStore.load(this);
            if (cfg == null) throw new IllegalStateException("No Rescue Bridge configuration saved");
            cfg.validate();
            while (running.get()) {
                try {
                    setStatus("Connecting to relay…");
                    Socket relay = TlsUtil.connect(cfg, 10000);
                    relaySocket = relay;
                    relay.setSoTimeout(25000);
                    InputStream rin = relay.getInputStream(); OutputStream rout = relay.getOutputStream();
                    JSONObject hello = new JSONObject();
                    hello.put("v", 1); hello.put("role", "agent"); hello.put("device_id", cfg.deviceId); hello.put("token", cfg.token);
                    Protocol.line(rout, hello.toString());
                    String first = Protocol.line(rin, 1024);
                    if (!"WAIT".equals(first)) throw new IllegalStateException("Relay: " + first);
                    backoff = 2;
                    setStatus("Ready — waiting for authorized Mac");

                    while (running.get()) {
                        String line;
                        try {
                            line = Protocol.line(rin, 1024);
                        } catch (SocketTimeoutException timeout) {
                            Protocol.line(rout, "PING");
                            continue;
                        }
                        if ("PONG".equals(line)) continue;
                        if ("PREPARE".equals(line)) {
                            setStatus("Mac requested connection — opening local ADB…");
                            Socket local = new Socket();
                            try {
                                local.connect(new InetSocketAddress(cfg.localAdbHost, cfg.localAdbPort), 5000);
                                local.setKeepAlive(true);
                                localSocket = local;
                            } catch (Exception exc) {
                                try { local.close(); } catch (Exception ignored) {}
                                Protocol.line(rout, "ERROR local_adb_unavailable");
                                throw new IllegalStateException("Local ADB unavailable at " + cfg.localAdbHost + ":" + cfg.localAdbPort, exc);
                            }
                            Protocol.line(rout, "READY");
                            relay.setSoTimeout(0);
                            setStatus("Remote ADB ACTIVE");
                            proxy(relay, local);
                            break; // consumed relay connection; reconnect and wait for the next session
                        }
                        if (line.startsWith("ERROR ")) throw new IllegalStateException("Relay: " + line);
                    }
                } catch (Exception exc) {
                    closeSockets();
                    if (!running.get()) break;
                    setStatus("Disconnected: " + shortMessage(exc) + " — retrying in " + backoff + "s");
                    sleep(backoff * 1000L);
                    backoff = Math.min(30, backoff * 2);
                } finally {
                    closeSockets();
                }
            }
        } catch (Exception fatal) {
            setStatus("Stopped: " + shortMessage(fatal));
        } finally {
            running.set(false);
            closeSockets();
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
    }

    private void proxy(Socket relay, Socket local) throws InterruptedException {
        Thread a = new Thread(() -> {
            try { Protocol.copy(relayInput(relay), localOutput(local)); } catch (Exception ignored) {} finally { close(relay); close(local); }
        }, "relay-to-adb");
        Thread b = new Thread(() -> {
            try { Protocol.copy(localInput(local), relayOutput(relay)); } catch (Exception ignored) {} finally { close(relay); close(local); }
        }, "adb-to-relay");
        a.start(); b.start();
        while (running.get() && a.isAlive() && b.isAlive()) {
            a.join(500); b.join(500);
        }
        close(relay); close(local);
        a.join(1500); b.join(1500);
        setStatus(running.get() ? "Session ended — reconnecting" : "Stopped");
    }

    private static InputStream relayInput(Socket s) { try { return s.getInputStream(); } catch (Exception e) { throw new RuntimeException(e); } }
    private static OutputStream relayOutput(Socket s) { try { return s.getOutputStream(); } catch (Exception e) { throw new RuntimeException(e); } }
    private static InputStream localInput(Socket s) { return relayInput(s); }
    private static OutputStream localOutput(Socket s) { return relayOutput(s); }

    private static void close(Socket s) { if (s != null) try { s.close(); } catch (Exception ignored) {} }
    private void closeSockets() { Socket r=relaySocket,l=localSocket; relaySocket=null; localSocket=null; close(r); close(l); }
    private void sleep(long ms) { try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } }
    private String shortMessage(Throwable t) { String m=t.getMessage(); return (m==null||m.trim().isEmpty())?t.getClass().getSimpleName():(m.length()>120?m.substring(0,120):m); }

    private void setStatus(String value) { status=value; updateNotification(); }

    private Notification notification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent content = PendingIntent.getActivity(this, 1, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, BridgeService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 2, stop, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(com.rescuebridge.agent.R.drawable.ic_rescue)
                .setContentTitle("Rescue Bridge is enabled")
                .setContentText(text)
                .setContentIntent(content)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_rescue), "Turn off", stopPi).build())
                .build();
    }

    private void updateNotification() {
        if (!running.get()) return;
        NotificationManager nm=(NotificationManager)getSystemService(Context.NOTIFICATION_SERVICE);
        nm.notify(NOTIFICATION_ID, notification(status));
    }

    private void stopBridge(String why) {
        running.set(false); setStatus(why); closeSockets();
        if (executor != null) executor.shutdownNow();
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf();
    }

    @Override public void onDestroy() { running.set(false); closeSockets(); if(executor!=null)executor.shutdownNow(); super.onDestroy(); }
    @Override public IBinder onBind(Intent intent) { return null; }
}
