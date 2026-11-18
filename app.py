#!/usr/bin/env python3
"""iOS Backup Generator — native backup and deployment-profile manager."""
from __future__ import annotations
import json, os, plistlib, queue, shutil, subprocess, threading, zipfile, tempfile, uuid, hashlib, sqlite3, re, struct
from pathlib import Path
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

ROOT=Path(__file__).resolve().parent
APP="iOS Backup Generator"

def asset(name, fallback):
    try: return json.loads((ROOT/name).read_text(encoding="utf-8"))
    except Exception: return fallback

def exe(name): return shutil.which(name)
def safe(s): return ''.join(c if c.isalnum() or c in ' -_.' else '_' for c in s).strip(' .') or 'Backup'
def plist(path):
    with open(path,'rb') as f: return plistlib.load(f)
def locate(path):
    path=Path(path)
    if (path/'Info.plist').is_file() and (path/'Manifest.plist').is_file(): return path
    found=sorted({p.parent for p in path.rglob('Info.plist') if (p.parent/'Manifest.plist').is_file()})
    if len(found)==1: return found[0]
    if len(found)>1: raise ValueError('The selected folder contains multiple native backups. Select one backup folder.')
    raise ValueError('No native backup found. Select a folder containing Info.plist and Manifest.plist, or a ZIP containing one backup.')

def inspect(path):
    b=locate(path); info=plist(b/'Info.plist'); manifest=plist(b/'Manifest.plist')
    status=plist(b/'Status.plist') if (b/'Status.plist').exists() else {}
    if not (b/'Manifest.db').is_file(): raise ValueError('Manifest.db is missing.')
    return b,info,manifest,status

def sha256_file(path, chunk_size=1024 * 1024):
    """Hash large backup files without loading them fully into memory."""
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b''):
            digest.update(chunk)
    return digest.digest()


def validate_manifest(backup, encrypted):
    db=backup/'Manifest.db'
    if not db.is_file() or db.stat().st_size < 100:
        raise ValueError('Manifest.db is missing or unexpectedly small.')
    with db.open('rb') as f: header=f.read(16)
    if header == b'SQLite format 3\x00':
        con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            result=con.execute('PRAGMA quick_check').fetchone()
            if not result or result[0] != 'ok': raise ValueError('Manifest.db failed SQLite quick_check.')
            names={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'Files','Properties'}.issubset(names): raise ValueError('Manifest.db is missing expected Apple backup tables.')
            return {'kind':'SQLite','integrity':'ok','size':db.stat().st_size}
        finally: con.close()
    if encrypted:
        return {'kind':'encrypted opaque database','integrity':'not inspectable without decryption','size':db.stat().st_size}
    raise ValueError('Manifest.db is not SQLite and the backup is not marked encrypted.')




def build_archive_inventory(backup):
    backup=locate(backup)
    info=plist(backup/'Info.plist'); manifest=plist(backup/'Manifest.plist')
    status=plist(backup/'Status.plist') if (backup/'Status.plist').exists() else {}
    installed=sorted(set(info.get('Installed Applications',[]) or []))
    info_apps=info.get('Applications',{}) if isinstance(info.get('Applications',{}),dict) else {}
    os_apps=manifest.get('Applications',{}) if isinstance(manifest.get('Applications',{}),dict) else {}
    terms=('AppleAccount','AppleID','AccountsUI','MobileMe','iCloud','CloudKit','FindMy')
    account_services=sorted(k for k in os_apps if any(t.lower() in k.lower() for t in terms))
    return {
        'device': {'product_name':info.get('Product Name'),'product_type':info.get('Product Type'),'product_version':info.get('Product Version')},
        'backup': {'format_version':manifest.get('Version'),'encrypted':bool(manifest.get('IsEncrypted')),'snapshot_state':status.get('SnapshotState'),'is_full_backup':status.get('IsFullBackup')},
        'installed_applications':installed,
        'application_metadata_bundle_ids':sorted(info_apps.keys()),
        'os_application_component_bundle_ids':sorted(os_apps.keys()),
        'apple_account_icloud_system_components':account_services,
        'itunes_settings_keys':sorted((info.get('iTunes Settings') or {}).keys()) if isinstance(info.get('iTunes Settings'),dict) else [],
        'itunes_files_keys':sorted((info.get('iTunes Files') or {}).keys()) if isinstance(info.get('iTunes Files'),dict) else [],
        'note':'System component bundle IDs are OS inventory, not app installers. Account services do not reveal Apple Account credentials.'
    }



