package com.rex.sms;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.widget.TextView;
import android.widget.FrameLayout;

public class SetupActivity extends Activity {

    private static final int PERMISSION_REQUEST = 1;
    private static final String[] PERMISSIONS = {
        Manifest.permission.SEND_SMS,
        Manifest.permission.RECEIVE_SMS,
        Manifest.permission.CALL_PHONE,
        Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.READ_CALL_LOG,
        "android.permission.ANSWER_PHONE_CALLS",
        Manifest.permission.RECORD_AUDIO
    };

    private TextView statusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        statusView = new TextView(this);
        statusView.setPadding(48, 96, 48, 48);
        statusView.setTextSize(16);
        statusView.setText("RexBridge\n\nTap Allow on the permission prompts below.");
        root.addView(statusView);
        setContentView(root);

        requestPermissions(PERMISSIONS, PERMISSION_REQUEST);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == PERMISSION_REQUEST) {
            boolean allGranted = true;
            StringBuilder sb = new StringBuilder("RexBridge\n\n");
            for (int i = 0; i < permissions.length; i++) {
                String status = results[i] == PackageManager.PERMISSION_GRANTED ? "GRANTED" : "DENIED";
                sb.append(permissions[i]).append(": ").append(status).append("\n");
                if (results[i] != PackageManager.PERMISSION_GRANTED) allGranted = false;
            }

            if (allGranted) {
                sb.append("\nStarting service...\n");
                statusView.setText(sb.toString());
                startService(new Intent(this, SmsBridgeService.class));
                finish();
            } else {
                sb.append("\nPermission denied. Please grant SEND_SMS and RECEIVE_SMS.");
                statusView.setText(sb.toString());
            }
        }
    }
}
