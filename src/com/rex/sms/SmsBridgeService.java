package com.rex.sms;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.telephony.SmsManager;
import android.telephony.PhoneStateListener;
import android.telephony.TelephonyManager;
import android.util.Log;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.ServerSocket;
import java.net.Socket;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;
import java.lang.reflect.Method;

public class SmsBridgeService extends Service {

    private static final String TAG = "RexBridge";
    private static final int SOCKET_PORT = 9999;
    private static final int NOTIFICATION_ID = 1;

    private ServerSocket serverSocket;
    private AtomicBoolean running = new AtomicBoolean(false);
    private volatile String callState = "UNKNOWN";
    private volatile String callNumber = "";
    private TelephonyManager telephonyManager;
    private PhoneStateListener phoneStateListener;

    @Override
    public void onCreate() {
        super.onCreate();
        Log.d(TAG, "Service created");
        createNotificationChannel();
        startForeground(NOTIFICATION_ID, buildNotification());
        registerCallStateListener();
        startSocketListener();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startFlags) {
        Log.d(TAG, "Service started");
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        running.set(false);
        try {
            if (serverSocket != null && !serverSocket.isClosed()) {
                serverSocket.close();
            }
        } catch (Exception e) {
            Log.e(TAG, "Error closing socket", e);
        }
        Log.d(TAG, "Service destroyed");
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        android.app.NotificationManager nm =
                (android.app.NotificationManager) getSystemService("notification");
        if (nm == null) {
            Log.e(TAG, "NotificationManager is null");
            return;
        }

        try {
            Class<?> ncClass = Class.forName("android.app.NotificationChannel");
            Object channel = ncClass.getConstructor(String.class, CharSequence.class, int.class)
                    .newInstance("rexbridge_channel", "RexBridge", 3);
            Log.d(TAG, "NotificationChannel object created");

            Method create = android.app.NotificationManager.class.getMethod("createNotificationChannel", ncClass);
            create.invoke(nm, channel);
            Log.d(TAG, "NotificationChannel registered successfully");
        } catch (Exception e) {
            Log.e(TAG, "Channel creation failed: " + e.getMessage());
            e.printStackTrace();
        }
    }

    private Notification buildNotification() {
        // Use single-arg Builder(Context) — exists since API 1, compiles on API 23
        // Then call setChannelId() via reflection for Android 8+ runtime
        android.app.Notification.Builder builder = new android.app.Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.ic_dialog_info);
        builder.setContentTitle("RexBridge");
        builder.setContentText("SMS bridge active");
        builder.setOngoing(true);
        builder.setWhen(System.currentTimeMillis());

