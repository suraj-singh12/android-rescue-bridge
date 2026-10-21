# Rescue Bridge — no-account remote ADB for owner-authorized Android support

Documentation: start here for setup, then read [ARCHITECTURE.md](ARCHITECTURE.md) for internals and limitations, and [VALIDATION.md](VALIDATION.md) for recorded test evidence. Contributors should also read [AGENTS.md](AGENTS.md).

Rescue Bridge lets an explicitly authorized Android phone reach your Mac from another network/city without Tailscale, a user account, or a public IP on the phone.

It has three pieces:

```text
Android phone                      Self-hosted relay                    Your Mac
─────────────                      ─────────────────                    ────────
Rescue Bridge APK  ── outbound ──> TLS TCP :8443 <── outbound ──  rescue_mac.py
      │                                                                  │
      └─ 127.0.0.1:5555 (adbd)                         127.0.0.1:15555 ─┘
                                                                         │
                                                              adb / scrcpy / backup tool
```

The phone and Mac both initiate outbound connections to the relay, so carrier CGNAT/NAT is not a problem.

## Deliberate safety/UX properties

- **No account, email, password, cloud login or Tailscale login.**
- Each phone has a random 256-bit token. The relay stores only the SHA-256 hash of that token.
- The phone stores its token encrypted with Android Keystore and has `allowBackup=false`.
- A self-signed relay certificate can be pinned by SHA-256, so a domain/CA is optional.
- The Android app is **not hidden**. It has a normal launcher entry/settings page.
- While remote support is enabled, Android shows an ongoing foreground-service notification with a **Turn off** action.
- The service starts only from an explicit user action and deliberately uses `START_NOT_STICKY`; it does **not** silently start after reboot.
- The relay is self-hosted and uses only the Python standard library.
- Normal ADB RSA authorization remains in force in addition to the Rescue Bridge token.

## Important Android limitation

The app does not have permission to start privileged `adbd` TCP mode. Before the phone goes remote, use an already-authorized ADB connection once:

```bash
adb devices
adb tcpip 5555
```

The phone app then forwards its own local `127.0.0.1:5555` connection through the relay.

Some Android/OEM builds reset `adb tcpip 5555` after reboot or when Developer Options/USB debugging state changes. If that happens, the APK cannot silently restore it. The app's **Test local ADB** button will report that the local ADB port is unavailable.

If the Mac's ADB RSA key has not previously been authorized, Android can show its normal “Allow USB debugging?” authorization prompt when the remote transport is first established. The owner must approve it.

---

# Part A — Build the Android APK

Project:

```text
android/RescueBridge/
```

The project intentionally uses no third-party Android libraries. It targets Android 16/API 36 and declares the `connectedDevice` foreground-service type for the user-visible long-running network connection.

Recommended build path:

1. Install/currently use Android Studio.
2. Open `android/RescueBridge`.
3. Let Gradle sync.
4. **Build → Build APK(s)**.
5. The debug APK appears under:

```text
app/build/outputs/apk/debug/app-debug.apk
```

You can also run:

```bash
cd android/RescueBridge
./build_apk.sh
```

if a Gradle executable/wrapper is available. The project is configured for AGP 9.4.0 and Gradle 9.6.0.

For long-term use, create and keep your own release signing key rather than depending on Android Studio's debug signing key.

Install while you still have local/USB ADB access:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb tcpip 5555
```

Do **not** expose TCP/5555 with router port-forwarding. Rescue Bridge uses an outbound relay specifically so the ADB port itself never needs to be Internet-facing.

---

# Part B — Set up the self-hosted relay

You need any small Linux server/VPS/home server reachable on one TCP port. The server itself does not require a Rescue Bridge account.

Copy these files to it:

```text
relay/relay_server.py
relay/rescue_admin.py
```

Python 3 and OpenSSL are enough.

## Simple setup using a pinned self-signed certificate

Assume the relay's public IP is `203.0.113.10`:

```bash
mkdir -p ~/rescue-bridge
cd ~/rescue-bridge

python3 rescue_admin.py init-relay \
  --dir state \
  --name 203.0.113.10
```

This creates:

```text
state/relay.key
state/relay.crt
state/devices.json
```

Open TCP port **8443** in the server firewall/security group, then start:

```bash
python3 relay_server.py \
  --listen 0.0.0.0 \
  --port 8443 \
  --registry state/devices.json \
  --cert state/relay.crt \
  --key state/relay.key
```

`relay/rescue-bridge.service.example` is included as a starting systemd unit for a permanent relay.

`--insecure` exists only for isolated development testing. Do not use plaintext relay mode over the Internet.

## If you already have a CA-trusted TLS certificate

Run the relay with that certificate/key and provision devices using `--tls-verify system` instead of certificate pinning.

---

# Part C — Enroll a phone (no sign-in)

On the relay/admin machine:

```bash
python3 rescue_admin.py add-device \
  --registry state/devices.json \
  --name "Dad Pixel" \
  --relay-host 203.0.113.10 \
  --relay-port 8443 \
  --tls-verify pin \
  --cert state/relay.crt \
  --out configs
