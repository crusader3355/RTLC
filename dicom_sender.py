#Remote To Local Copy(RTLC) — отправка файлов на DICOM-узел (C-STORE SCU)
import pydicom
from pynetdicom import AE
from pynetdicom.sop_class import Verification
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian)


def _make_ae(calling_aet: str, timeout: int) -> AE:
    ae = AE(ae_title=calling_aet)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout
    return ae


def _associate(ae: AE, host: str, port: int, called_aet: str):
    return ae.associate(host, port, ae_title=called_aet)


def echo(host: str, port: int, called_aet: str, calling_aet: str,
         timeout: int) -> tuple:
    """C-ECHO до узла. Возвращает (ok, сообщение)."""
    try:
        ae = _make_ae(calling_aet, timeout)
        ae.add_requested_context(Verification)
        assoc = _associate(ae, host, port, called_aet)
        if not assoc.is_established:
            assoc.abort()
            return False, 'association rejected'
        status = assoc.send_c_echo()
        assoc.release()
        if status and status.Status == 0x0000:
            return True, 'C-ECHO success'
        return False, f'C-ECHO status: 0x{status.Status:04X}' if status else 'no response'
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'


def send_file(path: str, host: str, port: int, called_aet: str,
              calling_aet: str, timeout: int) -> tuple:
    """C-STORE одного файла. Возвращает (ok, ошибка|None, permanent).

    permanent=True — повторять бессмысленно (не DICOM, узел не понимает
    объект); permanent=False — транзиентная ошибка, нужен ретрай.
    """
    try:
        ds = pydicom.dcmread(path)
    except Exception as exc:
        return False, f'not a DICOM file: {exc}', True
    try:
        sop_class = ds.SOPClassUID
    except AttributeError:
        return False, 'no SOPClassUID in dataset', True
    transfer_syntax = ds.file_meta.get('TransferSyntaxUID')
    syntaxes = [u for u in (transfer_syntax, ImplicitVRLittleEndian,
                            ExplicitVRLittleEndian, ExplicitVRBigEndian)
                if u]
    # без дублей, порядок сохранён (родной синтаксис первым)
    syntaxes = list(dict.fromkeys(syntaxes))

    try:
        ae = _make_ae(calling_aet, timeout)
        ae.add_requested_context(sop_class, syntaxes)
        assoc = _associate(ae, host, port, called_aet)
    except Exception as exc:
        return False, f'association failed: {type(exc).__name__}: {exc}', False
    if not assoc.is_established:
        assoc.abort()
        return False, 'association rejected by the remote node', False
    try:
        status = assoc.send_c_store(ds)
    except Exception as exc:
        assoc.abort()
        return False, f'C-STORE failed: {type(exc).__name__}: {exc}', False
    finally:
        if assoc.is_established:
            assoc.release()
    if status is None:
        return False, 'no C-STORE response from the remote node', False
    code = status.Status
    if code == 0x0000:
        return True, None, False
    if 0xB000 <= code <= 0xBFFF:
        # предупреждение: узел сохранил объект с оговорками
        return True, f'warning 0x{code:04X}', False
    permanent = 0xC000 <= code <= 0xCFFF
    return False, f'C-STORE status 0x{code:04X}', permanent
