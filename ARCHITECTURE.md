# Rescue Bridge architecture

## Purpose

Expose an owner-authorized remote Android ADB connection as a local TCP endpoint on a Mac. ADB and scrcpy provide commands, file transfer and screen control; Rescue Bridge transports their bytes. It does not implement those tools or a backup engine itself.

Both endpoints connect outward to a reachable self-hosted relay. The phone needs no public IP, inbound router rule, VPN account or Rescue Bridge account. Both endpoints still need network access to the relay.

## Components

| Component | Code | Responsibility |
| --- | --- | --- |
| Enrollment CLI | `relay/rescue_admin.py` | Create relay certificate, issue paired configs, list/revoke device credentials |
| Relay | `relay/relay_server.py` | Authenticate connections, track agents, report status, pair and copy streams |
| Phone UI | `android/RescueBridge/app/src/main/java/com/rescuebridge/agent/MainActivity.java` | Import/edit/save configuration, probe local TCP port, enable/stop service |
| Phone service | `BridgeService.java` in the same directory | Maintain outbound relay connection, open local ADB on request, proxy bytes, reconnect |
| Phone helpers | `Config.java`, `SecureStore.java`, `TlsUtil.java`, `Protocol.java` | Config validation, encrypted storage, relay verification and stream framing |
| Mac bridge | `mac/rescue_mac.py` | Local listener, relay status/waiting, stream proxy, optional ADB/scrcpy launch |
| Mac launcher | `mac/remote_rescue.command` | Choose config and launch the Mac workflow |

Python components use the standard library. Android uses Java and platform APIs, minimum API 26, compile/target API 36. Build files request AGP 9.4.0 and Gradle 9.6.0; availability and compatibility have not been established by this review.

## Enrollment and state

The admin CLI generates a random 256-bit token per device. Phone and Mac JSON configs contain the same token and relay settings. The relay's JSON registry contains the token's SHA-256 hash and device name. Registry reads occur at authentication time; enrollment changes need no relay restart.

Android encrypts its saved config using AES-256-GCM with an Android Keystore key; ciphertext and IV live in private SharedPreferences. Backup is disabled. Exported configs and the Mac config remain plaintext secrets. Config files are chmod 0600 where supported.

The relay keeps waiting agents and active-session state in memory, without a database or traffic archive. The Android service loads configuration at startup; saving edits while running does not reload that service's config.

## Connection protocol

1. Phone opens relay TCP/TLS and sends one newline-terminated JSON handshake containing `v: 1`, `role: agent`, `device_id` and `token`.
2. Relay authenticates the token against the registry hash and replies `WAIT`. Idle phone read timeouts trigger `PING`; relay replies `PONG`.
3. Mac exposes `127.0.0.1:15555`. An ADB connection to that port triggers a separate authenticated relay handshake with `role: client`.
4. Relay marks the device busy and sends the phone `PREPARE`.
5. Phone connects to its configured local ADB address, normally `127.0.0.1:5555`, then sends `READY`. Failure sends `ERROR local_adb_unavailable`.
6. Relay sends Mac `GO`. These same sockets then carry raw bidirectional ADB bytes, with no further control framing.
7. Either direction ending closes the tunnel. The enabled phone service reconnects to wait for another session. An interrupted ADB operation is not resumed automatically.

An authenticated `role: status` connection returns `STATUS ONLINE`, `BUSY` or `OFFLINE`. ONLINE means an agent is registered, not that ADB has been tested or authorized. The phone's Test local ADB button checks TCP reachability only.

One active tunnel per device is supported; different enrolled devices have separate sessions. The Mac also rejects additional simultaneous local connections. A new agent registration for the same device replaces the prior agent socket, including an active one. Mac `--wait-agent` polls for ONLINE; it is not a complete automatic recovery mechanism for active ADB/scrcpy sessions.

## Trust boundaries

- Each endpoint verifies the relay using a trusted certificate or pinned SHA-256 leaf-certificate fingerprint. Pin rotation requires updating both configs.
- TLS terminates at the relay. These are two protected network legs, not application-level end-to-end encryption through an untrusted relay. Treat the relay as trusted infrastructure.
- Phone and Mac share one bearer credential; possession grants either relay role. There are no separate operator accounts or role-specific credentials.
- Android's normal ADB RSA authorization is still required. The bridge cannot unlock the phone, bypass permissions or start privileged TCP ADB by itself.
- Revocation rejects new handshakes. It does not terminate already authenticated waiting agents or active streams; disconnect those separately for immediate revocation.
- Legacy `adb tcpip 5555` may also listen on the phone's network interfaces. Outbound relaying does not force adbd itself to be loopback-only. Use trusted networks and retain ADB authorization.

## Operational limits and readiness

- Provision TCP ADB using an already authorized connection before remote use. Some phones reset this after reboot; the APK cannot restore it independently.
- Service start requires owner action, displays a foreground notification and offers Turn off. No boot receiver or sticky service restart is implemented.
- Network errors trigger phone reconnect backoff, starting at two seconds and capped at thirty. Active-session continuity is not guaranteed.
- A reachable relay, valid configs, running phone service, reachable local ADB and authorized Mac ADB key are all needed for useful access.
- No committed automated test suite, CI pipeline, APK or complete Gradle wrapper is present in this checkout.
- `VALIDATION.md` records earlier simulated Python tunnel/TLS checks and explicitly leaves APK compilation and real-phone acceptance incomplete.
- Review on 2026-10-21 rechecked Python syntax, Android XML parsing and shell syntax successfully. No APK build or real-device test was performed; Gradle was not on PATH and the wrapper is incomplete.

Code-review follow-ups, not verified failures: add-device writes the registry before validating the pin certificate; init-relay can replace an existing registry when certificate files are absent; Mac TLS mode values are not strictly validated; Android TLS handshake has no explicit read timeout. Harden these paths and verify the declared Android build toolchain before deployment claims.