        // Set channel ID via reflection (method added in API 26)
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                java.lang.reflect.Method setChannelId = builder.getClass().getMethod("setChannelId", String.class);
                setChannelId.invoke(builder, "rexbridge_channel");
                Log.d(TAG, "setChannelId invoked via reflection");
            } catch (Exception e) {
                Log.e(TAG, "setChannelId reflection failed: " + e.getMessage());
            }
        }

        return builder.getNotification();
    }

    private void startSocketListener() {
        running.set(true);
        new Thread(() -> {
            try {
                serverSocket = new ServerSocket(SOCKET_PORT);
                Log.d(TAG, "Listening on port " + SOCKET_PORT);

                while (running.get()) {
                    try {
                        Socket client = serverSocket.accept();
                        Log.d(TAG, "Client connected");
                        handleClient(client);
                    } catch (Exception e) {
                        if (running.get()) {
                            Log.e(TAG, "Socket accept error", e);
                        }
                    }
                }
            } catch (Exception e) {
                Log.e(TAG, "Server socket error", e);
            }
        }).start();
    }

    private void handleClient(Socket client) {
        try {
            BufferedReader in = new BufferedReader(
                    new InputStreamReader(client.getInputStream()));
            PrintWriter out = new PrintWriter(client.getOutputStream(), true);

            String line = in.readLine();
            Log.d(TAG, "Received: " + line);

            if (line == null) {
                out.println("ERR empty input");
            } else if (line.equals("PING")) {
                out.println("PONG");
            } else if (line.startsWith("SEND ")) {
                handleSend(line.substring(5).trim(), out);
            } else if (line.equals("READ_INBOX")) {
                handleReadInbox(out);
            } else if (line.startsWith("CALL ")) {
                handleCall(line.substring(5).trim(), out);
            } else if (line.equals("CALL_STATUS")) {
                out.println("OK {\"state\":\"" + getCurrentCallState() + "\",\"number\":\"" + callNumber + "\"}");
            } else if (line.equals("HANGUP")) {
                handleHangup(out);
            } else if (line.equals("ANSWER")) {
                handleAnswer(out);
            } else if (line.startsWith("AUDIO_TEST ")) {
                handleAudioTest(line.substring("AUDIO_TEST ".length()).trim(), out);
            } else {
                out.println("ERR unknown command");
            }
        } catch (Exception e) {
            Log.e(TAG, "Client handler error", e);
        } finally {
            try {
                client.close();
            } catch (Exception e) {
                Log.e(TAG, "Error closing client", e);
            }
        }
    }

    private void registerCallStateListener() {
        telephonyManager = (TelephonyManager) getSystemService(TELEPHONY_SERVICE);
        if (telephonyManager == null) {
            Log.e(TAG, "TelephonyManager unavailable");
            return;
        }
        phoneStateListener = new PhoneStateListener() {
            @Override
            public void onCallStateChanged(int state, String incomingNumber) {
                if (state == TelephonyManager.CALL_STATE_RINGING) {
                    callState = "RINGING";
                    callNumber = incomingNumber == null ? "" : incomingNumber;
                } else if (state == TelephonyManager.CALL_STATE_OFFHOOK) {
                    callState = "OFFHOOK";
                } else if (state == TelephonyManager.CALL_STATE_IDLE) {
                    callState = "IDLE";
                    callNumber = "";
                }
            }
        };
        try {
            telephonyManager.listen(phoneStateListener, PhoneStateListener.LISTEN_CALL_STATE);
        } catch (SecurityException e) {
            Log.e(TAG, "Call-state listener registration failed", e);
        }
    }

    private String getCurrentCallState() {
        try {
            TelephonyManager manager = (TelephonyManager) getSystemService(TELEPHONY_SERVICE);
            if (manager != null) {
                int state = manager.getCallState();
                if (state == TelephonyManager.CALL_STATE_IDLE) return "IDLE";
                if (state == TelephonyManager.CALL_STATE_RINGING) return "RINGING";
                if (state == TelephonyManager.CALL_STATE_OFFHOOK) return "OFFHOOK";
            }
        } catch (Exception e) {
            Log.e(TAG, "Call-state query failed", e);
        }
        return callState;
    }

    private void handleCall(String phone, PrintWriter out) {
        if (phone.length() == 0) {
            out.println("FAIL missing phone number");
            return;
        }
        try {
            if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission("android.permission.CALL_PHONE") != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                out.println("FAIL CALL_PHONE permission not granted");
                return;
            }
            Intent intent = new Intent(Intent.ACTION_CALL);
            intent.setData(android.net.Uri.parse("tel:" + android.net.Uri.encode(phone)));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
            callNumber = phone;
            callState = "DIALING";
            out.println("OK dialing " + phone);
        } catch (Exception e) {
            out.println("FAIL " + e.getClass().getSimpleName() + ": " + e.getMessage());
            Log.e(TAG, "Call failed", e);
        }
    }

    private void handleHangup(PrintWriter out) {
        try {
            if (Build.VERSION.SDK_INT >= 28) {
                Object telecom = getSystemService("telecom");
                if (telecom == null) {
                    out.println("FAIL hangup unavailable: telecom service is null");
                    return;
                }
                Method endCall = telecom.getClass().getMethod("endCall");
                Object result = endCall.invoke(telecom);
                if (result instanceof Boolean && !((Boolean) result)) {
                    out.println("FAIL hangup rejected by TelecomManager");
                    return;
                }
                out.println("OK hangup " + result);
                return;
            }
            out.println("FAIL hangup requires Android 9+");
        } catch (Exception e) {
            Throwable cause = e;
            if (e instanceof java.lang.reflect.InvocationTargetException && e.getCause() != null) {
                cause = e.getCause();
            }
            out.println("FAIL hangup unavailable: " + cause.getClass().getSimpleName() + ": " + cause.getMessage());
            Log.e(TAG, "Hangup failed", e);
        }
    }

    private void handleAnswer(PrintWriter out) {
        try {
            if (Build.VERSION.SDK_INT < 26) {
                out.println("FAIL answer requires Android 8+");
                return;
            }
            if (checkSelfPermission("android.permission.ANSWER_PHONE_CALLS") != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                out.println("FAIL ANSWER_PHONE_CALLS permission not granted");
                return;
            }
            Object telecom = getSystemService("telecom");
            if (telecom == null) {
                out.println("FAIL answer unavailable: telecom service is null");
                return;
            }
            Method answer = telecom.getClass().getMethod("acceptRingingCall");
            answer.invoke(telecom);
            out.println("OK answer requested");
        } catch (Exception e) {
            Throwable cause = e;
            if (e instanceof java.lang.reflect.InvocationTargetException && e.getCause() != null) {
                cause = e.getCause();
            }
            out.println("FAIL answer unavailable: " + cause.getClass().getSimpleName() + ": " + cause.getMessage());
            Log.e(TAG, "Answer failed", e);
        }
    }

    private void handleAudioTest(String args, PrintWriter out) {
        String[] parts = args.split("\\s+");
        int seconds;
        double frequency;
        try {
            seconds = Integer.parseInt(parts[0]);
            frequency = parts.length > 1 ? Double.parseDouble(parts[1]) : 1000.0;
        } catch (Exception e) {
            out.println("FAIL usage AUDIO_TEST <seconds> [frequency]");
            return;
        }
        if (seconds < 1 || seconds > 15 || frequency <= 0 || frequency >= 20000) {
            out.println("FAIL invalid duration or frequency");
            return;
        }
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission("android.permission.RECORD_AUDIO") != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            out.println("FAIL RECORD_AUDIO permission not granted");
            return;
        }
        final int rate = 48000;
        int buffer = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (buffer <= 0) {
            out.println("FAIL audio input unavailable");
            return;
        }
        AudioRecord recorder = null;
        try {
            recorder = new AudioRecord(MediaRecorder.AudioSource.DEFAULT, rate,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(buffer, rate / 2));
            short[] samples = new short[rate * seconds];
            recorder.startRecording();
            int captured = 0;
            while (captured < samples.length) {
                int count = recorder.read(samples, captured, samples.length - captured);
                if (count < 0) throw new IllegalStateException("AudioRecord read " + count);
                captured += count;
            }
            recorder.stop();
            double sum = 0.0;
            double state1 = 0.0;
            double state2 = 0.0;
            double coefficient = 2.0 * Math.cos(2.0 * Math.PI * frequency / rate);
            for (int i = 0; i < captured; i++) {
                double value = samples[i] / 32768.0;
                sum += value * value;
                double state0 = value + coefficient * state1 - state2;
                state2 = state1;
                state1 = state0;
            }
            double rms = Math.sqrt(sum / captured);
            double power = state1 * state1 + state2 * state2 - coefficient * state1 * state2;
            double tone = power > 0 ? Math.sqrt(power) / captured * 2.0 : 0.0;
            out.println("OK {\"rms\":" + rms + ",\"frequency\":" + frequency + ",\"tone\":" + tone + ",\"frames\":" + captured + "}");
        } catch (Exception e) {
            out.println("FAIL audio capture: " + e.getClass().getSimpleName());
            Log.e(TAG, "Audio test failed", e);
        } finally {
            if (recorder != null) recorder.release();
        }
    }

    private void handleSend(String args, PrintWriter out) {
        try {
            int spaceIndex = args.indexOf(' ');
            String phone;
            String message;
            if (spaceIndex > 0) {
                phone = args.substring(0, spaceIndex);
                message = args.substring(spaceIndex + 1);
            } else {
                phone = args;
                message = "";
            }

            Log.d(TAG, "Sending SMS to " + phone);
            SmsManager smsManager = SmsManager.getDefault();

            if (message.length() > 160) {
                // Split long message manually and send parts
                ArrayList<String> parts = smsManager.divideMessage(message);
                sendMultipart(smsManager, phone, parts);
            } else {
                smsManager.sendTextMessage(phone, null, message, null, null);
            }

            out.println("OK sent to " + phone);
            Log.d(TAG, "SMS sent successfully to " + phone);
        } catch (Exception e) {
            String error = e.getClass().getSimpleName() + ": " + e.getMessage();
            out.println("FAIL " + error);
            Log.e(TAG, "SMS send failed", e);
        }
    }

    /**
     * Send multipart SMS using reflection (API 24+).
     * Falls back to sending parts sequentially on older APIs.
     */
    private void sendMultipart(SmsManager smsManager, String phone, ArrayList<String> parts) {
        if (Build.VERSION.SDK_INT >= 24) {
            try {
                Method method = SmsManager.class.getMethod(
                        "sendMultipartTextMessage",
                        String.class, String.class,
                        ArrayList.class,
                        ArrayList.class, ArrayList.class);
                method.invoke(smsManager, phone, null, parts, null, null);
                return;
            } catch (Exception e) {
                Log.w(TAG, "Multipart send failed, falling back", e);
            }
        }
        // Fallback: send parts individually
        for (String part : parts) {
            try {
                smsManager.sendTextMessage(phone, null, part, null, null);
            } catch (Exception e) {
                Log.e(TAG, "Part send failed", e);
            }
        }
    }

    /**
     * Read incoming SMS from the JSON inbox directory.
     * Returns JSON array of unread messages, then clears them.
     */
    private void handleReadInbox(PrintWriter out) {
        try {
            File inboxDir = new File(getExternalFilesDir(null), "inbox");
            if (!inboxDir.exists()) {
                out.println("OK []");
                return;
            }

            File[] files = inboxDir.listFiles((dir, name) -> name.endsWith(".json"));
            if (files == null || files.length == 0) {
                out.println("OK []");
                return;
            }

            java.util.Arrays.sort(files, (a, b) -> a.getName().compareTo(b.getName()));
            StringBuilder sb = new StringBuilder();
            sb.append("OK [\n");

            boolean first = true;
            for (File file : files) {
                try {
                    java.io.FileReader reader = new java.io.FileReader(file);
                    int c;
                    if (!first) sb.append(",\n");
                    while ((c = reader.read()) != -1) {
                        sb.append((char) c);
                    }
                    first = false;
                    reader.close();
                    file.delete();
                } catch (Exception e) {
                    Log.e(TAG, "Error reading inbox file " + file.getName(), e);
                }
            }
            sb.append("\n]");
            out.println(sb.toString());
        } catch (Exception e) {
            out.println("FAIL " + e.getMessage());
        }
    }
}
