"""Windows Shell recycling with a callback that vetoes permanent deletion."""

import os
from pathlib import Path
from typing import Callable


def recycle_file(path: Path, before_recycle: Callable[[], None]) -> None:
    if os.name != "nt":
        raise OSError("Recycle Bin cleanup is supported only on Windows")

    import pythoncom
    import pywintypes
    from winerror import E_ABORT
    from win32com.server.exception import COMException
    from win32com.server.policy import DesignatedWrapPolicy
    from win32com.shell import shell, shellcon

    class RecycleSink(DesignatedWrapPolicy):
        _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
        _public_methods_ = [
            "StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem",
            "PreMoveItem", "PostMoveItem", "PreCopyItem", "PostCopyItem",
            "PreDeleteItem", "PostDeleteItem", "PreNewItem", "PostNewItem",
            "UpdateProgress", "ResetTimer", "PauseTimer", "ResumeTimer",
        ]

        def __init__(self):
            self._wrap_(self)
            self.error = ""
            self.recycled = False

        def noop(self, *args):
            pass

        StartOperations = FinishOperations = PreRenameItem = PostRenameItem = noop
        PreMoveItem = PostMoveItem = PreCopyItem = PostCopyItem = noop
        PreNewItem = PostNewItem = UpdateProgress = ResetTimer = PauseTimer = ResumeTimer = noop

        def PreDeleteItem(self, flags, item):
            try:
                if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                    raise OSError("Windows cannot recycle this item; permanent deletion was blocked")
                actual = Path(item.GetDisplayName(shellcon.SIGDN_FILESYSPATH))
                if os.path.normcase(str(actual)) != os.path.normcase(str(path)):
                    raise OSError("Windows selected an unexpected item; operation blocked")
                before_recycle()
            except Exception as exc:
                self.error = str(exc) or "Operation cancelled"
                # pywin32 ignores integer return values for this callback.
                # Raising a COM error is required to abort the Shell operation.
                raise COMException(desc=self.error, scode=E_ABORT)

        def PostDeleteItem(self, flags, item, result, new_item):
            # Windows can return nonzero success HRESULTs for recycled files.
            failed = bool(result & 0x80000000)
            self.recycled = not failed and new_item is not None
            if failed:
                self.error = self.error or f"Windows recycling failed (0x{result & 0xFFFFFFFF:08X})"
            elif new_item is None:
                self.error = "Windows did not confirm a Recycle Bin destination; check the item location"

    pythoncom.CoInitialize()
    operation = sink_interface = item = None
    sink = RecycleSink()
    try:
        operation = pythoncom.CoCreateInstance(
            shell.CLSID_FileOperation, None, pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IFileOperation,
        )
        operation.SetOperationFlags(
            shellcon.FOF_SILENT | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI
            | shellcon.FOF_NO_CONNECTED_ELEMENTS | shellcon.FOFX_EARLYFAILURE
            | 0x00080000  # FOFX_RECYCLEONDELETE
            | 0x20000000  # FOFX_ADDUNDORECORD
        )
        sink_interface = pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink)
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        operation.DeleteItem(item, sink_interface)
        operation.PerformOperations()
        if operation.GetAnyOperationsAborted() or not sink.recycled:
            raise OSError(sink.error or "Windows did not recycle this item")
    except pywintypes.com_error as exc:
        raise OSError(sink.error or str(exc)) from exc
    finally:
        operation = sink_interface = item = None
        pythoncom.CoUninitialize()
