# 🖥️ CursorRouter Desktop Application (WebView2)

Dedicated standalone Windows desktop application container for **CursorRouter**, powered by Microsoft Edge WebView2 (`pywebview`) and an embedded background orchestration service.

---

## 🌟 Key Capabilities

- **Native Edge Chromium Window**: Clean, minimalist dark interface running natively on Windows without browser chrome or external tabs.
- **Self-Contained Orchestrator**: Automatically boots local Flask server on `127.0.0.1:7860` (or finds next open port).
- **Live Cursor Process Detection**: Detects running `Cursor.exe` instances and displays dynamic status with 1-click startup reminders.
- **Anti-Detect Hardware Isolation**: Generates and injects isolated hardware fingerprints (`machineId`, `macMachineId`, `devDeviceId`, `sqmId`) directly into Cursor's `storage.json`.
- **Win32 Lifecycle Safety**: Unhooks and unsticks physical modifier keys (`Alt`, `Ctrl`, `Shift`, `Win`) upon exit.

---

## 🚀 Quick Start (Development)

1. Install desktop dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the desktop application:
   ```bash
   python desktop_main.py
   ```
   Or double-click `START_DESKTOP.bat`.

---

## 📦 Building Standalone Executable (Distribution)

To compile a single, zero-dependency Windows `.exe` (`CursorRouter.exe`):

```bash
python build.py --windowed
```
Or run `BUILD_APP.bat`.

### Command-Line Build Options

| Flag | Description |
|------|-------------|
| *(default)* | Pack everything into a single standalone `.exe` |
| `--onedir` | Generate an unpacked folder distribution (faster launch) |
| `--windowed` | Suppress console window on startup |

Compiled artifacts and update manifest (`manifest.json` with SHA-256 integrity hash) will be created in `desktop_app/dist/`.