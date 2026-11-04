# -*- coding: utf-8 -*-
"""Frozen-exe smoke: DicomQueue в бандле — доставка на живой SCP."""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from pynetdicom import AE, AllStoragePresentationContexts, evt

EXE = r'D:\build\RTLC\dist\rtlc.exe'
work = tempfile.mkdtemp(prefix='rtlc-frozen-')
remote = os.path.join(work, 'remote')
local = os.path.join(work, 'local')
os.makedirs(remote)
os.makedirs(local)

received = []


def on_c_store(event):
    ds = event.dataset
    ds.file_meta = event.file_meta
    received.append(ds)
    return 0x0000


scp_ae = AE(ae_title='FROZENSCP')
scp_ae.supported_contexts = AllStoragePresentationContexts
srv = scp_ae.start_server(('127.0.0.1', 0), block=False,
                          evt_handlers=[(evt.EVT_C_STORE, on_c_store)])
port = srv.server_address[1]

rnd = random.Random(99)
meta = FileMetaDataset()
meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
meta.MediaStorageSOPInstanceUID = generate_uid()
meta.TransferSyntaxUID = ExplicitVRLittleEndian
ds = Dataset()
ds.file_meta = meta
ds.SOPClassUID = SecondaryCaptureImageStorage
ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
ds.Modality = 'OT'
ds.PatientName = 'FROZEN^SMOKE'
ds.PatientID = 'FRZ1'
ds.Rows = 8
ds.Columns = 8
ds.BitsAllocated = 16
ds.BitsStored = 16
ds.HighBit = 15
ds.PixelRepresentation = 0
ds.SamplesPerPixel = 1
ds.PhotometricInterpretation = 'MONOCHROME2'
ds.PixelData = bytes(rnd.getrandbits(8) for _ in range(128))
src = os.path.join(remote, 'frozen.dcm')
ds.save_as(src, enforce_file_format=True)
src_bytes = open(src, 'rb').read()

with open(os.path.join(work, 'config.ini'), 'w', encoding='utf-8') as f:
    f.write(f'''[Options]
start_date = 2000-01-01 00:00
last_file = 0
refresh_time = 1
extension_file = .dcm
stabilize_time = 0.5

[Paths]
remote = {remote}
local = {local}

[Delivery]
to_folder = true
to_dicom = true

[Dicom]
host = 127.0.0.1
port = {port}
called_aet = FROZENSCP
calling_aet = RTLC
timeout = 10
retry_initial = 5
retry_max = 10
breaker_threshold = 3
breaker_cooldown = 30
queue_tick = 2

[Logs]
level = DEBUG
size = 5
backups = 2
''')

frozen_exe = os.path.join(work, 'rtlc.exe')
shutil.copy(EXE, frozen_exe)
proc = subprocess.Popen([frozen_exe, 'run'], cwd=work)
try:
    deadline = time.time() + 60
    ok = False
    while time.time() < deadline:
        if len(received) == 1 and len(os.listdir(local)) == 1:
            ok = True
            break
        time.sleep(1)
    log = open(os.path.join(work, 'Logs', 'rtlc.log'), encoding='utf-8').read()
    checks = {
        'dicom_queue_imported': 'DicomQueue started' in log,
        'scp_got_file': len(received) == 1,
        'folder_copy': len(os.listdir(local)) == 1,
        'sent_logged': 'sent to DICOM node' in log,
    }
    if received:
        checks['bytes_ok'] = received[0].PatientID == 'FRZ1'
    if os.listdir(local):
        checks['folder_bytes_ok'] = open(
            os.path.join(local, os.listdir(local)[0]), 'rb').read() == src_bytes
    checks['ALL'] = all(v for k, v in checks.items() if k != 'ALL')
    for k, v in checks.items():
        print(f'{k}: {v}')
    if not checks['ALL']:
        print('--- log tail ---')
        print('\n'.join(log.splitlines()[-15:]))
        sys.exit(1)
finally:
    proc.kill()
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
print('FROZEN-SMOKE-OK')
