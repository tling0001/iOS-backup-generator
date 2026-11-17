# iOS Backup Generator

A Python/Tkinter desktop utility for Windows and macOS to create genuine iPhone/iPad backups through libimobiledevice, inspect existing Apple backup folders, select device-model targets for deployment planning, and prepare repeatable app/configuration-profile projects.

## Based on the supplied archives

The two supplied ZIP archives were inspected as native Apple backup folders. Both identify **iPad (10th generation), ProductType `iPad13,18`**, backup format **3.3**, and `SnapshotState=finished`. Their metadata differs as follows:

| Sample | Product Version | Encrypted | IsFullBackup |
|---|---:|---:|---:|
| Archive A | 26.6.1 | Yes | false |
| Archive B | 18.0.1 | Yes | false |

The sanitized `archive_profiles.json` preserves the model, OS version, backup metadata, and installed app bundle IDs from both archives. Personal device names, serial numbers, UDIDs, passwords, and backup contents are not included. The Deployment Project tab can load either sample's app list and automatically set the corresponding iPad model and OS planning values.

## Features

- Device inventory through `idevice_id` and `ideviceinfo`.
- Model selector with iPhone and iPad examples in `device_models.json`.
- Target iOS/iPadOS version selector.
- Create native device backups using `idevicebackup2`, including a full-backup request.
- Optional encryption setting change; default is to leave the device setting unchanged.
- Inspect native backup folders and ZIP-wrapped backup folders, including product model, OS, encryption, snapshot state, and full-backup flag.
- Batch restore to selected connected devices with basic OS-version and iPhone/iPad-family preflight checks.
- Generate a deployment project with app bundle IDs and target model/OS metadata.
- Generate a basic Wi-Fi `.mobileconfig` payload.

## Requirements and launch

- Python 3.10+ with Tkinter.
- libimobiledevice tools: `idevicebackup2`, `idevice_id`, and preferably `ideviceinfo`.
- Windows: run `run_windows.bat`.
- macOS: run `chmod +x run_macos.command && ./run_macos.command`.
- No third-party Python packages are required.

## Native backup validity and limitations

A valid Apple backup cannot be manufactured offline from a model number and an OS version. It contains device-generated manifests, file records, and (when encrypted) keybag material. The program therefore only creates restorable backup data through a real connected device. A deployment project is a separate template; an app bundle-ID list does not install apps. Use an authorized MDM/Apple Configurator workflow for app deployment and managed configuration.

The model and OS selectors are **planning/preflight metadata only**. They do not alter device hardware, install/downgrade iOS, or rewrite a backup's source identity. Restore compatibility is determined by the actual source backup and target device; basic checks here are not a guarantee that every cross-device restore is supported. Backups may contain device-specific data and should be treated as sensitive.

The two supplied examples both have `IsFullBackup=false`. Their snapshots are marked finished, but they should not be represented as full backups. The app will display this distinction when inspecting a source backup.

## Password handling

The app keeps the entered password in process memory and passes it to libimobiledevice using its documented `BACKUP_PASSWORD` environment variable. It does not save passwords to project files or place them in command-line arguments. Avoid using a shared computer and clear the password field when finished.

## Safety

Restoring replaces data on the target device. Make a current backup of every target before starting a restore, confirm device ownership/authorization, and test with a non-production device first. The current app has not been validated against a physical iPad or Finder/Apple Devices restore workflow; treat it as an initial implementation until that end-to-end test is completed.