def validate_replacement_payload(backup, row, replacement):
    """Validate common structured payloads before a same-size replacement."""
    payload=validate_editable_payload(backup,row)
    replacement=Path(replacement)
    if not replacement.is_file():
        raise ValueError('Replacement file does not exist.')
    if replacement.stat().st_size!=payload.stat().st_size:
        raise ValueError('Replacement must have exactly the same byte length as the existing payload.')
    with payload.open('rb') as f: original_header=f.read(16)
    with replacement.open('rb') as f: replacement_header=f.read(16)
    if original_header==b'SQLite format 3\x00':
        if replacement_header!=b'SQLite format 3\x00':
            raise ValueError('The existing payload is SQLite; replacement must also be SQLite.')
        if Path(str(replacement)+'-wal').exists() or Path(str(replacement)+'-shm').exists():
            raise ValueError('Checkpoint the replacement SQLite database before importing it; WAL/SHM sidecars are not imported.')
        db=locate(backup)/'Manifest.db'
        con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            related=con.execute('SELECT 1 FROM Files WHERE domain=? AND relativePath IN (?,?) LIMIT 1',(row['domain'],row['relativePath']+'-wal',row['relativePath']+'-shm')).fetchone()
            if related:
                raise ValueError('The backup includes SQLite WAL/SHM companion files for this database; editing just the main database is unsafe.')
        finally:
            con.close()
        check=sqlite3.connect(replacement.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            result=check.execute('PRAGMA quick_check').fetchone()
            if not result or result[0]!='ok':
                raise ValueError('Replacement SQLite database failed quick_check.')
        finally:
            check.close()
    elif original_header==b'bplist00' or original_header.lstrip().startswith(b'<?xml'):
        try: plistlib.loads(replacement.read_bytes())
        except Exception as e: raise ValueError('Replacement property list is invalid: '+str(e))
    return payload


def manifest_search_rows(backup, query='', limit=2500):
    """Read indexed backup paths from an unencrypted native Manifest.db."""
    backup=locate(backup)
    manifest=plist(backup/'Manifest.plist')
    if manifest.get('IsEncrypted'):
        raise ValueError('This backup is encrypted. Its manifest and payloads are read-only.')
    db=backup/'Manifest.db'
    if (backup/'Manifest.db-wal').exists() or (backup/'Manifest.db-shm').exists():
        raise ValueError('Manifest.db has WAL/SHM sidecars. Checkpoint it with an Apple-compatible backup tool before editing.')
    validate_manifest(backup, False)
    con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        cols={r[1] for r in con.execute('PRAGMA table_info(Files)')}
        if not {'fileID','domain','relativePath','flags','file'}.issubset(cols):
            raise ValueError('Manifest.db Files table has an unsupported schema.')
        terms=[x.strip() for x in (query or '').split('|') if x.strip()]
        if terms:
            where=' OR '.join(['domain LIKE ? OR relativePath LIKE ?' for _ in terms])
            args=[]
            for term in terms: args.extend(['%'+term+'%','%'+term+'%'])
            args.append(int(limit))
            rows=con.execute('SELECT fileID,domain,relativePath,flags,file FROM Files WHERE '+where+' ORDER BY domain,relativePath LIMIT ?',args).fetchall()
        else:
            rows=con.execute('SELECT fileID,domain,relativePath,flags,file FROM Files ORDER BY domain,relativePath LIMIT ?',(int(limit),)).fetchall()
        return [{'fileID':r[0],'domain':r[1],'relativePath':r[2],'flags':r[3],'metadata':r[4]} for r in rows]
    finally:
        con.close()


def payload_for_row(backup, row):
    backup=locate(backup)
    domain=str(row['domain']); relative=str(row['relativePath']); file_id=str(row['fileID'])
    expected=hashlib.sha1((domain+'-'+relative).encode('utf-8')).hexdigest()
    if file_id.lower()!=expected:
        raise ValueError('Manifest fileID does not match its domain/path; refusing to touch this record.')
    if not re.fullmatch(r'[0-9a-fA-F]{40}',file_id):
        raise ValueError('Unsupported manifest fileID.')
    payload=backup/file_id[:2]/file_id
    if not payload.is_file():
        raise ValueError('The indexed payload file is missing.')
    return payload


def metadata_values(node, objects=None, seen=None):
    if seen is None: seen=set()
    if isinstance(node,dict):
        if isinstance(node.get('$objects'),list): objects=node['$objects']
        marker=id(node)
        if marker in seen: return
        seen.add(marker)
        for key,value in node.items():
            if key=='$objects': continue
            resolved=value
            if isinstance(value,plistlib.UID) and objects is not None and 0<=value.data<len(objects):
                resolved=objects[value.data]
            yield str(key),resolved
            if resolved is value:
                yield from metadata_values(value,objects,seen)
            else:
                yield from metadata_values(resolved,objects,seen)
    elif isinstance(node,(list,tuple)):
        marker=id(node)
        if marker in seen: return
        seen.add(marker)
        for value in node:
            resolved=value
            if isinstance(value,plistlib.UID) and objects is not None and 0<=value.data<len(objects):
                resolved=objects[value.data]
            yield from metadata_values(resolved,objects,seen)


def encrypt_cbc_file(source, destination, key):
    """Encrypt a file with the iOS backup AES-CBC zero-IV and PKCS#7 convention."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    cipher=AES.new(key,AES.MODE_CBC,iv=b'\\x00'*16)
    source=Path(source); destination=Path(destination)
    temp=destination.with_name(destination.name+'.encrypting')
    try:
        with source.open('rb') as src, temp.open('wb') as dst:
            pending=b''
            while True:
                chunk=src.read(1024*1024)
                if not chunk: break
                pending+=chunk
                usable=(len(pending)//16)*16
                if usable:
                    dst.write(cipher.encrypt(pending[:usable])); pending=pending[usable:]
            dst.write(cipher.encrypt(pad(pending,16)))
            dst.flush(); os.fsync(dst.fileno())
        os.replace(temp,destination)
    finally:
        if temp.exists(): temp.unlink()


def rewrite_encrypted_manifest_size(backup_obj, backup_dir, row, new_size, rollback_dir):
    """Update one NSKeyedArchiver Size field and re-encrypt Manifest.db safely."""
    from iphone_backup_decrypt import utils as backup_utils
    backup_dir=Path(backup_dir); rollback_dir=Path(rollback_dir)
    if (backup_dir/'Manifest.db-wal').exists() or (backup_dir/'Manifest.db-shm').exists():
        raise ValueError('Encrypted Manifest.db has WAL/SHM sidecars; refusing metadata rewrite.')
    work=Path(tempfile.mkdtemp(prefix='iosbg-manifest-',dir=rollback_dir))
    plain=work/'Manifest.db'
    changed=work/'Manifest.db.changed'
    encrypted=work/'Manifest.db.encrypted'
    verified=work/'Manifest.db.verified'
    try:
        backup_obj.save_manifest_file(str(plain))
        con=sqlite3.connect(plain)
        try:
            con.execute('PRAGMA journal_mode=DELETE')
            found=con.execute('SELECT file FROM Files WHERE fileID=? AND domain=? AND relativePath=?',(row['fileID'],row['domain'],row['relativePath'])).fetchone()
            if not found or not found[0]:
                raise ValueError('Could not find the exact encrypted Manifest.db record to update.')
            archived=plistlib.loads(found[0])
            top=archived.get('$top',{})
            root_uid=top.get('root')
            objects=archived.get('$objects')
            if not isinstance(root_uid,plistlib.UID) or not isinstance(objects,list) or not 0<=root_uid.data<len(objects):
                raise ValueError('Unsupported NSKeyedArchiver structure; refusing to rewrite metadata.')
            root=objects[root_uid.data]
            if not isinstance(root,dict) or 'Size' not in root or not isinstance(root['Size'],int):
                raise ValueError('Manifest file metadata has no directly editable Size integer.')
            root['Size']=int(new_size)
            if 'FileLength' in root:
                root['FileLength']=int(new_size)
            updated=plistlib.dumps(archived,fmt=plistlib.FMT_BINARY,sort_keys=False)
            con.execute('UPDATE Files SET file=? WHERE fileID=? AND domain=? AND relativePath=?',(updated,row['fileID'],row['domain'],row['relativePath']))
            if con.total_changes != 1:
                raise ValueError('Manifest metadata update did not affect exactly one record.')
            con.commit()
            check=con.execute('PRAGMA quick_check').fetchone()
            if not check or check[0]!='ok':
                raise ValueError('Updated decrypted Manifest.db failed SQLite quick_check.')
        finally:
            con.close()
        if Path(str(plain)+'-wal').exists() or Path(str(plain)+'-shm').exists():
            raise ValueError('Temporary Manifest.db has WAL/SHM sidecars after commit.')
        manifest_key_blob=backup_obj._manifest_plist.get('ManifestKey')
        if not isinstance(manifest_key_blob,bytes) or len(manifest_key_blob)<5:
            raise ValueError('Manifest.plist has no supported ManifestKey.')
        manifest_class=struct.unpack('<l',manifest_key_blob[:4])[0]
        manifest_key=backup_obj.keybag.unwrap_key_for_class(manifest_class,manifest_key_blob[4:])
        encrypt_cbc_file(plain,encrypted,manifest_key)
        backup_utils.aes_decrypt_chunked(in_filename=str(encrypted),out_filepath=str(verified),key=manifest_key)
        verify=sqlite3.connect(verified)
        try:
            result=verify.execute('PRAGMA quick_check').fetchone()
            if not result or result[0]!='ok':
                raise ValueError('Re-encrypted Manifest.db failed decrypt/SQLite round-trip verification.')
        finally:
            verify.close()
        return encrypted, work
    except Exception:
        shutil.rmtree(work,ignore_errors=True)
        raise


def validate_editable_payload(backup, row):
    backup=locate(backup)
    manifest=plist(backup/'Manifest.plist')
    if manifest.get('IsEncrypted'):
        raise ValueError('Encrypted backup editing is disabled. The original backup remains unchanged.')
    if (backup/'Manifest.db-wal').exists() or (backup/'Manifest.db-shm').exists():
        raise ValueError('Manifest.db WAL/SHM sidecars must be checkpointed before editing.')
    sensitive_terms=('keychain','accounts','appleid','appleaccount','mobileme','icloud','cloudkit','identityservices','com.apple.account')
    if any(term in (str(row.get('domain',''))+'/'+str(row.get('relativePath',''))).lower() for term in sensitive_terms):
        raise ValueError('Apple Account, iCloud, Keychain and authentication-related records are inventory-only and cannot be exported or replaced by this editor.')
    if int(row.get('flags',0))!=1:
        raise ValueError('Only regular files can be replaced. Directories and symlinks are not editable.')
    payload=payload_for_row(backup,row)
    raw=row.get('metadata')
    if not raw:
        raise ValueError('Manifest file metadata is empty.')
    try:
        meta=plistlib.loads(raw)
    except Exception as e:
        raise ValueError('Could not decode this file metadata plist: '+str(e))
    pairs=list(metadata_values(meta))
    if any(k in ('EncryptionKey','ProtectionClassKey') for k,v in pairs):
        raise ValueError('This file record contains encryption metadata; refusing to edit it.')
    if any(k in ('Digest','DataHash') for k,v in pairs):
        raise ValueError('This file record has a content digest that this editor cannot safely regenerate.')
    sizes=[v for k,v in pairs if k in ('Size','FileLength') and isinstance(v,int)]
    if sizes and any(int(v)!=payload.stat().st_size for v in sizes):
        raise ValueError('Payload size disagrees with Manifest.db metadata.')
    return payload


class ToolTip:
    """Small delayed hover help bubble for desktop controls."""
    def __init__(self, widget, text):
        self.widget=widget
        self.text=text
        self.window=None
        self.after_id=None
        widget.bind('<Enter>',self._schedule,add='+')
        widget.bind('<Leave>',self._hide,add='+')
        widget.bind('<ButtonPress>',self._hide,add='+')

    def _schedule(self, _event=None):
        self._hide()
        self.after_id=self.widget.after(550,self._show)

    def _show(self):
        self.after_id=None
        if self.window or not self.widget.winfo_exists():
            return
        try:
            x=self.widget.winfo_rootx()+12
            y=self.widget.winfo_rooty()+self.widget.winfo_height()+7
            tip=tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f'+{x}+{y}')
            frame=tk.Frame(tip,background='#fffbe8',borderwidth=1,relief='solid')
            frame.pack()
            label=tk.Label(frame,text=self.text,justify='left',anchor='w',
                           background='#fffbe8',foreground='#292929',
                           font=('Segoe UI',9),wraplength=390,
                           padx=10,pady=7)
            label.pack()
            self.window=tip
        except tk.TclError:
            self.window=None

    def _hide(self, _event=None):
        if self.after_id is not None:
            try: self.widget.after_cancel(self.after_id)
            except tk.TclError: pass
            self.after_id=None
        if self.window is not None:
            try: self.window.destroy()
            except tk.TclError: pass
            self.window=None


class Generator(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(APP); self.geometry('1180x850'); self.minsize(980,700)
        style=ttk.Style(self)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure('TButton',padding=(10,6))
        style.configure('TNotebook.Tab',padding=(14,8))
        style.configure('TLabelframe.Label',font=('Segoe UI',10,'bold'))
        self.models=asset('device_models.json',{'models':[]})['models']; self.samples=asset('archive_profiles.json',{'profiles':[]})['profiles']
        self.q=queue.Queue(); self.worker=None; self.devices=[]; self.apps=[]; self.active_password=''
        self.backup_tool=tk.StringVar(value=exe('idevicebackup2') or ''); self.id_tool=tk.StringVar(value=exe('idevice_id') or ''); self.info_tool=tk.StringVar(value=exe('ideviceinfo') or '')
        self.out=tk.StringVar(value=str(Path.home()/'iOS Backups')); self.source=tk.StringVar(); self.password=tk.StringVar()
        self.model=tk.StringVar(value='iPad (10th generation) — iPad13,18'); self.osver=tk.StringVar(value='26.6.1')
        self.sample=tk.StringVar(value='All apps from both archives (merged)'); self.project=tk.StringVar(value=str(Path.home()/'iOS Deployment Projects'/'New Project'))
        self.project_name=tk.StringVar(value='New iPad Deployment'); self.appid=tk.StringVar(); self.appurl=tk.StringVar()
        self.enc=tk.StringVar(value='Leave device setting unchanged'); self.full=tk.BooleanVar(value=True)
        self.profile_name=tk.StringVar(value='Fleet Configuration'); self.org=tk.StringVar(value='Organization'); self.profile_id=tk.StringVar(value='com.example.fleet.configuration')
        self.ssid=tk.StringVar(); self.wifi_pw=tk.StringVar(); self.security=tk.StringVar(value='WPA')
        self.edit_source=tk.StringVar(); self.edit_dest=tk.StringVar(value=str(Path.home()/'iOS Backup Workspaces')); self.editor_backup=tk.StringVar(); self.editor_query=tk.StringVar(); self.editor_rows=[]; self.encrypted_backup=None
        self.apple_email=tk.StringVar(); self.apple_label=tk.StringVar(); self.apple_account_mode=tk.StringVar(value='Sign in manually on each device')
        self._ui(); self.after(150,self.poll); self.refresh()

    def _ui(self):
        root=ttk.Frame(self,padding=12); root.pack(fill='both',expand=True)
        ttk.Label(root,text=APP,font=('Segoe UI',21,'bold')).pack(anchor='w')
        ttk.Label(root,text='Create genuine Apple backups, inspect supplied archives, select device models, and prepare repeatable deployment projects.',foreground='#555').pack(anchor='w',pady=(2,8))
        tools=ttk.LabelFrame(root,text='Device connection tools — hover over a tool or path for help',padding=8); tools.pack(fill='x')
        tool_help=[
            ('idevicebackup2',self.backup_tool,
             'Backup and restore engine. This is the libimobiledevice command that talks to the Apple mobile backup service. Select the idevicebackup2 executable. Use it to create native backups and restore a selected backup. Connect the device, unlock it, and tap Trust first. Restore overwrites device data.'),
            ('idevice_id',self.id_tool,
             'Device discovery. This command lists the UDIDs of iPhones and iPads visible over USB or supported network pairing. Select the idevice_id executable, then open the Devices tab and press Refresh to find connected devices.'),
            ('ideviceinfo (optional)',self.info_tool,
             'Device information reader. This optional command reads the device name, product/model identifier, and iOS version for the Devices list. If left blank, the app looks for ideviceinfo on PATH. It is not required to create a backup, but gives you more useful device details.')
        ]
        for i,(label,var,help_text) in enumerate(tool_help):
            lab=ttk.Label(tools,text=label,width=23)
            lab.grid(row=i,column=0,sticky='w',padx=5,pady=4)
            entry=ttk.Entry(tools,textvariable=var)
            entry.grid(row=i,column=1,sticky='ew',padx=5,pady=4)
            browse=ttk.Button(tools,text='Browse…',command=lambda v=var:self.pick_exe(v))
            browse.grid(row=i,column=2,padx=5,pady=3)
            for widget in (lab,entry,browse):
                ToolTip(widget,help_text)
        tools.columnconfigure(1,weight=1)
        ttk.Label(root,text='Tip: hover over the tool names above for setup guidance. Executable paths are detected automatically when available.',
                  foreground='#555',font=('Segoe UI',8)).pack(anchor='w',pady=(2,4))
        self.tabs=ttk.Notebook(root); self.tabs.pack(fill='both',expand=True,pady=8)
        self.dev=ttk.Frame(self.tabs,padding=10); self.create=ttk.Frame(self.tabs,padding=10); self.plan=ttk.Frame(self.tabs,padding=10); self.profile=ttk.Frame(self.tabs,padding=10); self.restore=ttk.Frame(self.tabs,padding=10); self.editor=ttk.Frame(self.tabs,padding=10)
        for f,t in [(self.dev,'Devices'),(self.create,'Create backup'),(self.plan,'Deployment project'),(self.profile,'Configuration profile'),(self.restore,'Inspect / restore'),(self.editor,'Safe workspace (copy only)')]: self.tabs.add(f,text=t)
        self.devices_ui(); self.create_ui(); self.plan_ui(); self.profile_ui(); self.restore_ui(); self.editor_ui()
        self.load_sample()
        logbox=ttk.LabelFrame(root,text='Activity',padding=5); logbox.pack(fill='both'); self.log=tk.Text(logbox,height=7,state='disabled',wrap='word'); self.log.pack(fill='both',expand=True)
        self.status=ttk.Label(root,text='Ready'); self.status.pack(anchor='w')
    def devices_ui(self):
        self.dev.rowconfigure(1,weight=1); self.dev.columnconfigure(0,weight=1)
        ttk.Label(self.dev,text='Connected devices (unlock and trust each device first)').grid(row=0,column=0,sticky='w')
        self.list=tk.Listbox(self.dev,selectmode='extended',exportselection=False); self.list.grid(row=1,column=0,sticky='nsew',pady=7)
        bar=ttk.Frame(self.dev); bar.grid(row=2,column=0,sticky='w'); ttk.Button(bar,text='Refresh',command=self.refresh).pack(side='left'); ttk.Button(bar,text='Select all',command=lambda:self.list.select_set(0,'end')).pack(side='left',padx=5)
        ttk.Label(self.dev,text='The model selector is for deployment planning. It does not change connected hardware or create a fabricated backup.',wraplength=850).grid(row=3,column=0,sticky='w',pady=8)
    def create_ui(self):
        self.create.columnconfigure(1,weight=1); self.pathrow(self.create,0,'Output folder',self.out,lambda:self.pick_dir(self.out))
        ttk.Checkbutton(self.create,text='Request full backup',variable=self.full).grid(row=1,column=0,columnspan=2,sticky='w',pady=5)
        ttk.Label(self.create,text='Encryption action').grid(row=2,column=0,sticky='w'); ttk.Combobox(self.create,textvariable=self.enc,values=['Leave device setting unchanged','Enable encryption','Disable encryption'],state='readonly').grid(row=2,column=1,sticky='ew')
        ttk.Label(self.create,text='Backup password (held in memory only)').grid(row=3,column=0,sticky='w',pady=8); ttk.Entry(self.create,textvariable=self.password,show='•').grid(row=3,column=1,sticky='ew')
        ttk.Label(self.create,text='Encryption is a device-level setting. Changing it affects future backups from that device. The tool never changes it silently.',foreground='#875500',wraplength=850).grid(row=4,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Button(self.create,text='Create backup(s) for selected devices',command=self.create_backups).grid(row=5,column=0,sticky='w',pady=8)
    def plan_ui(self):
        f=self.plan; f.columnconfigure(1,weight=1); f.rowconfigure(7,weight=1); self.pathrow(f,0,'Project folder',self.project,lambda:self.pick_dir(self.project))
        ttk.Label(f,text='Project name').grid(row=1,column=0,sticky='w'); ttk.Entry(f,textvariable=self.project_name).grid(row=1,column=1,sticky='ew',pady=3)
        ttk.Label(f,text='Target device model').grid(row=2,column=0,sticky='w'); choices=[f"{m['name']} — {m['identifier']}" for m in self.models]; ttk.Combobox(f,textvariable=self.model,values=choices,state='readonly').grid(row=2,column=1,sticky='ew',pady=3)
        ttk.Label(f,text='Target iOS / iPadOS version').grid(row=3,column=0,sticky='w'); ttk.Combobox(f,textvariable=self.osver,values=['18.0.1','26.6.1'],state='normal').grid(row=3,column=1,sticky='ew',pady=3)
        ttk.Label(f,text='App list source').grid(row=4,column=0,sticky='w'); line=ttk.Frame(f); line.grid(row=4,column=1,sticky='ew')
        ttk.Combobox(line,textvariable=self.sample,values=['All apps from both archives (merged)','Custom app list']+[x['label'] for x in self.samples],state='readonly').pack(side='left',fill='x',expand=True); ttk.Button(line,text='Load sample apps',command=self.load_sample).pack(side='left',padx=5)
        ttk.Label(f,text='Add bundle ID / optional App Store URL').grid(row=5,column=0,sticky='w',pady=5); line=ttk.Frame(f); line.grid(row=5,column=1,sticky='ew')
        ttk.Entry(line,textvariable=self.appid).pack(side='left',fill='x',expand=True); ttk.Entry(line,textvariable=self.appurl).pack(side='left',fill='x',expand=True,padx=5); ttk.Button(line,text='Add',command=self.add_app).pack(side='left')
        self.applist=tk.Listbox(f,height=8); self.applist.grid(row=7,column=0,columnspan=2,sticky='nsew',pady=5); ttk.Button(f,text='Remove selected',command=self.remove_app).grid(row=8,column=0,sticky='w')
        ttk.Label(f,text='Apple Account email / label (optional)').grid(row=9,column=0,sticky='w',pady=4); account=ttk.Frame(f); account.grid(row=9,column=1,sticky='ew')
        ttk.Entry(account,textvariable=self.apple_email).pack(side='left',fill='x',expand=True); ttk.Entry(account,textvariable=self.apple_label).pack(side='left',fill='x',expand=True,padx=(5,0))
        ttk.Label(f,text='Apple Account handling').grid(row=10,column=0,sticky='w',pady=4); ttk.Combobox(f,textvariable=self.apple_account_mode,values=['Sign in manually on each device','Leave Apple Account unchanged','Managed Apple Account via MDM'],state='readonly').grid(row=10,column=1,sticky='ew')
        ttk.Label(f,text='The optional email/label is saved as plain text in the project. No Apple Account passwords, 2FA codes, Keychain items or sign-in tokens are copied or stored. This is a deployment note only; sign in on each device using Apple’s own flow.',foreground='#805000',wraplength=900).grid(row=11,column=0,columnspan=2,sticky='w',pady=5)
        ttk.Label(f,text='Creates a deployment template, not a synthetic Finder backup. App bundle IDs do not install apps; use MDM or Apple Configurator for managed deployment. OS/model are planning values only.',wraplength=900).grid(row=12,column=0,columnspan=2,sticky='w',pady=5)
        ttk.Button(f,text='Create deployment project',command=self.create_project).grid(row=13,column=0,sticky='w')
    def profile_ui(self):
        f=self.profile; f.columnconfigure(1,weight=1)
        for i,(label,var) in enumerate([('Profile name',self.profile_name),('Organization',self.org),('Profile identifier',self.profile_id),('Wi-Fi SSID',self.ssid),('Wi-Fi password',self.wifi_pw)]):
            ttk.Label(f,text=label).grid(row=i,column=0,sticky='w',pady=4); ttk.Entry(f,textvariable=var,show='•' if var==self.wifi_pw else '').grid(row=i,column=1,sticky='ew',pady=4)
        ttk.Label(f,text='Wi-Fi security').grid(row=5,column=0,sticky='w'); ttk.Combobox(f,textvariable=self.security,values=['WPA','WEP','None'],state='readonly').grid(row=5,column=1,sticky='ew')
        ttk.Button(f,text='Generate .mobileconfig',command=self.make_profile).grid(row=6,column=0,sticky='w',pady=12)
    def restore_ui(self):
        f=self.restore; f.columnconfigure(0,weight=1); self.pathrow(f,0,'Native backup folder or ZIP',self.source,lambda:self.pick_backup())
        inspectbar=ttk.Frame(f); inspectbar.grid(row=1,column=0,sticky='w',pady=6)
        ttk.Button(inspectbar,text='Inspect backup metadata',command=self.inspect_dialog).pack(side='left')
        ttk.Button(inspectbar,text='Export full archive inventory…',command=self.export_archive_inventory).pack(side='left',padx=6)
        ttk.Label(f,text='Select target devices on the Devices tab. Restore replaces data. The source backup must be a genuine compatible device backup.',wraplength=850).grid(row=2,column=0,sticky='w',pady=6)
        ttk.Button(f,text='Restore source backup to selected devices…',command=self.restore_backups).grid(row=3,column=0,sticky='w')
    def editor_ui(self):
        f=self.editor; f.columnconfigure(1,weight=1); f.rowconfigure(5,weight=1)
        self.pathrow(f,0,'Source native backup folder',self.edit_source,lambda:self.pick_backup_into(self.edit_source))
        self.pathrow(f,1,'Workspace destination folder',self.edit_dest,lambda:self.pick_dir(self.edit_dest))
        bar=ttk.Frame(f); bar.grid(row=2,column=0,columnspan=2,sticky='w',pady=5)
        ttk.Button(bar,text='Create verified workspace',command=self.create_workspace).pack(side='left')
        ttk.Button(bar,text='Open / unlock workspace…',command=self.open_editor_workspace).pack(side='left',padx=6)
        ttk.Label(f,text='Active workspace').grid(row=3,column=0,sticky='w')
        ttk.Entry(f,textvariable=self.editor_backup,state='readonly').grid(row=3,column=1,sticky='ew')
        search=ttk.Frame(f); search.grid(row=4,column=0,columnspan=2,sticky='ew',pady=6)
        ttk.Label(search,text='Search domain or path').pack(side='left')
        ttk.Entry(search,textvariable=self.editor_query).pack(side='left',fill='x',expand=True,padx=6)
        ttk.Button(search,text='Search files',command=self.load_editor_files).pack(side='left')
        ttk.Button(search,text='Find account files',command=self.search_account_files).pack(side='left',padx=6)
        self.editor_list=tk.Listbox(f,height=12,exportselection=False); self.editor_list.grid(row=5,column=0,columnspan=2,sticky='nsew')
        actions=ttk.Frame(f); actions.grid(row=6,column=0,columnspan=2,sticky='w',pady=7)
        ttk.Button(actions,text='Export selected…',command=self.export_editor_file).pack(side='left')
        ttk.Button(actions,text='Replace (same size)…',command=self.replace_editor_file).pack(side='left',padx=6)
        ttk.Button(actions,text='Undo replacement',command=self.undo_editor_file).pack(side='left')
        ttk.Label(f,text='Encrypted workspaces can be unlocked, searched and decrypted for export. Encrypted regular files can be replaced with a new size: the app reuses the per-file key, updates the NSKeyedArchiver Size field in a decrypted Manifest.db copy, re-encrypts Manifest.db, and verifies both round trips. Only one size-changing edit may be pending before undo; Apple Account/Keychain secrets, directories, symlinks and unsupported records remain read-only.',foreground='#805000',wraplength=900,justify='left').grid(row=7,column=0,columnspan=2,sticky='w',pady=5)
    def pick_backup_into(self,var):
        p=filedialog.askdirectory()
        if p: var.set(p)
    def create_workspace(self):
        src=Path(self.edit_source.get()).expanduser()
        destroot=Path(self.edit_dest.get()).expanduser()
        if not src.exists():
            messagebox.showerror(APP,'Select an existing native backup folder.'); return
        try: b,info,manifest,status=inspect(src)
        except Exception as e: messagebox.showerror(APP,str(e)); return
        dest=destroot/(safe(b.name)+'_workspace_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
        def work():
            if destroot.resolve()==b.resolve() or b.resolve() in destroot.resolve().parents:
                raise RuntimeError('Workspace destination must be outside the source backup.')
            destroot.mkdir(parents=True,exist_ok=True)
            shutil.copytree(b,dest)
            validate_manifest(dest, bool(manifest.get('IsEncrypted')))
            source_files={p.relative_to(b) for p in b.rglob('*') if p.is_file()}
            copied_files={p.relative_to(dest) for p in dest.rglob('*') if p.is_file()}
            if source_files != copied_files:
                raise RuntimeError('Workspace file inventory differs from source; original remains untouched.')
            for rel in source_files:
                original=b/rel; copied=dest/rel
                if original.stat().st_size != copied.stat().st_size or sha256_file(original)!=sha256_file(copied):
                    raise RuntimeError('Workspace byte verification failed for '+str(rel)+'; original remains untouched.')
            self.logmsg(f'Verified {len(source_files)} files byte-for-byte in workspace: '+str(dest))
            self.logmsg('Original backup remains untouched. Verified workspace copy is ready for encrypted or unencrypted editing.')
            if self.encrypted_backup:
                self.encrypted_backup._cleanup()
            self.encrypted_backup=None
            self.q.put(('editor_backup',str(dest)))
        self.start(work)
    def open_editor_workspace(self):
        p=filedialog.askdirectory(title='Select an existing workspace copy')
        if not p: return
        try:
            b=locate(Path(p))
            if '_workspace_' not in b.name:
                raise ValueError('For safety, only open a verified workspace copy, never the original backup.')
            manifest=plist(b/'Manifest.plist')
            if manifest.get('IsEncrypted'):
                password=self.password.get()
                if password == '':
                    password=simpledialog.askstring(APP,'Enter the encrypted backup password (spaces are significant):',show='•',parent=self)
                if password is None or password == '':
                    return
                def unlock():
                    from iphone_backup_decrypt import EncryptedBackup
                    unlocked=EncryptedBackup(backup_directory=str(b),passphrase=password)
                    unlocked.test_decryption()
                    if self.encrypted_backup:
                        self.encrypted_backup._cleanup()
                    self.encrypted_backup=unlocked
                    self.logmsg('Encrypted backup keybag unlocked and Manifest.db decrypted in a temporary private working directory.')
                    self.q.put(('editor_backup',str(b)))
                    self.q.put(('editor_refresh',None))
                self.start(unlock)
            else:
                validate_manifest(b,False)
                if self.encrypted_backup:
                    self.encrypted_backup._cleanup()
                self.encrypted_backup=None
                self.editor_backup.set(str(b)); self.load_editor_files()
        except Exception as e: messagebox.showerror(APP,str(e))

    def load_editor_files(self):
        backup=self.editor_backup.get().strip()
        if not backup: messagebox.showwarning(APP,'Create or open a workspace first.'); return
        query=self.editor_query.get()
        def work():
            if self.encrypted_backup:
                terms=[x.strip() for x in (query or '').split('|') if x.strip()]
                with self.encrypted_backup.manifest_db_cursor() as cur:
                    if terms:
                        where=' OR '.join(['domain LIKE ? OR relativePath LIKE ?' for _ in terms])
                        args=[]
                        for term in terms: args.extend(['%'+term+'%','%'+term+'%'])
                        args.append(2500)
                        cur.execute('SELECT fileID,domain,relativePath,flags,file FROM Files WHERE '+where+' ORDER BY domain,relativePath LIMIT ?',args)
                    else:
                        cur.execute('SELECT fileID,domain,relativePath,flags,file FROM Files ORDER BY domain,relativePath LIMIT 2500')
                    rows=[{'fileID':r[0],'domain':r[1],'relativePath':r[2],'flags':r[3],'metadata':r[4]} for r in cur.fetchall()]
            else:
                rows=manifest_search_rows(backup,query)
            self.q.put(('editor_rows',rows))
            self.logmsg('Loaded '+str(len(rows))+' manifest file entries'+(' (showing up to 2,500).' if len(rows)>=2500 else '.'))
        self.start(work)

    def search_account_files(self):
        self.editor_query.set('Accounts|AppleID|iCloud|MobileMe|CloudKit|Keychain')
        self.load_editor_files()
        self.logmsg('Account inventory is path-only. Credential databases, Keychain secrets and authentication tokens are not displayed or modified.')

    def selected_editor_row(self):
        sel=self.editor_list.curselection()
        if not sel or sel[0]>=len(self.editor_rows):
            messagebox.showwarning(APP,'Select a manifest file first.'); return None
        return self.editor_rows[sel[0]]

    def export_editor_file(self):
        row=self.selected_editor_row()
        if not row: return
        backup=self.editor_backup.get()
        try:
            if self.encrypted_backup:
                from iphone_backup_decrypt import utils as backup_utils
                sensitive=('keychain','accounts','appleid','appleaccount','mobileme','icloud','cloudkit','identityservices')
                if any(term in (str(row['domain'])+'/'+str(row['relativePath'])).lower() for term in sensitive):
                    raise ValueError('Apple Account, iCloud, Keychain and authentication-related records are inventory-only.')
                meta=backup_utils.FilePlist(row['metadata'])
                if meta.encryption_key is None or int(row.get('flags',0))!=1:
                    raise ValueError('Selected record is not an encrypted regular file.')
                if any(k in meta._data for k in ('Digest','DataHash')):
                    raise ValueError('This record contains an unsupported content digest.')
                key=self.encrypted_backup.keybag.unwrap_key_for_class(meta.protection_class,meta.encryption_key)
                payload=Path(backup_utils.backup_file_path(backup,row['fileID']))
            else:
                payload=validate_editable_payload(backup,row)
        except Exception as e: messagebox.showerror(APP,str(e)); return
        dest=filedialog.asksaveasfilename(initialfile=Path(row['relativePath']).name)
        if not dest: return
        def work():
            if self.encrypted_backup:
                self.encrypted_backup._decrypt_file_to_disk(file_id=row['fileID'],key=key,file_plist=meta,output_filepath=dest)
                self.logmsg('Decrypted and exported '+row['domain']+'/'+row['relativePath']+' to '+dest)
            else:
                shutil.copy2(payload,dest)
                self.logmsg('Exported '+row['domain']+'/'+row['relativePath']+' to '+dest)
        self.start(work)

    def replace_editor_file(self):
        row=self.selected_editor_row()
        if not row: return
        backup=self.editor_backup.get()
        encrypted=bool(self.encrypted_backup)
        try:
            if encrypted:
                from iphone_backup_decrypt import utils as backup_utils
                sensitive=('keychain','accounts','appleid','appleaccount','mobileme','icloud','cloudkit','identityservices')
                if any(term in (str(row['domain'])+'/'+str(row['relativePath'])).lower() for term in sensitive):
                    raise ValueError('Apple Account, iCloud, Keychain and authentication-related records are inventory-only.')
                if int(row.get('flags',0))!=1:
                    raise ValueError('Only regular files can be replaced.')
                expected=hashlib.sha1((str(row['domain'])+'-'+str(row['relativePath'])).encode('utf-8')).hexdigest()
                if str(row['fileID']).lower()!=expected:
                    raise ValueError('Manifest fileID does not match its domain/path.')
                meta=backup_utils.FilePlist(row['metadata'])
                if meta.encryption_key is None or any(k in meta._data for k in ('Digest','DataHash')):
                    raise ValueError('This file record lacks a usable per-file key or contains an unsupported digest.')
                key=self.encrypted_backup.keybag.unwrap_key_for_class(meta.protection_class,meta.encryption_key)
                payload=Path(backup_utils.backup_file_path(backup,str(row['fileID'])))
                expected_size=meta.filesize
            else:
                payload=validate_editable_payload(backup,row)
                expected_size=payload.stat().st_size
        except Exception as e:
            messagebox.showerror(APP,str(e)); return
        replacement=filedialog.askopenfilename(title='Choose replacement file')
        if not replacement: return
        replacement=Path(replacement)
        if not encrypted and replacement.stat().st_size!=expected_size:
            messagebox.showerror(APP,'Replacement must be exactly '+str(expected_size)+' bytes. This editor does not rewrite unencrypted manifest metadata.'); return
        if not messagebox.askyesno(APP,'Replace this file in the workspace? The original source backup remains untouched. A rollback copy is saved outside the backup folder.'):
            return
        rollback=Path(backup).parent/(Path(backup).name+'.iosbg-rollback')
        saved=rollback/(str(row['fileID'])+'.encrypted' if encrypted else str(row['fileID']))
        saved_manifest=rollback/(str(row['fileID'])+'.manifest.encrypted')
        def work():
            rollback.mkdir(parents=True,exist_ok=True)
            if encrypted:
                from iphone_backup_decrypt import utils as backup_utils
                if replacement.stat().st_size!=expected_size:
                    pending=[p for p in rollback.glob('*.manifest.encrypted') if p.is_file()]
                    own_manifest=rollback/(str(row['fileID'])+'.manifest.encrypted')
                    if pending and not own_manifest.exists():
                        raise RuntimeError('A different size-changing edit already has a pending Manifest.db rollback. Undo it first so metadata rollback order remains consistent.')
                verify_old=rollback/(str(row['fileID'])+'.old-verify')
                self.encrypted_backup._decrypt_file_to_disk(file_id=str(row['fileID']),key=key,file_plist=meta,output_filepath=str(verify_old))
                if verify_old.stat().st_size!=expected_size:
                    verify_old.unlink(missing_ok=True)
                    raise RuntimeError('Existing encrypted payload size disagrees with Manifest.db; refusing replacement.')
                verify_old.unlink()
                if not saved.exists(): shutil.copy2(payload,saved)
                temp_cipher=payload.with_name(payload.name+'.iosbg-encrypted-tmp')
                temp_plain=rollback/(str(row['fileID'])+'.replacement-verify')
                manifest_temp=None
                old_manifest=backup/'Manifest.db'
                try:
                    encrypt_cbc_file(replacement,temp_cipher,key)
                    decrypted_size=backup_utils.aes_decrypt_chunked(in_filename=str(temp_cipher),out_filepath=str(temp_plain),key=key)
                    if decrypted_size!=replacement.stat().st_size or sha256_file(temp_plain)!=sha256_file(replacement):
                        raise RuntimeError('Encrypted replacement failed plaintext round-trip verification.')
                    if replacement.stat().st_size!=expected_size:
                        manifest_temp,workdir=rewrite_encrypted_manifest_size(self.encrypted_backup,backup,row,replacement.stat().st_size,rollback)
                    if manifest_temp is None:
                        os.replace(temp_cipher,payload)
                    else:
                        try:
                            if not saved_manifest.exists(): shutil.copy2(old_manifest,saved_manifest)
                            os.replace(temp_cipher,payload)
                            os.replace(manifest_temp,old_manifest)
                            derived_key=self.encrypted_backup.keybag.passphrase_key
                            self.encrypted_backup._cleanup()
                            from iphone_backup_decrypt import EncryptedBackup
                            refreshed=EncryptedBackup(backup_directory=str(backup),passphrase_key=derived_key)
                            refreshed.test_decryption()
                            self.encrypted_backup=refreshed
                        except Exception:
                            shutil.copy2(saved,payload)
                            if saved_manifest.exists():
                                shutil.copy2(saved_manifest,old_manifest)
                                saved_manifest.unlink(missing_ok=True)
                            raise
                    self.q.put(('editor_refresh',None))
                finally:
                    temp_cipher.unlink(missing_ok=True)
                    temp_plain.unlink(missing_ok=True)
                    if manifest_temp is not None:
                        shutil.rmtree(manifest_temp.parent,ignore_errors=True)
            else:
                validate_replacement_payload(backup,row,replacement)
                if not saved.exists(): shutil.copy2(payload,saved)
                temp=payload.with_name(payload.name+'.iosbg-tmp')
                try:
                    shutil.copyfile(replacement,temp)
                    with open(temp,'rb+') as stream: os.fsync(stream.fileno())
                    if temp.stat().st_size!=payload.stat().st_size: raise RuntimeError('Replacement size changed during copy.')
                    if sha256_file(temp)!=sha256_file(replacement): raise RuntimeError('Replacement file verification failed.')
                    os.replace(temp,payload)
                finally:
                    if temp.exists(): temp.unlink()
            self.logmsg('Replaced '+row['domain']+'/'+row['relativePath']+(' with encrypted payload and updated Manifest.db Size metadata' if encrypted else '')+'; rollback saved at '+str(saved))
        self.start(work)

    def undo_editor_file(self):
        row=self.selected_editor_row()
        if not row: return
        backup=Path(self.editor_backup.get())
        encrypted=bool(self.encrypted_backup)
        try:
            if encrypted:
                from iphone_backup_decrypt import utils as backup_utils
                expected=hashlib.sha1((str(row['domain'])+'-'+str(row['relativePath'])).encode('utf-8')).hexdigest()
                if str(row['fileID']).lower()!=expected or int(row.get('flags',0))!=1:
                    raise ValueError('Unsupported encrypted manifest record.')
                payload=Path(backup_utils.backup_file_path(str(backup),str(row['fileID'])))
            else:
                payload=validate_editable_payload(backup,row)
        except Exception as e:
            messagebox.showerror(APP,str(e)); return
        rollback=backup.parent/(backup.name+'.iosbg-rollback')
        saved=rollback/(str(row['fileID'])+'.encrypted' if encrypted else str(row['fileID']))
        saved_manifest=rollback/'Manifest.db.encrypted'
        if not saved.is_file():
            messagebox.showinfo(APP,'No rollback copy exists for this file.'); return
        if not messagebox.askyesno(APP,'Restore the original payload and any saved Manifest.db metadata?'): return
        def work():
            temp=payload.with_name(payload.name+'.iosbg-undo')
            try:
                shutil.copyfile(saved,temp)
                if sha256_file(temp)!=sha256_file(saved):
                    raise RuntimeError('Rollback verification failed.')
                os.replace(temp,payload); saved.unlink()
                if encrypted and saved_manifest.is_file():
                    old_manifest=backup/'Manifest.db'
                    shutil.copy2(saved_manifest,old_manifest)
                    saved_manifest.unlink()
                    derived_key=self.encrypted_backup.keybag.passphrase_key
                    self.encrypted_backup._cleanup()
                    from iphone_backup_decrypt import EncryptedBackup
                    refreshed=EncryptedBackup(backup_directory=str(backup),passphrase_key=derived_key)
                    refreshed.test_decryption()
                    self.encrypted_backup=refreshed
                    self.q.put(('editor_refresh',None))
            finally:
                if temp.exists(): temp.unlink()
            self.logmsg('Restored original '+('encrypted ' if encrypted else '')+'payload and metadata for '+row['domain']+'/'+row['relativePath'])
        self.start(work)

    def pathrow(self,f,r,label,var,cmd):
        ttk.Label(f,text=label).grid(row=r,column=0,sticky='w',pady=3); line=ttk.Frame(f); line.grid(row=r,column=1,sticky='ew'); line.columnconfigure(0,weight=1); ttk.Entry(line,textvariable=var).grid(row=0,column=0,sticky='ew'); ttk.Button(line,text='Browse',command=cmd).grid(row=0,column=1,padx=5)
    def pick_dir(self,var):
        p=filedialog.askdirectory()
        if p: var.set(p)
    def pick_exe(self,var):
        p=filedialog.askopenfilename()
        if p: var.set(p)
    def pick_backup(self):
        p=filedialog.askopenfilename(filetypes=[('ZIP archive','*.zip'),('All files','*.*')]) or filedialog.askdirectory()
        if p: self.source.set(p)
    def logmsg(self,s): self.q.put(('log',s))
    def poll(self):
        try:
            while True:
                k,v=self.q.get_nowait()
                if k=='log':
                    self.log.configure(state='normal'); self.log.insert('end',v+'\n'); self.log.see('end'); self.log.configure(state='disabled')
                elif k=='devices':
                    self.devices=v; self.list.delete(0,'end')
                    for d in v: self.list.insert('end',f"{d['name']} | {d['product']} | iOS {d['version']} | {d['udid']}")
                elif k=='done': self.status.configure(text=v)
                elif k=='editor_backup': self.editor_backup.set(v); self.editor_query.set(''); self.editor_list.delete(0,'end')
                elif k=='editor_rows':
                    self.editor_rows=v; self.editor_list.delete(0,'end')
                    for r in v: self.editor_list.insert('end',r['domain']+'  |  '+r['relativePath']+'  |  flags='+str(r['flags']))
        except queue.Empty: pass
        self.after(150,self.poll)
    def start(self,fn):
        if self.worker and self.worker.is_alive(): messagebox.showwarning(APP,'Another operation is running.'); return
        self.status.configure(text='Working…')
        def go():
            try: fn(); self.q.put(('done','Completed'))
            except Exception as e: self.logmsg('ERROR: '+str(e)); self.q.put(('done','Failed — see activity log'))
        self.worker=threading.Thread(target=go,daemon=True); self.worker.start()
    def run(self,args):
        self.logmsg('$ '+' '.join(map(str,args))); env=os.environ.copy()
        if self.active_password: env['BACKUP_PASSWORD']=self.active_password
        p=subprocess.Popen(list(map(str,args)),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',env=env)
        for line in p.stdout:
            if line.strip(): self.logmsg(line.rstrip())
        rc=p.wait()
        if rc: raise RuntimeError(f'Command failed with exit code {rc}')
    def refresh(self):
        id_tool=self.id_tool.get()
        if not id_tool or not Path(id_tool).is_file(): self.logmsg('idevice_id not found. Install libimobiledevice and set its executable path in the tools section above.'); return
        def work():
            ds=[]; info_tool=self.info_tool.get().strip() or exe('ideviceinfo')
            if info_tool and not Path(info_tool).is_file():
                self.logmsg('ideviceinfo path is not a file; device discovery will continue without detailed device information.')
                info_tool=None
            raw=subprocess.check_output([id_tool,'-l'],text=True,stderr=subprocess.STDOUT,timeout=20)
            for udid in [x.strip() for x in raw.splitlines() if x.strip()]:
                d={'udid':udid,'name':'iOS device','product':'','version':''}
                if info_tool:
                    for key,k in [('DeviceName','name'),('ProductType','product'),('ProductVersion','version')]:
                        try: d[k]=subprocess.check_output([info_tool,'-u',udid,'-k',key],text=True,stderr=subprocess.DEVNULL,timeout=8).strip() or d[k]
                        except Exception: pass
                ds.append(d)
            self.q.put(('devices',ds)); self.logmsg(f'Detected {len(ds)} device(s).')
        threading.Thread(target=work,daemon=True).start()
    def selected(self): return [self.devices[i] for i in self.list.curselection() if i<len(self.devices)]
    def load_sample(self):
        if self.sample.get()=='All apps from both archives (merged)':
            ids=self.samples_data_all_apps()
            p=max(self.samples,key=lambda x:tuple(int(v) for v in x['product_version'].split('.')))
            label='both supplied archives (merged)'
        else:
            p=next((x for x in self.samples if x['label']==self.sample.get()),None)
            if not p: return
            ids=sorted(set(p.get('all_observed_app_ids',p.get('installed_apps',[]))))
            label=p['label']
        self.apps=[{'bundle_id':x,'app_store_url':None} for x in ids]; self.applist.delete(0,'end')
        for x in self.apps: self.applist.insert('end',x['bundle_id'])
        self.model.set(p['product_name']+' — '+p['product_type']); self.osver.set(p['product_version'])
        self.logmsg(f"Loaded {len(self.apps)} distinct app IDs from {label}. Apple bundle IDs are retained too.")
    def samples_data_all_apps(self):
        ids=set()
        for p in self.samples: ids.update(p.get('all_observed_app_ids',p.get('installed_apps',[])))
        return sorted(ids)
    def add_app(self):
        b=self.appid.get().strip(); u=self.appurl.get().strip()
        if not b or '.' not in b: messagebox.showerror(APP,'Enter a valid bundle identifier.'); return
        if u and not u.startswith('https://'): messagebox.showerror(APP,'App Store URL must use HTTPS.'); return
        if not any(x['bundle_id']==b for x in self.apps): self.apps.append({'bundle_id':b,'app_store_url':u or None}); self.applist.insert('end',b)
        self.appid.set(''); self.appurl.set('')
    def remove_app(self):
        for i in reversed(self.applist.curselection()): self.applist.delete(i); self.apps.pop(i)
    def create_project(self):
        dest=Path(self.project.get()).expanduser()
        if dest.exists() and any(dest.iterdir()): dest=dest/(safe(self.project_name.get())+'_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
        project_name=self.project_name.get(); model=self.model.get(); osver=self.osver.get(); apps=list(self.apps)
        apple_email=self.apple_email.get().strip(); apple_label=self.apple_label.get().strip(); apple_mode=self.apple_account_mode.get(); source_value=self.source.get().strip()
        def work():
            dest.mkdir(parents=True,exist_ok=True)
            (dest/'project.json').write_text(json.dumps({'name':project_name,'target_model':model,'target_os_version':osver,'created_utc':datetime.now(timezone.utc).isoformat()},indent=2))
            (dest/'apps.json').write_text(json.dumps({'apps':apps},indent=2))
            account={'email':apple_email,'label':apple_label,'handling':apple_mode,'credentials_included':False,'setup_required':apple_mode!='Leave Apple Account unchanged'}
            (dest/'apple_account_setup.json').write_text(json.dumps(account,indent=2))
            if source_value:
                b,tmp=self.resolve(source_value)
                try: (dest/'archive_inventory.json').write_text(json.dumps(build_archive_inventory(b),indent=2,ensure_ascii=False),encoding='utf-8')
                finally:
                    if tmp: shutil.rmtree(tmp,ignore_errors=True)
            (dest/'README.md').write_text('# '+project_name+'\n\nDeployment template only. It is not a synthetic Apple backup. Apple Account sign-in must be completed on each device. No credentials or tokens are included. Archive system components are inventory-only and are not installed by this project.\n')
            self.logmsg('Created deployment project: '+str(dest))
        self.start(work)
    def make_profile(self):
        try:
            payloads=[]
            if self.ssid.get().strip():
                p={'PayloadType':'com.apple.wifi.managed','PayloadVersion':1,'PayloadIdentifier':self.profile_id.get()+'.wifi','PayloadUUID':str(uuid.uuid4()).upper(),'PayloadDisplayName':'Wi-Fi '+self.ssid.get(),'SSID_STR':self.ssid.get(),'HIDDEN_NETWORK':False,'AutoJoin':True,'EncryptionType':self.security.get()}
                if self.wifi_pw.get(): p['Password']=self.wifi_pw.get()
                payloads.append(p)
            d={'PayloadType':'Configuration','PayloadVersion':1,'PayloadIdentifier':self.profile_id.get(),'PayloadUUID':str(uuid.uuid4()).upper(),'PayloadDisplayName':self.profile_name.get(),'PayloadOrganization':self.org.get(),'PayloadScope':'System','PayloadContent':payloads}
            path=filedialog.asksaveasfilename(defaultextension='.mobileconfig',filetypes=[('Configuration profile','*.mobileconfig')])
            if path:
                with open(path,'wb') as f: plistlib.dump(d,f,fmt=plistlib.FMT_XML,sort_keys=False)
                self.logmsg('Generated '+path)
        except Exception as e: messagebox.showerror(APP,str(e))
    def create_backups(self):
        ds=self.selected()
        if not ds: messagebox.showwarning(APP,'Select connected devices.'); return
        if not messagebox.askyesno(APP,f'Create native backups for {len(ds)} selected devices?'): return
        out=Path(self.out.get()).expanduser(); mode=self.enc.get()
        if mode!='Leave device setting unchanged' and not self.password.get(): messagebox.showerror(APP,'Enter the current/new backup password first.'); return
        full=bool(self.full.get()); password=self.password.get(); backup_tool=self.backup_tool.get()
        def work():
            try:
                self.active_password=password
                out.mkdir(parents=True,exist_ok=True)
                for d in ds:
                    folder=out/(safe(d['name'])+'_'+d['udid']+'_'+datetime.now().strftime('%Y%m%d_%H%M%S')); folder.mkdir()
                    if mode!='Leave device setting unchanged': self.run([backup_tool,'-u',d['udid'],'encryption','on' if mode=='Enable encryption' else 'off'])
                    args=[backup_tool,'-u',d['udid'],'backup']+(['--full'] if full else [])+[str(folder)]; self.run(args)
                    b=locate(folder); _,inf,man,st=inspect(b)
                    report=validate_manifest(b, bool(man.get('IsEncrypted')))
                    self.logmsg('Manifest.db generated and checked: '+str(report))
                    if st.get('SnapshotState')!='finished': raise RuntimeError('Backup did not reach SnapshotState=finished.')
                    self.logmsg(f"Verified {d['udid']}: {inf.get('Product Type')} / {inf.get('Product Version')} / encrypted={man.get('IsEncrypted')}")
            finally:
                self.active_password=''
        self.start(work)
    def resolve(self, source_value=None):
        p=Path(source_value if source_value is not None else self.source.get()).expanduser()
        if p.is_file() and zipfile.is_zipfile(p):
            t=Path(tempfile.mkdtemp(prefix='iosbg-'))
            try:
                with zipfile.ZipFile(p) as z:
                    for x in z.infolist():
                        out=(t/x.filename).resolve()
                        if t.resolve() not in out.parents and out!=t.resolve(): raise ValueError('Unsafe ZIP entry path.')
                    z.extractall(t)
                return locate(t),t
            except Exception:
                shutil.rmtree(t,ignore_errors=True)
                raise
        return locate(p),None
    def export_archive_inventory(self):
        source_value=self.source.get().strip()
        if not source_value: messagebox.showwarning(APP,'Select a native backup folder or ZIP first.'); return
        dest=filedialog.asksaveasfilename(defaultextension='.json',filetypes=[('JSON inventory','*.json')],initialfile='ios-backup-inventory.json')
        if not dest: return
        def work():
            b,tmp=self.resolve(source_value)
            try:
                inventory=build_archive_inventory(b)
                Path(dest).write_text(json.dumps(inventory,indent=2,ensure_ascii=False),encoding='utf-8')
                self.logmsg('Exported full archive inventory: '+dest)
            finally:
                if tmp: shutil.rmtree(tmp,ignore_errors=True)
        self.start(work)

    def inspect_dialog(self):
        try:
            b,i,m,s=inspect(self.resolve()[0]); dbreport=validate_manifest(b,bool(m.get('IsEncrypted')))
            app_records=m.get('Applications',{}) if isinstance(m.get('Applications',{}),dict) else {}
            account_terms=('AppleAccount','AppleID','AccountsUI','MobileMe','iCloud','CloudKit','FindMy')
            account_services=sorted(k for k in app_records if any(term.lower() in k.lower() for term in account_terms))
            service_text='\n'.join(account_services[:30]) if account_services else 'No matching service names found in readable Manifest.plist.'
            messagebox.showinfo(APP,f"{i.get('Product Name')} ({i.get('Product Type')})\niOS/iPadOS {i.get('Product Version')}\nEncrypted: {m.get('IsEncrypted')}\nManifest.db: {dbreport}\nSnapshot: {s.get('SnapshotState')}\nFull backup: {s.get('IsFullBackup')}\nBackup format: {s.get('Version')}\nInstalled apps: {len(i.get('Installed Applications',[]))}\nOS application/component records: {len(app_records)}\nApple Account / iCloud system components: {len(account_services)}\n\n{service_text}\n\nThe service inventory is OS metadata, not an Apple ID or login token. The supplied archives are marked IsFullBackup=false.")
        except Exception as e: messagebox.showerror(APP,str(e))
    def restore_backups(self):
        ds=self.selected()
        if not ds or not self.source.get(): messagebox.showwarning(APP,'Select devices and a source backup.'); return
        if not messagebox.askyesno(APP,'Restore overwrites data on every selected device. Back up targets first. Continue?',icon='warning'): return
        source_value=self.source.get(); password=self.password.get(); backup_tool=self.backup_tool.get()
        def work():
            try:
                self.active_password=password
                b,tmp=self.resolve(source_value); _,i,m,s=inspect(b)
                if s.get('SnapshotState')!='finished': raise ValueError('Source backup is not marked finished.')
                if m.get('IsEncrypted') and not password: raise ValueError('Enter the source backup password.')
                ver=tuple(int(x) for x in str(i.get('Product Version','0')).split('.')[:3]); family=str(i.get('Product Type',''))
                for d in ds:
                    dv=tuple(int(x) for x in str(d.get('version','0')).split('.')[:3]) if d.get('version') else ()
                    if not dv or dv<ver: raise ValueError(f"Target {d['udid']} is older/unknown OS than source {i.get('Product Version')}; no restore started.")
                    if family.startswith('iPad') and not d.get('product','').startswith('iPad'): raise ValueError('iPad backup cannot be applied to a non-iPad target.')
                    if family.startswith('iPhone') and not d.get('product','').startswith('iPhone'): raise ValueError('iPhone backup cannot be applied to a non-iPhone target.')
                source_udid=str(i.get('Target Identifier') or i.get('Unique Identifier') or b.name)
                for d in ds: self.run([backup_tool,'-u',d['udid'],'-s',source_udid,'restore','--copy',str(b.parent)])
            finally:
                self.active_password=''
                if 'tmp' in locals() and tmp: shutil.rmtree(tmp,ignore_errors=True)
        self.start(work)

if __name__=='__main__':
    Generator().mainloop()
