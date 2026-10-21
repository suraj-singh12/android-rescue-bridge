package com.rescuebridge.agent;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
    private static final int IMPORT_CONFIG = 7001;
    private EditText relayHost, relayPort, deviceId, token, localHost, localPort, verify, fingerprint;
    private CheckBox tls;
    private TextView status;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable ticker = new Runnable() { public void run() { if(status!=null)status.setText("Status: " + BridgeService.getStatus()); handler.postDelayed(this,1000); } };

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 99);
        }
        setContentView(buildUi());
        loadSaved();
        handler.post(ticker);
    }

    private View buildUi() {
        int pad=dp(20), gap=dp(10);
        ScrollView scroll=new ScrollView(this); LinearLayout root=new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setPadding(pad,pad,pad,pad); scroll.addView(root);
        root.setOnApplyWindowInsetsListener((v,insets)->{ if (Build.VERSION.SDK_INT >= 30) { android.graphics.Insets s=insets.getInsets(WindowInsets.Type.systemBars()); v.setPadding(pad,pad+s.top,pad,pad+s.bottom); } else { v.setPadding(pad,pad+insets.getSystemWindowInsetTop(),pad,pad+insets.getSystemWindowInsetBottom()); } return insets; });

        TextView title=text("Rescue Bridge",26,true); root.addView(title);
        TextView intro=text("No account or sign-in. When you tap Enable Remote Rescue, this phone opens an authenticated outbound connection to your self-hosted relay. Android shows an ongoing notification while support is active. It does not auto-start after reboot.",15,false); add(root,intro,gap);

        Button importBtn=button("Import phone config JSON",v->importConfig()); add(root,importBtn,gap);
        relayHost=field("Relay host / IP"); relayPort=field("Relay port"); deviceId=field("Device ID"); token=field("Device token"); token.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);
        tls=new CheckBox(this); tls.setText("Use TLS (recommended / required on Internet)"); tls.setChecked(true);
        verify=field("TLS verify: system | pin | none"); fingerprint=field("Pinned cert SHA-256 (64 hex chars)"); localHost=field("Local ADB host"); localPort=field("Local ADB port");
        for(View v:new View[]{relayHost,relayPort,deviceId,token,tls,verify,fingerprint,localHost,localPort}) add(root,v,gap);

        LinearLayout row=new LinearLayout(this); row.setOrientation(LinearLayout.HORIZONTAL);
        Button save=button("Save",v->save()); Button test=button("Test local ADB",v->testAdb()); row.addView(save,weight()); row.addView(test,weight()); add(root,row,gap);
        Button start=button("Enable Remote Rescue",v->startBridge()); Button stop=button("Turn Off Remote Rescue",v->stopBridge()); add(root,start,gap); add(root,stop,gap);
        status=text("Status: Stopped",16,true); add(root,status,gap);
        TextView note=text("Important: the bridge can only forward ADB if adbd is already listening on the configured local TCP port (normally provisioned once with ‘adb tcpip 5555’). Stock Android may reset that after a reboot; this app cannot secretly re-enable ADB or bypass Android security.",14,false); add(root,note,gap);
        Button settings=button("Open this app's Android settings",v->startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:"+getPackageName())))); add(root,settings,gap);
        return scroll;
    }

    private void importConfig() { Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT); i.setType("application/json"); i.addCategory(Intent.CATEGORY_OPENABLE); startActivityForResult(i,IMPORT_CONFIG); }
    @Override protected void onActivityResult(int req,int res,Intent data) { super.onActivityResult(req,res,data); if(req==IMPORT_CONFIG&&res==RESULT_OK&&data!=null&&data.getData()!=null){ try(InputStream in=getContentResolver().openInputStream(data.getData())){ ByteArrayOutputStream b=new ByteArrayOutputStream(); byte[] buf=new byte[8192]; int n,total=0; while((n=in.read(buf))>0){total+=n;if(total>128*1024)throw new IllegalArgumentException("Config file too large");b.write(buf,0,n);} Config c=Config.fromJson(b.toString(StandardCharsets.UTF_8.name())); populate(c); toast("Config imported. Tap Save, then Test local ADB."); }catch(Exception e){toast("Import failed: "+e.getMessage());}} }

    private Config fromFields() {
        try {
            JSONObject o=new JSONObject(); o.put("version",1); o.put("relayHost",relayHost.getText().toString().trim()); o.put("relayPort",Integer.parseInt(relayPort.getText().toString().trim())); o.put("tls",tls.isChecked()); o.put("tlsVerify",verify.getText().toString().trim()); o.put("certSha256",fingerprint.getText().toString().trim()); o.put("deviceId",deviceId.getText().toString().trim()); o.put("token",token.getText().toString().trim()); o.put("localAdbHost",localHost.getText().toString().trim()); o.put("localAdbPort",Integer.parseInt(localPort.getText().toString().trim())); return Config.fromJson(o.toString());
        } catch(Exception e){ throw new IllegalArgumentException(e.getMessage(),e); }
    }
    private void populate(Config c){relayHost.setText(c.relayHost);relayPort.setText(String.valueOf(c.relayPort));tls.setChecked(c.tls);verify.setText(c.tlsVerify);fingerprint.setText(c.certSha256);deviceId.setText(c.deviceId);token.setText(c.token);localHost.setText(c.localAdbHost);localPort.setText(String.valueOf(c.localAdbPort));}
    private void loadSaved(){try{Config c=SecureStore.load(this);if(c!=null)populate(c);else{relayPort.setText("8443");verify.setText("pin");localHost.setText("127.0.0.1");localPort.setText("5555");}}catch(Exception e){toast("Could not load saved config: "+e.getMessage());}}
    private boolean save(){try{SecureStore.save(this,fromFields());toast("Saved securely using Android Keystore");return true;}catch(Exception e){toast("Save failed: "+e.getMessage());return false;}}
    private void testAdb(){ final Config c;try{c=fromFields();}catch(Exception e){toast(e.getMessage());return;} status.setText("Status: Testing local ADB socket…"); new Thread(()->{String result;try(Socket s=new Socket()){s.connect(new InetSocketAddress(c.localAdbHost,c.localAdbPort),3000);result="Local ADB TCP port is reachable";}catch(Exception e){result="Local ADB unavailable: "+e.getMessage();} final String r=result;runOnUiThread(()->{status.setText("Status: "+r);toast(r);});},"test-adb").start();}
    private void startBridge(){if(!save())return; Intent i=new Intent(this,BridgeService.class).setAction(BridgeService.ACTION_START); if(Build.VERSION.SDK_INT>=26)startForegroundService(i);else startService(i);}
    private void stopBridge(){startService(new Intent(this,BridgeService.class).setAction(BridgeService.ACTION_STOP));}

    private TextView text(String s,int sp,boolean bold){TextView v=new TextView(this);v.setText(s);v.setTextSize(sp);if(bold)v.setTypeface(Typeface.DEFAULT,Typeface.BOLD);return v;}
    private EditText field(String hint){EditText e=new EditText(this);e.setHint(hint);e.setSingleLine(true);e.setTextSize(15);return e;}
    private Button button(String s,View.OnClickListener l){Button b=new Button(this);b.setText(s);b.setOnClickListener(l);return b;}
    private LinearLayout.LayoutParams weight(){return new LinearLayout.LayoutParams(0,ViewGroup.LayoutParams.WRAP_CONTENT,1f);}
    private void add(LinearLayout root,View v,int bottom){LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.WRAP_CONTENT);p.bottomMargin=bottom;root.addView(v,p);}
    private int dp(int n){return (int)(n*getResources().getDisplayMetrics().density+.5f);}
    private void toast(String s){Toast.makeText(this,s,Toast.LENGTH_LONG).show();}
    @Override protected void onDestroy(){handler.removeCallbacks(ticker);super.onDestroy();}
}