```

It creates two credentials/configs:

```text
configs/dad-pixel-XXXXXX.phone.json
configs/dad-pixel-XXXXXX.mac.json
```

The phone and Mac configs contain the random secret token. `devices.json` contains only its SHA-256 hash. Config files are created mode 0600 where the OS supports it.

### Phone

1. Transfer the `.phone.json` file to the owner's phone using a trusted method.
2. Open **Rescue Bridge**.
3. Tap **Import phone config JSON** and select it.
4. Tap **Save**.
5. Delete the plaintext `.phone.json` from Downloads/files after import; the saved copy inside the app is encrypted with Android Keystore.
6. Tap **Test local ADB**. It should say the local ADB TCP port is reachable.
7. Tap **Enable Remote Rescue**.

The phone now makes only an outbound TLS connection to your relay and automatically reconnects while the service remains enabled, including after switching Wi-Fi/mobile networks.

The owner can stop it from the app or from the ongoing Android notification.

---

# Part D — Mac usage

Requirements:

```bash
brew install --cask android-platform-tools
brew install scrcpy
```

Python 3 is also required.

Copy the phone's corresponding `.mac.json` securely to your Mac.

## Check whether the phone is online

```bash
python3 mac/rescue_mac.py \
  --config dad-pixel-XXXXXX.mac.json \
  --status
```

Authenticated status is one of:

```text
ONLINE
BUSY
OFFLINE
```

## Open the remote ADB path

Terminal 1:

```bash
python3 mac/rescue_mac.py \
  --config dad-pixel-XXXXXX.mac.json \
  --wait-agent
```

It prints something like:

```text
Local endpoint: 127.0.0.1:15555
```

Then Terminal 2:

```bash
adb connect 127.0.0.1:15555
adb -s 127.0.0.1:15555 shell
scrcpy -s 127.0.0.1:15555
```

Everything above ADB now behaves as if `127.0.0.1:15555` were the phone.

Or let the Mac bridge run `adb connect` and scrcpy automatically:

```bash
python3 mac/rescue_mac.py \
  --config dad-pixel-XXXXXX.mac.json \
  --wait-agent \
  --connect-adb \
  --scrcpy
```

`mac/remote_rescue.command` is a double-clickable macOS launcher that asks you to drag in the `.mac.json` config and then performs the wait/connect/scrcpy flow.

Once connected, your existing ADB rescue commands/scripts can use:

```text
127.0.0.1:15555
```

as the ADB serial.

---

# Revoke a phone

```bash
python3 rescue_admin.py remove-device \
  --registry state/devices.json \
  dad-pixel-XXXXXX
```

New connections from that device/config are immediately refused. An already-active byte stream should be disconnected separately if immediate active-session revocation is required.

List enrollments:

```bash
python3 rescue_admin.py list-devices --registry state/devices.json
```

---

# Persistence behavior

What persists:

- Enrollment/config on the phone (encrypted by Android Keystore).
- ADB RSA authorization unless Android/user revokes it.
- Relay identity/token until you revoke it.
- The foreground service automatically reconnects across ordinary Internet/Wi-Fi/mobile-data changes while the owner has enabled it.

What deliberately does **not** silently persist:

- Rescue Bridge does not auto-start itself after phone reboot.
- It does not hide from Android Settings/app drawer.
- It does not bypass a stopped/force-stopped app.
- It cannot re-enable `adbd` TCP mode if Android/OEM resets it after reboot.

This is intentional: it remains an owner-visible remote-support mechanism rather than covert persistence.

---

# Security notes

1. Keep `*.mac.json` secret. It contains the per-device token.
2. Delete `*.phone.json` from the phone's shared storage after importing it.
3. Prefer pinned TLS (`tlsVerify=pin`) or a CA-trusted certificate. Never use `tlsVerify=none` on an untrusted network except temporary testing.
4. Do not forward phone port 5555 to the public Internet.
5. Restrict relay firewall access where practical and keep the relay OS patched.
6. The relay terminates TLS and therefore can see tunneled ADB bytes in memory. Self-host it on infrastructure you trust.
7. Android's own ADB RSA authentication still protects `adbd`; Rescue Bridge's token/TLS is an additional transport gate, not a replacement for ADB authentication.
8. If a device is lost/reassigned, revoke its device ID and uninstall/disable Rescue Bridge.

---

# Validation performed

The Python relay/admin/Mac components were syntax-compiled and tested end-to-end with a simulated Android agent and local ADB-like byte stream. Tests included:

- 300 KB binary payload across the complete agent → relay → Mac path.
- TLS with a self-signed SHA-256-pinned certificate.
- Per-device token authentication.
- Relay waiting/connection lifecycle.

The Android source was constructed against current public Android APIs, but this environment does not contain an Android SDK, so the APK itself was **not compiled here**. Open the included Android Studio project and build/test it on the intended Android models before relying on it for a critical rescue.
