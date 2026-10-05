package dev.vphone.input;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.inputmethodservice.InputMethodService;
import android.os.Build;
import android.text.InputType;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.ExtractedText;
import android.view.inputmethod.ExtractedTextRequest;
import android.view.inputmethod.InputConnection;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.nio.charset.StandardCharsets;

/** Shows a compact keyboard and commits ADB-provided text through the focused editor. */
public final class VPhoneInputMethodService extends InputMethodService
        implements View.OnClickListener {
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
        LinearLayout keyboard = new LinearLayout(this);
        keyboard.setOrientation(LinearLayout.VERTICAL);
        keyboard.setPadding(dp(6), dp(6), dp(6), dp(8));
        keyboard.setBackgroundColor(Color.rgb(224, 227, 232));

        TextView label = new TextView(this);
        label.setText("vPhone Input");
        label.setTextColor(Color.rgb(55, 60, 68));
        label.setTextSize(13);
        label.setGravity(Gravity.CENTER);
        keyboard.addView(label, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(28)
        ));

        keyboard.addView(createLetterRow("qwertyuiop"));
        keyboard.addView(createLetterRow("asdfghjkl"));
        keyboard.addView(createLetterRow("zxcvbnm"));

        LinearLayout controls = createRow();
        addKey(controls, "⌫", "BACKSPACE", 1.0f);
        addKey(controls, "space", " ", 4.0f);
        addKey(controls, "↵", "ENTER", 1.0f);
        keyboard.addView(controls);
        return keyboard;
    }

    @Override
    public boolean onEvaluateInputViewShown() {
        return true;
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

    @Override
    public void onClick(View view) {
        InputConnection connection = getCurrentInputConnection();
        Object tag = view.getTag();
        if (connection == null || !(tag instanceof String)) {
            return;
        }
        String key = (String) tag;
        if ("BACKSPACE".equals(key)) {
            connection.deleteSurroundingText(1, 0);
        } else if ("ENTER".equals(key)) {
            connection.commitText("\n", 1);
        } else {
            connection.commitText(key, 1);
        }
    }

    private LinearLayout createLetterRow(String letters) {
        LinearLayout row = createRow();
        for (int index = 0; index < letters.length(); index++) {
            String letter = String.valueOf(letters.charAt(index));
            addKey(row, letter, letter, 1.0f);
        }
        return row;
    }

    private LinearLayout createRow() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);
        row.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(48)
        ));
        return row;
    }

    private void addKey(LinearLayout row, String label, String value, float weight) {
        TextView key = new TextView(this);
        key.setText(label);
        key.setTag(value);
        key.setTextColor(Color.rgb(32, 36, 42));
        key.setTextSize(16);
        key.setGravity(Gravity.CENTER);
        key.setBackgroundColor(Color.WHITE);
        key.setClickable(true);
        key.setOnClickListener(this);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(42), weight);
        params.setMargins(dp(3), dp(3), dp(3), dp(3));
        row.addView(key, params);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
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
