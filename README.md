# Android Rescue Bridge

Two complementary tools for authorized Android data rescue and remote support from macOS. Built on Android Debug Bridge (ADB), with optional scrcpy screen control.

| Tool | Purpose | Get started |
| --- | --- | --- |
| **Android Rescue Local** | Save accessible phone files to your Mac over USB or local Wi-Fi. Includes selective copying, resumable sessions, integrity checks and acquisition reports. | [Local guide](android-rescue-local/README.md) |
| **Android Rescue Remote** | Reach an authorized phone across networks through a self-hosted TLS relay, then use ADB or scrcpy from your Mac. | [Remote guide](android-rescue-remote/README.md) |

Use **Local** to collect data. Use **Remote** to establish a distant connection. They are separate projects; their combined workflow has not yet been verified end to end.

Both require Android's normal ADB authorization. Neither bypasses screen locks, encrypted vaults or app protections, and neither provides a complete phone clone.

Implementation details: [Local architecture](android-rescue-local/ARCHITECTURE.md) · [Remote architecture](android-rescue-remote/ARCHITECTURE.md).

**Status:** Local has synthetic regression coverage; real-device acceptance remains pending. The remote Android app still requires build and device validation.
