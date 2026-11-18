# iOS Backup Generator

A Windows/macOS desktop utility for creating genuine iPhone/iPad backups through libimobiledevice, inspecting Apple backup folders, preparing deployment app lists, and making verified backup workspaces.

## Based on the supplied archives

The two supplied archives are native Apple backup folders. Both identify **iPad (10th generation), ProductType `iPad13,18`**, backup format **3.3**, and `SnapshotState=finished`.

| Archive | Product Version | Encrypted | IsFullBackup |
|---|---:|---:|---:|
| A | 26.6.1 | Yes | false |
| B | 18.0.1 | Yes | false |

The sanitized archive profiles include all app bundle IDs listed by each archive's `Info.plist` `Installed Applications` and `Applications` metadata. The merged app list is automatically selected when the program opens. It currently contains 11 unique bundle IDs across the two archives, including `com.apple.TestFlight`. These are identifiers observed in the archives, not installers or app binaries. Built-in iPadOS system apps are not necessarily represented as installable backup applications.

## Features

- Device inventory using `idevice_id` and optional `ideviceinfo`; executable paths are configurable in the app.
- Hover help on each libimobiledevice tool explains its purpose, setup, and when to use it.
- iPhone/iPad model catalog and target iOS/iPadOS planning selector.
- Automatically preloads the merged app list from both supplied archives; individual archive profiles remain selectable.
- Create native device backups with `idevicebackup2`; the device backup service creates the native `Manifest.db`.
- Verify `Manifest.db` presence and size; run SQLite `quick_check` and expected-table checks when it is a readable SQLite database.
- Unlock encrypted iOS 13+ backups locally with the backup encryption password using `iphone_backup_decrypt`; decrypt the keybag and a temporary copy of `Manifest.db` for browsing and searching.
- Decrypt and export selected encrypted regular files using their per-file protection-class keys. The source encrypted backup remains unchanged.
- Inspect backup metadata, encryption, snapshot state, and full-backup flag.
- Create an independent backup workspace and verify the complete file inventory and every copied file byte-for-byte against the source.
- Batch restore with basic device-family and OS preflight checks and `idevicebackup2 --copy` so the source backup is not modified by restore.
- Generate deployment project app lists and basic Wi-Fi `.mobileconfig` profiles.
- Record optional Apple Account email/label and whether each device should be signed in manually, left unchanged, or provisioned as a Managed Apple Account through MDM. No password, 2FA code, Keychain item, or authentication token is stored or copied.
- Export a full sanitized archive inventory from readable `Info.plist`, `Manifest.plist` and `Status.plist`, including all installed-app IDs, app metadata IDs, OS component bundle IDs, Apple Account/iCloud service IDs, iTunes settings/file keys and backup compatibility flags. A selected source archive can also add this inventory to a deployment project.
- Search encrypted or unencrypted backup `Manifest.db` entries by domain/path from a verified workspace. Export selected files as plaintext from encrypted backups; unencrypted backup export remains byte-preserving.
- Replace encrypted regular files with matching or changed plaintext sizes by reusing the per-file key; update the archived `Size` metadata, re-encrypt Manifest.db, and verify both payload and manifest round trips.

## Downloadable desktop builds

GitHub Actions builds standalone applications for:

- Windows x64 (`.exe`)
- macOS Intel (`.app`, x86_64)
- macOS Apple Silicon (`.app`, arm64)

Open the repository's **Actions → Build desktop applications** workflow and download the artifact matching your computer. Each build is self-contained and includes the app catalog JSON files. The workflow runs on pushes, pull requests, version tags, and manual dispatches. Pushing a version tag such as `v1.0.0` automatically publishes a GitHub Release with all three platform archives attached. To publish a release, create and push a version tag: `git tag v1.0.0` followed by `git push origin v1.0.0`. The macOS archive contains an `.app` bundle. These builds are unsigned; macOS may require approval in Privacy & Security before opening.

## Requirements and source launch

- Python 3.10+ with Tkinter.
- libimobiledevice tools: `idevicebackup2`, `idevice_id`, and preferably `ideviceinfo`. These external command-line tools are **not bundled** in the desktop executable.
- Windows source launch: run `run_windows.bat`.
- macOS source launch: `chmod +x run_macos.command && ./run_macos.command`.

The packaged app does not need Python installed, but it still needs compatible libimobiledevice tools installed and configured.

## Manifest.db and editing safety

A valid Apple backup's `Manifest.db` is not a generic app list. It is generated as part of a real device backup and contains file/domain records tied to the payload files and backup metadata. For encrypted backups, the database is encrypted/opaque until processed with the appropriate backup decryption workflow. Fabricating a new SQLite database from bundle IDs or changing manifest rows without updating the corresponding payloads can produce a backup that looks plausible but cannot be restored.

The app does not fabricate a native Manifest.db. It verifies the database created by `idevicebackup2` and creates a separate workspace copy with full inventory and SHA-256 verification. For encrypted backups, `iphone_backup_decrypt` unlocks the keybag, decrypts Manifest.db to a temporary working file, and decrypts selected payloads for export. The original encrypted Manifest.db is never overwritten by this read/browse workflow. For encrypted files, the editor can replace a regular file using its existing wrapped per-file key. Same-size replacements leave Manifest.db metadata unchanged. Size-changing replacements attempt to update the corresponding NSKeyedArchiver Size metadata, re-encrypt Manifest.db, and verify the round trip; only one size-changing edit may be pending before undo. This path is implemented but still needs validation against both supplied encrypted archives and a physical-device restore. Adding/deleting records, regenerating unsupported content digests, and general manifest restructuring are not supported. Test any edited workspace with a spare device before production restore. Validate every edited workspace with a test restore before using it for important devices.

The app list is a deployment-planning artifact only. Bundle IDs do not install apps. Use an authorized MDM/Apple Configurator workflow for actual app deployment and managed configuration.

### Apple Account / iCloud data

The supplied archives' readable `Manifest.plist` files list Apple Account and related iOS system services, but the `Info.plist` files do not expose an Apple ID email address or credentials. The backups are encrypted, so account databases, Keychain records, 2FA material and sign-in tokens cannot be inspected or safely transplanted by this app. The project builder therefore stores only an optional email/label (as plain text in the project folder) and per-device setup instruction. Apple Account authentication must be completed on each device; Managed Apple Accounts should be provisioned through an authorized MDM workflow. The editor can search account-related file paths in an unencrypted workspace, but intentionally refuses to display or modify credential stores.

## OS/model compatibility

Model and OS selectors are planning values; they do not change device hardware, install/downgrade iOS, or rewrite source backup metadata. Restore compatibility depends on the actual source backup and target device. The preflight checks are conservative warnings, not a guarantee that every cross-device restore is supported.

## Passwords and safety

Backup passwords are kept in process memory and passed through the documented `BACKUP_PASSWORD` environment variable. They are not saved in project files or command-line arguments. Restore overwrites target data; back up every target first, confirm authorization, and test with a non-production device.

The supplied archives are encrypted and marked `IsFullBackup=false`. The app has not yet been validated end-to-end with physical devices and Finder/Apple Devices. Do not treat a syntax/metadata check as proof of restore success.
