# iOS Backup Generator

A Windows/macOS desktop utility for managing Apple-compatible iPhone and iPad backups, inspecting existing Finder/Apple Devices backup folders, preparing deployment profiles, and restoring genuine device-generated backups to multiple devices.

## Important format distinction

A complete Apple backup is not a generic folder that can be invented from a model number and OS version. It contains device-generated manifests, file records, and potentially encryption keybag material. This application therefore creates **restorable backups through a connected device** using libimobiledevice. Offline projects are deployment templates (for example, app bundle-ID lists and configuration-profile payloads), not fabricated Finder backups.

## Features

- Device model catalog and target OS version selector.
- Sample profiles based on the supplied `iPad13,18` archives (iPadOS 18.0.1 and 26.6.1).
- Inspect native backup folders and ZIP-wrapped backup folders.
- Extract installed app bundle IDs from backup `Info.plist`.
- Create genuine backups with `idevicebackup2`.
- Batch restore a selected backup to multiple connected devices, with compatibility warnings.
- Generate a deployment project and basic `.mobileconfig` Wi-Fi/restriction payloads.
- Optional backup password via the `BACKUP_PASSWORD` environment variable; passwords are not stored in project files.
- Windows and macOS launch scripts.

## Requirements

- Python 3.10+
- `idevicebackup2` and `idevice_id` from libimobiledevice (plus `ideviceinfo` for richer device details)
- On macOS, install libimobiledevice with Homebrew (`brew install libimobiledevice`).
- On Windows, install a trusted libimobiledevice build and configure executable paths in the app.

## Run

Windows: `run_windows.bat`

macOS/Linux: `chmod +x run_macos.command && ./run_macos.command`

Or: `python -m pip install -r requirements.txt && python app.py`

## Backup compatibility

The selected target model and OS version are planning/preflight inputs. The app never edits `Product Type`, `Product Version`, `Manifest.db`, or keybag metadata to impersonate a different device/OS. Restore compatibility is determined from the real source backup and connected target device. A target may need to be updated before restoring a backup made on a newer OS.

## Test fixtures

The two user-provided archives describe iPad (10th generation), `iPad13,18`:
- iPadOS 18.0.1, backup format 3.3, encrypted, `IsFullBackup=false`.
- iPadOS 26.6.1, backup format 3.3, encrypted, `IsFullBackup=false`.

Only non-secret descriptive metadata is represented in the sample profile. Backup data and credentials are not included in this repository.
