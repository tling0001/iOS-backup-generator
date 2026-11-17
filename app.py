#!/usr/bin/env python3
"""iOS Backup Generator — native backup and deployment-profile manager."""
from __future__ import annotations
import json, os, plistlib, queue, shutil, subprocess, threading, zipfile, tempfile, uuid, hashlib, sqlite3
from pathlib import Path
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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
        self.backup_tool=tk.StringVar(value=exe('idevicebackup2') or ''); self.id_tool=tk.StringVar(value=exe('idevice_id') or '')
        self.out=tk.StringVar(value=str(Path.home()/'iOS Backups')); self.source=tk.StringVar(); self.password=tk.StringVar()
        self.model=tk.StringVar(value='iPad (10th generation) — iPad13,18'); self.osver=tk.StringVar(value='26.6.1')
        self.sample=tk.StringVar(value='All apps from both archives (merged)'); self.project=tk.StringVar(value=str(Path.home()/'iOS Deployment Projects'/'New Project'))
        self.project_name=tk.StringVar(value='New iPad Deployment'); self.appid=tk.StringVar(); self.appurl=tk.StringVar()
        self.enc=tk.StringVar(value='Leave device setting unchanged'); self.full=tk.BooleanVar(value=True)
        self.profile_name=tk.StringVar(value='Fleet Configuration'); self.org=tk.StringVar(value='Organization'); self.profile_id=tk.StringVar(value='com.example.fleet.configuration')
        self.ssid=tk.StringVar(); self.wifi_pw=tk.StringVar(); self.security=tk.StringVar(value='WPA')
        self.edit_source=tk.StringVar(); self.edit_dest=tk.StringVar(value=str(Path.home()/'iOS Backup Workspaces'))
        self._ui(); self.after(150,self.poll); self.refresh()

    def _ui(self):
        root=ttk.Frame(self,padding=12); root.pack(fill='both',expand=True)
        ttk.Label(root,text=APP,font=('Segoe UI',21,'bold')).pack(anchor='w')
        ttk.Label(root,text='Create genuine Apple backups, inspect supplied archives, select device models, and prepare repeatable deployment projects.',foreground='#555').pack(anchor='w',pady=(2,8))
        tools=ttk.LabelFrame(root,text='libimobiledevice tools',padding=6); tools.pack(fill='x')
        for i,(label,var) in enumerate([('idevicebackup2',self.backup_tool),('idevice_id',self.id_tool)]):
            ttk.Label(tools,text=label).grid(row=i,column=0,sticky='w',padx=5,pady=3); ttk.Entry(tools,textvariable=var).grid(row=i,column=1,sticky='ew',padx=5); ttk.Button(tools,text='Browse',command=lambda v=var:self.pick_exe(v)).grid(row=i,column=2,padx=5)
        tools.columnconfigure(1,weight=1)
        self.tabs=ttk.Notebook(root); self.tabs.pack(fill='both',expand=True,pady=8)
        self.dev=ttk.Frame(self.tabs,padding=10); self.create=ttk.Frame(self.tabs,padding=10); self.plan=ttk.Frame(self.tabs,padding=10); self.profile=ttk.Frame(self.tabs,padding=10); self.restore=ttk.Frame(self.tabs,padding=10); self.editor=ttk.Frame(self.tabs,padding=10)
        for f,t in [(self.dev,'Devices'),(self.create,'Create backup'),(self.plan,'Deployment project'),(self.profile,'Configuration profile'),(self.restore,'Inspect / restore'),(self.editor,'Safe backup workspace')]: self.tabs.add(f,text=t)
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
        ttk.Label(f,text='Creates a project template, not a synthetic Finder backup. App bundle IDs do not install apps; use MDM or Apple Configurator for managed deployment. OS/model are planning and compatibility values only.',wraplength=900).grid(row=9,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Button(f,text='Create deployment project',command=self.create_project).grid(row=10,column=0,sticky='w')
    def profile_ui(self):
        f=self.profile; f.columnconfigure(1,weight=1)
        for i,(label,var) in enumerate([('Profile name',self.profile_name),('Organization',self.org),('Profile identifier',self.profile_id),('Wi-Fi SSID',self.ssid),('Wi-Fi password',self.wifi_pw)]):
            ttk.Label(f,text=label).grid(row=i,column=0,sticky='w',pady=4); ttk.Entry(f,textvariable=var,show='•' if var==self.wifi_pw else '').grid(row=i,column=1,sticky='ew',pady=4)
        ttk.Label(f,text='Wi-Fi security').grid(row=5,column=0,sticky='w'); ttk.Combobox(f,textvariable=self.security,values=['WPA','WEP','None'],state='readonly').grid(row=5,column=1,sticky='ew')
        ttk.Button(f,text='Generate .mobileconfig',command=self.make_profile).grid(row=6,column=0,sticky='w',pady=12)
    def restore_ui(self):
        f=self.restore; f.columnconfigure(0,weight=1); self.pathrow(f,0,'Native backup folder or ZIP',self.source,lambda:self.pick_backup())
        ttk.Button(f,text='Inspect backup metadata',command=self.inspect_dialog).grid(row=1,column=0,sticky='w',pady=6)
        ttk.Label(f,text='Select target devices on the Devices tab. Restore replaces data. The source backup must be a genuine compatible device backup.',wraplength=850).grid(row=2,column=0,sticky='w',pady=6)
        ttk.Button(f,text='Restore source backup to selected devices…',command=self.restore_backups).grid(row=3,column=0,sticky='w')
    def editor_ui(self):
        f=self.editor; f.columnconfigure(1,weight=1)
        self.pathrow(f,0,'Source native backup folder',self.edit_source,lambda:self.pick_backup_into(self.edit_source))
        self.pathrow(f,1,'Workspace destination folder',self.edit_dest,lambda:self.pick_dir(self.edit_dest))
        ttk.Label(f,text='Safe editing workflow',font=('Segoe UI',12,'bold')).grid(row=2,column=0,columnspan=2,sticky='w',pady=(12,4))
        ttk.Label(f,text='Creates an independent, verified working copy. The original is never modified. Encrypted Manifest.db files remain opaque and are preserved byte-for-byte. Direct edits to backup payloads or manifest records are blocked to avoid corrupt restores.',wraplength=900,justify='left').grid(row=3,column=0,columnspan=2,sticky='w')
        ttk.Button(f,text='Create and verify safe workspace copy',command=self.create_workspace).grid(row=4,column=0,sticky='w',pady=10)
        ttk.Label(f,text='Manifest.db is generated by idevicebackup2 during a real device backup. This app verifies that it exists and checks SQLite integrity where possible; it will not fabricate a database from app IDs.',foreground='#805000',wraplength=900,justify='left').grid(row=5,column=0,columnspan=2,sticky='w',pady=5)
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
            self.logmsg('Original backup remains untouched. Direct payload editing is intentionally blocked until manifest-aware editing is supported.')
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
        if not id_tool or not Path(id_tool).exists(): self.logmsg('idevice_id not found. Install libimobiledevice and set the executable path.'); return
        def work():
            ds=[]; info_tool=exe('ideviceinfo')
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
        def work():
            dest.mkdir(parents=True,exist_ok=True)
            (dest/'project.json').write_text(json.dumps({'name':project_name,'target_model':model,'target_os_version':osver,'created_utc':datetime.now(timezone.utc).isoformat()},indent=2))
            (dest/'apps.json').write_text(json.dumps({'apps':apps},indent=2))
            (dest/'README.md').write_text('# '+project_name+'\n\nDeployment template only. It is not a synthetic Apple backup.\n')
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
        full=bool(self.full.get()); self.active_password=self.password.get(); backup_tool=self.backup_tool.get()
        def work():
            try:
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
    def inspect_dialog(self):
        try:
            b,i,m,s=inspect(self.resolve()[0]); dbreport=validate_manifest(b,bool(m.get('IsEncrypted'))); messagebox.showinfo(APP,f"{i.get('Product Name')} ({i.get('Product Type')})\niOS/iPadOS {i.get('Product Version')}\nEncrypted: {m.get('IsEncrypted')}\nManifest.db: {dbreport}\nSnapshot: {s.get('SnapshotState')}\nFull backup: {s.get('IsFullBackup')}\nBackup format: {s.get('Version')}\nInstalled apps: {len(i.get('Installed Applications',[]))}\n\nThe supplied archives are marked IsFullBackup=false.")
        except Exception as e: messagebox.showerror(APP,str(e))
    def restore_backups(self):
        ds=self.selected()
        if not ds or not self.source.get(): messagebox.showwarning(APP,'Select devices and a source backup.'); return
        if not messagebox.askyesno(APP,'Restore overwrites data on every selected device. Back up targets first. Continue?',icon='warning'): return
        source_value=self.source.get(); password=self.password.get(); self.active_password=password; backup_tool=self.backup_tool.get()
        def work():
            try:
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
                if tmp: shutil.rmtree(tmp,ignore_errors=True)
            finally:
                self.active_password=''
        self.start(work)

if __name__=='__main__':
    Generator().mainloop()
