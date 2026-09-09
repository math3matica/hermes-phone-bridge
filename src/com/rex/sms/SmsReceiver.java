package com.rex.sms;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telephony.SmsMessage;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

public class SmsReceiver extends BroadcastReceiver {

    private static final String TAG = "RexBridge";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!intent.getAction().equals("android.provider.Telephony.SMS_RECEIVED")) {
            return;
        }

        Object[] pdus = (Object[]) intent.getExtras().get("pdus");
        if (pdus == null || pdus.length == 0) {
            Log.w(TAG, "No PDUs in broadcast");
            return;
        }

        for (Object pdu : pdus) {
            try {
                SmsMessage msg;
                byte[] bytes = (byte[]) pdu;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    msg = SmsMessage.createFromPdu(bytes);
                } else {
                    msg = SmsMessage.createFromPdu(bytes);
                }

                String address = msg.getOriginatingAddress();
                long timestamp = msg.getTimestampMillis();
                String body = msg.getMessageBody();

                Log.d(TAG, "Received SMS from " + address + ": " + body);

                // Write JSON to app-scoped external directory
                File inboxDir = new File(context.getExternalFilesDir(null), "inbox");
                if (!inboxDir.exists()) {
                    inboxDir.mkdirs();
                }

                SimpleDateFormat sdf = new SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US);
                String fileName = sdf.format(new Date(timestamp)) + ".json";
                File jsonFile = new File(inboxDir, fileName);

                StringBuilder json = new StringBuilder();
                json.append("{\n");
                json.append("  \"address\": \"").append(escapeJson(address)).append("\",\n");
                json.append("  \"timestamp\": ").append(timestamp).append(",\n");
                json.append("  \"body\": \"").append(escapeJson(body)).append("\"\n");
                json.append("}");

                FileWriter writer = new FileWriter(jsonFile);
                writer.write(json.toString());
                writer.close();

                Log.d(TAG, "Saved SMS to " + jsonFile.getAbsolutePath());

            } catch (Exception e) {
                Log.e(TAG, "Error processing SMS", e);
            }
        }
    }

    private String escapeJson(String input) {
        if (input == null) return "";
        return input.replace("\\", "\\\\")
                     .replace("\"", "\\\"")
                     .replace("\n", "\\n")
                     .replace("\r", "\\r")
                     .replace("\t", "\\t");
    }
}
