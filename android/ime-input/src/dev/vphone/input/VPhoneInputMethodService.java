package dev.vphone.input;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.inputmethodservice.InputMethodService;
import android.os.Build;
import android.text.InputType;
import android.util.Base64;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.ExtractedText;
import android.view.inputmethod.ExtractedTextRequest;
import android.view.inputmethod.InputConnection;

import java.nio.charset.StandardCharsets;

/** Commits ADB-provided text through the focused editor's standard input connection. */
public final class VPhoneInputMethodService extends InputMethodService {
    public static final String ACTION_COMMIT_TEXT = "dev.vphone.input.COMMIT_TEXT";
    public static final String ACTION_REPLACE_TEXT = "dev.vphone.input.REPLACE_TEXT";
    public static final String ACTION_HIDE_INPUT = "dev.vphone.input.HIDE_INPUT";
    public static final String EXTRA_TEXT_BASE64 = "text_base64";

    private static final int RESULT_COMMITTED = 1;
    private static final int RESULT_NO_CONNECTION = 2;
    private static final int RESULT_INVALID_TEXT = 3;
    private static final int RESULT_REJECTED = 4;

    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (ACTION_HIDE_INPUT.equals(intent.getAction())) {
                requestHideSelf(0);
                setResultCode(RESULT_COMMITTED);
                setResultData("hidden");
                return;
            }

            String encoded = intent.getStringExtra(EXTRA_TEXT_BASE64);
            if (encoded == null) {
                setResultCode(RESULT_INVALID_TEXT);
                setResultData("missing text_base64");
                return;
            }

            final String text;
            try {
                byte[] data = Base64.decode(encoded, Base64.NO_WRAP);
                text = new String(data, StandardCharsets.UTF_8);
            } catch (IllegalArgumentException error) {
                setResultCode(RESULT_INVALID_TEXT);
                setResultData("invalid text_base64");
                return;
            }

            EditorInfo editor = getCurrentInputEditorInfo();
            InputConnection connection = getCurrentInputConnection();
            if (editor == null || editor.inputType == InputType.TYPE_NULL || connection == null) {
                setResultCode(RESULT_NO_CONNECTION);
                setResultData("no input connection");
                return;
            }

            if (ACTION_REPLACE_TEXT.equals(intent.getAction()) && !selectAll(connection)) {
                setResultCode(RESULT_REJECTED);
                setResultData("input connection rejected select all");
                return;
            }

            if (!connection.commitText(text, 1)) {
                setResultCode(RESULT_REJECTED);
                setResultData("input connection rejected text");
                return;
            }

            setResultCode(RESULT_COMMITTED);
            setResultData("committed");
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        IntentFilter filter = new IntentFilter(ACTION_COMMIT_TEXT);
        filter.addAction(ACTION_REPLACE_TEXT);
        filter.addAction(ACTION_HIDE_INPUT);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(
                    receiver,
                    filter,
                    android.Manifest.permission.DUMP,
                    null,
                    Context.RECEIVER_EXPORTED
            );
        } else {
            registerReceiver(receiver, filter, android.Manifest.permission.DUMP, null);
        }
    }

    @Override
    public View onCreateInputView() {
        return null;
    }

    @Override
    public boolean onEvaluateInputViewShown() {
        return false;
    }

    @Override
    public boolean onEvaluateFullscreenMode() {
        return false;
    }

    @Override
    public void onDestroy() {
        unregisterReceiver(receiver);
        super.onDestroy();
    }

    private static boolean selectAll(InputConnection connection) {
        ExtractedText extracted = connection.getExtractedText(new ExtractedTextRequest(), 0);
        if (extracted != null && extracted.text != null && extracted.startOffset >= 0) {
            int start = extracted.startOffset;
            if (connection.setSelection(start, start + extracted.text.length())) {
                return true;
            }
        }
        return connection.performContextMenuAction(android.R.id.selectAll);
    }
}
